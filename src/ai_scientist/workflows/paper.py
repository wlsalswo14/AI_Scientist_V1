from __future__ import annotations

import json

from ..agents import ReviewerAgent, WriterAgent
from ..artifacts import ArtifactStore
from ..config import Settings, repair_budget_exhausted
from ..quality import compare_quality
from ..progress import ProgressTracker, paper_progress_vector
from ..runtime import DeadlinePolicy, RuntimePhase
from ..schemas import (
    ClaimLedger,
    ExperimentStageResult,
    PaperStageResult,
    ResearchStageResult,
    ResearchResultStatus,
    ProvenanceGraph,
    WorkflowAction,
)
from ..validation import (
    compact_validation,
    validate_paper_draft,
    validate_review,
)


REVIEW_CRITERIA = {
    "Mode Fit",
    "Significance",
    "Literature Accuracy",
    "Target Clarity",
    "Experimental Validity",
    "Statistical Validity",
    "Result-Claim Consistency",
    "Citation-Claim Consistency",
    "Negative-Result Reporting",
    "Claim Ceiling Compliance",
    "Reproducibility",
    "Writing Quality",
}

REVIEW_HARD_GATES = {
    "Mode Fit",
    "Literature Accuracy",
    "Experimental Validity",
    "Statistical Validity",
    "Result-Claim Consistency",
    "Citation-Claim Consistency",
    "Negative-Result Reporting",
    "Claim Ceiling Compliance",
    "Reproducibility",
}


class PaperWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        writer: WriterAgent,
        reviewer: ReviewerAgent,
        deadline_policy: DeadlinePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.writer = writer
        self.reviewer = reviewer
        self.deadline_policy = deadline_policy
        self.progress = ProgressTracker(
            store,
            "paper",
            lower_is_better={
                "fatal_issues",
                "non_fatal_issues",
                "acceptance_conditions",
            },
            max_stagnant_rounds=settings.max_stagnant_rounds,
            state_store=(
                deadline_policy.state_store if deadline_policy is not None else None
            ),
        )

    async def run(
        self,
        research_stage: ResearchStageResult,
        experiment_stage: ExperimentStageResult,
        claim_ledger: ClaimLedger,
        provenance_graph: ProvenanceGraph,
    ) -> PaperStageResult:
        if not experiment_stage.passed:
            raise RuntimeError(
                "PaperWorkflow requires an ExperimentStageResult that passed all "
                "execution and Ex-Evaluator hard gates"
            )
        prior_draft = None
        prior_review = None
        prior_scores = None
        last_result = None
        start_round = 1
        frozen_package = {
            "pipeline_smoke_test": self.settings.pipeline_smoke_test,
            "question": research_stage.contract.original_question,
            "research_mode": research_stage.contract.research_mode,
            "research_profile": research_stage.contract.research_profile,
            "research_readiness": research_stage.contract.readiness,
            "claim_ceiling": research_stage.contract.claim_ceiling,
            "research_contract": research_stage.contract.model_dump(mode="json"),
            "selected_target_ids": research_stage.contract.selected_target_ids,
            "experiment_contract": experiment_stage.contract.model_dump(mode="json"),
            "experimentor_outputs": [
                item.model_dump(mode="json")
                for item in experiment_stage.experimentor_outputs
            ],
            "execution_results": [
                item.model_dump(mode="json") for item in experiment_stage.executions
            ],
            "experiment_evaluation": experiment_stage.evaluation.model_dump(mode="json"),
            "claim_ledger": claim_ledger.model_dump(mode="json"),
            "trace_preparation": (
                experiment_stage.trace_preparation.model_dump(mode="json")
                if experiment_stage.trace_preparation is not None
                else None
            ),
            "provenance_graph": provenance_graph.model_dump(mode="json"),
            "trace_reviewer_decisions": (
                experiment_stage.trace_reviewer_decisions.model_dump(mode="json")
                if experiment_stage.trace_reviewer_decisions is not None
                else None
            ),
        }
        evidence_ids = {
            item.evidence_id for item in research_stage.contract.evidence
        }
        result_ids = {
            result_id
            for execution in experiment_stage.executions
            for result_id in execution.result_ids
        }
        result_payloads: dict[str, object] = {}
        for execution in experiment_stage.executions:
            for relative_path, content in execution.output_files.items():
                result_id = f"{execution.experiment_id}:{relative_path}"
                if result_id not in result_ids or not relative_path.endswith(".json"):
                    continue
                try:
                    result_payloads[result_id] = json.loads(content)
                except json.JSONDecodeError:
                    continue
        checkpoint = self.store.load_checkpoint("paper", PaperStageResult)
        if (
            checkpoint is not None
            and not checkpoint.accepted
            and checkpoint.draft.research_mode == research_stage.contract.research_mode
            and checkpoint.draft.research_profile
            == research_stage.contract.research_profile
            and checkpoint.draft.claim_ceiling == research_stage.contract.claim_ceiling
            and all(
                set(claim.result_ids).issubset(result_ids)
                for claim in checkpoint.draft.linked_claims
            )
        ):
            if (
                checkpoint.review.action != WorkflowAction.RETURN_TO_WRITER
                or any(
                    reason.startswith("DEADLINE:")
                    for reason in checkpoint.failure_reasons
                )
            ):
                return checkpoint
            prior_draft = checkpoint.draft.model_dump(mode="json")
            prior_review = checkpoint.review.model_dump(mode="json")
            prior_scores = checkpoint.review.criteria
            last_result = checkpoint
            start_round = checkpoint.round_number + 1
            self.store.event(
                "paper.resumed_from_checkpoint",
                {"next_round": start_round},
            )
        execution_trace_ids = {
            trace
            for execution in experiment_stage.executions
            for trace in (execution.experiment_id, execution.code_hash)
        }
        frozen_package["available_evidence_ids"] = sorted(evidence_ids)
        frozen_package["available_result_ids"] = sorted(result_ids)
        frozen_package["available_execution_trace_ids"] = sorted(
            execution_trace_ids
        )
        expected_execution_keys = {
            (item.experiment_id, item.code_hash) for item in experiment_stage.executions
        }
        execution_dependencies: list[str] = []
        for artifact_id in self.store.find_artifact_ids(kind="execution-result"):
            envelope = self.store.artifact_envelope(artifact_id)
            if envelope is None:
                continue
            payload = envelope.get("payload", {})
            if (payload.get("experiment_id"), payload.get("code_hash")) in (
                expected_execution_keys
            ):
                execution_dependencies.append(artifact_id)
        base_dependencies = self.store.find_artifact_ids(
            kind="research-contract-final"
        )[-1:] + execution_dependencies
        negative_results_required = any(
            judgment.status
            in {
                ResearchResultStatus.PARTIALLY_SUPPORTED,
                ResearchResultStatus.NOT_SUPPORTED,
                ResearchResultStatus.FALSIFIED,
                ResearchResultStatus.INCONCLUSIVE,
                ResearchResultStatus.PROTOCOL_VIOLATION,
            }
            for judgment in experiment_stage.evaluation.judgments
        )
        deadline_stop_reason: str | None = None
        for round_number in range(
            start_round,
            self.settings.max_review_rounds + 1,
        ):
            if self.deadline_policy is not None:
                deadline = self.deadline_policy.can_start(RuntimePhase.PAPER)
                if not deadline.allowed:
                    deadline_stop_reason = deadline.reason
                    self.store.event(
                        "deadline.round_blocked",
                        {
                            "stage": "paper",
                            "round": round_number,
                            "reason": deadline.reason,
                            "remaining_seconds": deadline.remaining_seconds,
                        },
                    )
                    break
            writer_payload = {
                **frozen_package,
                "previous_draft": prior_draft,
                "review_notebook": prior_review,
            }
            draft = await self.writer.run(
                writer_payload,
                session_label=f"writer-round-{round_number}",
            )
            writer_attempt = 0
            previous_draft_id: str | None = None
            while True:
                draft_id = self.store.save(
                    "paper-draft",
                    draft,
                    dependencies=base_dependencies
                    + ([previous_draft_id] if previous_draft_id else []),
                    metadata={
                        "round": round_number,
                        "repair_attempt": writer_attempt,
                    },
                )
                draft_validation = validate_paper_draft(
                    draft,
                    expected_mode=research_stage.contract.research_mode,
                    expected_claim_ceiling=research_stage.contract.claim_ceiling,
                    evidence_ids=evidence_ids,
                    result_ids=result_ids,
                    negative_results_required=negative_results_required,
                    result_payloads=result_payloads,
                    expected_profile=research_stage.contract.research_profile,
                    claim_ledger=claim_ledger,
                )
                self.store.save(
                    "paper-draft-validation",
                    compact_validation(draft_validation),
                    dependencies=[draft_id],
                    metadata={
                        "round": round_number,
                        "repair_attempt": writer_attempt,
                    },
                )
                if draft_validation.valid:
                    break
                if repair_budget_exhausted(
                    writer_attempt, self.settings.max_component_repair_attempts
                ):
                    raise RuntimeError("Writer failed its Harness repair budget")
                writer_attempt += 1
                previous_draft_id = draft_id
                draft = await self.writer.run(
                    {
                        **writer_payload,
                        "repair_only": {
                            "prior_output": draft.model_dump(mode="json"),
                            "validation_issues": compact_validation(
                                draft_validation
                            ),
                            "instruction": (
                                "Return a corrected complete draft. Preserve the "
                                "frozen mode, claim ceiling, evidence, and results."
                            ),
                        },
                    },
                    session_label=(
                        f"writer-round-{round_number}-repair-{writer_attempt}"
                    ),
                )
            claim_ids = {item.claim_id for item in draft.linked_claims}
            known_review_ids = (
                evidence_ids | result_ids | execution_trace_ids | claim_ids
            )
            reviewer_payload = {
                "paper": draft.model_dump(mode="json"),
                "frozen_research_package": frozen_package,
                "paper_claim_ids": sorted(claim_ids),
                "known_review_trace_ids": sorted(known_review_ids),
                "previous_paper": prior_draft,
                "previous_review": prior_review,
                "thresholds": {
                    "minimum_passing_score": self.settings.minimum_gate_score,
                    "score_drop_absolute": self.settings.score_drop_absolute,
                    "score_drop_relative": self.settings.score_drop_relative,
                    "critical_dimension_drop": self.settings.critical_dimension_drop,
                },
            }
            review = await self.reviewer.run(
                reviewer_payload,
                session_label=f"reviewer-isolated-round-{round_number}",
            )
            reviewer_attempt = 0
            previous_review_id: str | None = None
            while True:
                review_id = self.store.save(
                    "review-report",
                    review,
                    dependencies=[draft_id]
                    + ([previous_review_id] if previous_review_id else []),
                    metadata={
                        "round": round_number,
                        "isolated": True,
                        "repair_attempt": reviewer_attempt,
                    },
                )
                quality_delta = compare_quality(
                    review.criteria,
                    prior_scores,
                    critical_dimensions={
                        "Citation-Claim Consistency",
                        "Result-Claim Consistency",
                        "Negative-Result Reporting",
                        "Claim Ceiling Compliance",
                    },
                    settings=self.settings,
                )
                validation = validate_review(
                    review,
                    required_criteria=REVIEW_CRITERIA,
                    hard_gate_criteria=REVIEW_HARD_GATES,
                    known_trace_ids=known_review_ids,
                    rubric_version=self.settings.rubric_version,
                    minimum_passing_score=self.settings.minimum_gate_score,
                    significant_degradation=(
                        quality_delta.significant_degradation
                    ),
                )
                self.store.save(
                    "review-validation",
                    {"issues": compact_validation(validation)},
                    dependencies=[review_id],
                    metadata={
                        "round": round_number,
                        "repair_attempt": reviewer_attempt,
                    },
                )
                if validation.valid:
                    break
                if repair_budget_exhausted(
                    reviewer_attempt, self.settings.max_component_repair_attempts
                ):
                    break
                reviewer_attempt += 1
                previous_review_id = review_id
                review = await self.reviewer.run(
                    {
                        **reviewer_payload,
                        "repair_only": {
                            "prior_output": review.model_dump(mode="json"),
                            "validation_issues": compact_validation(validation),
                            "python_quality_comparison": quality_delta.to_dict(),
                            "instruction": (
                                "Return a corrected complete review. Do not change "
                                "the paper or frozen research package."
                            ),
                        },
                    },
                    session_label=(
                        f"reviewer-isolated-round-{round_number}-"
                        f"repair-{reviewer_attempt}"
                    ),
                )
            if not validation.valid:
                last_result = PaperStageResult(
                    draft=draft,
                    review=review,
                    round_number=round_number,
                    accepted=False,
                    failure_reasons=[
                        "Reviewer failed its Harness repair budget",
                        *[
                            f"{issue.code}: {issue.message}"
                            for issue in validation.issues
                        ],
                    ],
                )
                self.store.checkpoint("paper", last_result)
                self.store.event(
                    "paper.failed_hard_gate",
                    {
                        "round": round_number,
                        "action": review.action,
                        "reasons": last_result.failure_reasons,
                    },
                )
                return last_result
            self.store.save(
                "review-quality",
                {"quality_delta": quality_delta.to_dict()},
                dependencies=[review_id],
                metadata={"round": round_number},
            )
            accepted = validation.valid and review.action == WorkflowAction.ACCEPT
            last_result = PaperStageResult(
                draft=draft,
                review=review,
                round_number=round_number,
                accepted=accepted,
                failure_reasons=(
                    []
                    if accepted
                    else [
                        f"Reviewer action: {review.action.value}",
                        review.rationale,
                    ]
                ),
            )
            self.store.checkpoint("paper", last_result)
            progress_record = self.progress.record(
                paper_progress_vector(draft, review),
                dependencies=[review_id],
                round_number=round_number,
                scope_id="|".join(
                    sorted(item.code_hash for item in experiment_stage.executions)
                ),
            )
            if accepted:
                self.store.event("paper.accepted", {"round": round_number})
                return last_result
            if self.progress.should_stop(progress_record):
                last_result = last_result.model_copy(
                    update={
                        "failure_reasons": [
                            *last_result.failure_reasons,
                            "NO_PROGRESS: Writer loop made no measurable forward progress",
                        ]
                    }
                )
                self.store.checkpoint("paper", last_result)
                self.store.event(
                    "paper.stalled",
                    {
                        "round": round_number,
                        "consecutive_stagnant_rounds": (
                            progress_record.consecutive_stagnant_rounds
                        ),
                    },
                )
                return last_result
            if review.action not in {WorkflowAction.RETURN_TO_WRITER}:
                return last_result
            self.store.invalidate(
                [draft_id],
                reason="Reviewer requested a Writer revision",
                cascade=True,
            )
            if review.contamination_status.value == "CONTAMINATED":
                self.store.event(
                    "session.replaced",
                    {"role": "writer", "round": round_number + 1},
                )
            prior_draft = draft.model_dump(mode="json")
            prior_review = review.model_dump(mode="json")
            prior_scores = review.criteria
        if last_result is None:
            raise RuntimeError("No paper draft was produced")
        if deadline_stop_reason is not None:
            last_result = last_result.model_copy(
                update={
                    "failure_reasons": [
                        *last_result.failure_reasons,
                        f"DEADLINE: {deadline_stop_reason}",
                    ]
                }
            )
            self.store.checkpoint("paper", last_result)
        self.store.event(
            "paper.failed_hard_gate",
            {
                "round": last_result.round_number,
                "action": last_result.review.action,
                "reasons": last_result.failure_reasons,
            },
        )
        return last_result
