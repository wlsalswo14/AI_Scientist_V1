from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from ..agents import ExEvaluatorAgent, ExperimentDesignerAgent, ExperimentorAgent
from ..artifacts import ArtifactStore
from ..config import Settings, repair_budget_exhausted
from ..evidence_audit import EvidenceAuditPipeline
from ..execution import LocalExperimentRunner, UnsafeExperimentError
from ..quality import compare_quality
from ..progress import ProgressTracker, experiment_progress_vector
from ..runtime import DeadlinePolicy, RuntimePhase
from ..schemas import (
    EvidenceAuditOutcome,
    ExecutionResult,
    ExEvaluatorReport,
    ExperimentStageResult,
    ExperimentorOutput,
    ResearchReadiness,
    ResearchStageResult,
    TracePreparationStageResult,
    TraceReviewerDecisionBatch,
    WorkflowAction,
)
from ..validation import (
    compact_validation,
    validate_ex_evaluator,
    validate_execution_bundle,
    validate_experiment_contract,
    validate_experimentor,
)


EX_EVALUATOR_CRITERIA = {
    "Protocol Compliance",
    "Execution Integrity",
    "Reproducibility",
    "Statistical Validity",
    "Result Traceability",
    "Failure Transparency",
    "Prediction-Test Alignment",
    "Target Discrimination",
    "Alternative-explanation Control",
}

EX_EVALUATOR_HARD_GATES = {
    "Protocol Compliance",
    "Execution Integrity",
    "Reproducibility",
    "Statistical Validity",
    "Result Traceability",
    "Failure Transparency",
    "Prediction-Test Alignment",
}


def _apply_evidence_audit_gate(
    evaluation: ExEvaluatorReport,
    evidence_audit: EvidenceAuditOutcome | None,
) -> ExEvaluatorReport:
    if evidence_audit is None or evidence_audit.paper_eligible:
        return evaluation
    evidence_reason = (
        "Evidence audit blocked scientific promotion; unresolved major="
        f"{evidence_audit.unresolved_major_ids}, fatal="
        f"{evidence_audit.unresolved_fatal_ids}."
    )
    return evaluation.model_copy(
        update={
            "action": WorkflowAction.RETURN_TO_HYPOTHESIS,
            "affected_hypothesis_ids": [],
            "failure_notebook": [],
            "rationale": f"{evidence_reason} {evaluation.rationale}",
        }
    )


class ExperimentWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        designer: ExperimentDesignerAgent,
        experimentor: ExperimentorAgent,
        evaluator: ExEvaluatorAgent,
        runner: LocalExperimentRunner,
        deadline_policy: DeadlinePolicy | None = None,
        evidence_audit_pipeline: EvidenceAuditPipeline | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.designer = designer
        self.experimentor = experimentor
        self.evaluator = evaluator
        self.runner = runner
        self.deadline_policy = deadline_policy
        self.evidence_audit_pipeline = evidence_audit_pipeline
        self.progress = ProgressTracker(
            store,
            "experiment",
            lower_is_better={
                "affected_targets",
                "protocol_violations",
                "contaminated_experimentors",
            },
            max_stagnant_rounds=settings.max_stagnant_rounds,
            state_store=(
                deadline_policy.state_store if deadline_policy is not None else None
            ),
        )

    async def run(
        self,
        research_stage: ResearchStageResult,
        *,
        upstream_feedback: list[str] | None = None,
        seed_checkpoint: ExperimentStageResult | None = None,
        initial_affected_target_ids: set[str] | None = None,
        trace_preparation: TracePreparationStageResult | None = None,
        trace_reviewer_decisions: TraceReviewerDecisionBatch | None = None,
    ) -> ExperimentStageResult:
        research_contract = research_stage.contract
        if research_contract.readiness == ResearchReadiness.PROPOSED:
            raise RuntimeError(
                "Experiment stage cannot run a PROPOSED Research Contract"
            )
        selected = research_contract.selected_target_ids
        if not selected:
            raise RuntimeError(
                "Experiment stage requires at least one ready research target"
            )
        selected = list(dict.fromkeys(selected))[
            : self.settings.target_promoted_hypotheses
        ]
        targets = [
            item.model_dump(mode="json")
            for item in research_contract.targets
            if item.target_id in selected
        ]
        designer_payload = {
            "question": research_contract.original_question,
            "research_mode": research_contract.research_mode,
            "research_readiness": research_contract.readiness,
            "claim_ceiling": research_contract.claim_ceiling,
            "research_profile": research_contract.research_profile,
            "trace_study_contract": (
                research_contract.trace_study_contract.model_dump(mode="json")
                if research_contract.trace_study_contract is not None
                else None
            ),
            "selected_target_ids": selected,
            "research_targets": targets,
            "prediction_matrix": [
                item.model_dump(mode="json")
                for item in research_contract.prediction_matrix
            ],
            "upstream_feedback": upstream_feedback or [],
            "pipeline_smoke_test": self.settings.pipeline_smoke_test,
            "trace_preparation": (
                trace_preparation.model_dump(mode="json")
                if trace_preparation is not None
                else None
            ),
            "trace_reviewer_decisions": (
                self._trace_reviewer_input_manifest(trace_reviewer_decisions)
                if trace_reviewer_decisions is not None
                else None
            ),
        }
        research_dependencies = self.store.find_artifact_ids(
            kind="research-contract-final"
        )[-1:]
        if trace_preparation is not None:
            research_dependencies += self.store.find_artifact_ids(
                kind="trace-benchmark-plan"
            )[-1:] + self.store.find_artifact_ids(
                kind="trace-corruption-plan"
            )[-1:]
        if trace_reviewer_decisions is not None:
            research_dependencies += self.store.find_artifact_ids(
                kind="trace-reviewer-decision-batch"
            )[-1:]
        resume_checkpoint = seed_checkpoint or (
            self.store.load_checkpoint("experiment", ExperimentStageResult)
            if not upstream_feedback
            else None
        )
        resume_contract = self.store.latest_envelope("experiment-contract")
        can_resume = (
            resume_checkpoint is not None
            and (not resume_checkpoint.passed or seed_checkpoint is not None)
            and set(resume_checkpoint.contract.hypothesis_ids) == set(selected)
            and resume_contract is not None
        )
        if can_resume and (
            resume_checkpoint.evaluation.action == WorkflowAction.RETURN_TO_HYPOTHESIS
            or any(
                reason.startswith(("DEADLINE:", "NO_PROGRESS:"))
                for reason in resume_checkpoint.failure_reasons
            )
        ):
            return resume_checkpoint

        start_round = 1
        if can_resume:
            contract = resume_checkpoint.contract
            contract_id = resume_contract["artifact_id"]
            if seed_checkpoint is not None and upstream_feedback:
                start_round = 1
                requested = set(initial_affected_target_ids or set())
                self.store.event(
                    "experiment.targeted_upstream_repair",
                    {
                        "target_ids": sorted(requested),
                        "preserved_target_ids": sorted(set(selected) - requested),
                    },
                )
            else:
                start_round = resume_checkpoint.round_number + 1
                self.store.event(
                    "experiment.resumed_from_checkpoint",
                    {"next_round": start_round},
                )
        else:
            designer_attempt = 0
            previous_contract_id: str | None = None
            contract = await self.designer.run(
                designer_payload,
                session_label="experiment-designer-frozen-contract",
            )
            while True:
                contract_id = self.store.save(
                    "experiment-contract",
                    contract,
                    dependencies=research_dependencies
                    + ([previous_contract_id] if previous_contract_id else []),
                    metadata={
                        "frozen": True,
                        "repair_attempt": designer_attempt,
                    },
                )
                contract_validation = validate_experiment_contract(
                    contract,
                    selected_target_ids=set(selected),
                    expected_trace_study_contract=(
                        research_contract.trace_study_contract
                    ),
                )
                self.store.save(
                    "experiment-contract-validation",
                    compact_validation(contract_validation),
                    dependencies=[contract_id],
                    metadata={"repair_attempt": designer_attempt},
                )
                if contract_validation.valid:
                    break
                if repair_budget_exhausted(
                    designer_attempt, self.settings.max_component_repair_attempts
                ):
                    raise RuntimeError(
                        "Experiment Designer failed its Harness repair budget"
                    )
                designer_attempt += 1
                previous_contract_id = contract_id
                contract = await self.designer.run(
                    {
                        **designer_payload,
                        "repair_only": {
                            "prior_output": contract.model_dump(mode="json"),
                            "validation_issues": compact_validation(
                                contract_validation
                            ),
                            "instruction": (
                                "Return a corrected complete Experiment Contract. "
                                "Keep the Research Contract and selected target IDs "
                                "frozen."
                            ),
                        },
                    },
                    session_label=(
                        f"experiment-designer-contract-repair-{designer_attempt}"
                    ),
                )
        current_outputs: dict[str, ExperimentorOutput] = (
            {
                item.hypothesis_id: item
                for item in resume_checkpoint.experimentor_outputs
            }
            if can_resume
            else {}
        )
        current_executions: dict[str, ExecutionResult] = (
            {item.hypothesis_id: item for item in resume_checkpoint.executions}
            if can_resume
            else {}
        )
        current_output_artifacts: dict[str, str] = {}
        current_execution_artifacts: dict[str, str] = {}
        for artifact_id in self.store.find_artifact_ids(kind="experimentor-output"):
            envelope = self.store.artifact_envelope(artifact_id)
            payload = envelope.get("payload", {}) if envelope else {}
            target_id = payload.get("hypothesis_id")
            if target_id in current_outputs and payload.get("experiment_id") == (
                current_outputs[target_id].experiment_id
            ):
                current_output_artifacts[target_id] = artifact_id
        for artifact_id in self.store.find_artifact_ids(kind="execution-result"):
            envelope = self.store.artifact_envelope(artifact_id)
            payload = envelope.get("payload", {}) if envelope else {}
            target_id = payload.get("hypothesis_id")
            if target_id in current_executions and (
                payload.get("experiment_id"),
                payload.get("code_hash"),
            ) == (
                current_executions[target_id].experiment_id,
                current_executions[target_id].code_hash,
            ):
                current_execution_artifacts[target_id] = artifact_id
        prior_evaluation: dict[str, Any] | None = (
            resume_checkpoint.evaluation.model_dump(mode="json")
            if can_resume
            else None
        )
        prior_scores = resume_checkpoint.evaluation.criteria if can_resume else None
        last_result: ExperimentStageResult | None = (
            resume_checkpoint if can_resume else None
        )
        affected = self._initial_affected_targets(
            selected=set(selected),
            resume_checkpoint=resume_checkpoint if can_resume else None,
            requested=initial_affected_target_ids,
        )
        deadline_stop_reason: str | None = None
        for round_number in range(
            start_round,
            self.settings.max_experiment_rounds + 1,
        ):
            if self.deadline_policy is not None:
                deadline = self.deadline_policy.can_start(RuntimePhase.EXPERIMENT)
                if not deadline.allowed:
                    deadline_stop_reason = deadline.reason
                    self.store.event(
                        "deadline.round_blocked",
                        {
                            "stage": "experiment",
                            "round": round_number,
                            "reason": deadline.reason,
                            "remaining_seconds": deadline.remaining_seconds,
                        },
                    )
                    break
            for target_id in affected:
                stale_roots = [
                    artifact_id
                    for artifact_id in (
                        current_output_artifacts.pop(target_id, None),
                        current_execution_artifacts.pop(target_id, None),
                    )
                    if artifact_id is not None
                ]
                if stale_roots:
                    self.store.invalidate(
                        stale_roots,
                        reason=(
                            f"Experiment target {target_id} was selected for rerun"
                        ),
                        cascade=True,
                    )
                current_outputs.pop(target_id, None)
                current_executions.pop(target_id, None)
            tasks = []
            for target_id in self._targets_to_generate(
                selected=selected,
                affected=affected,
                current_output_ids=set(current_outputs),
            ):
                target = next(
                    item for item in targets if item["target_id"] == target_id
                )
                tasks.append(
                    self.experimentor.run(
                        {
                            "research_mode": research_contract.research_mode,
                            "target_id": target_id,
                            "research_target": target,
                            "frozen_contract": contract.model_dump(mode="json"),
                            "previous_evaluation": prior_evaluation,
                            "upstream_feedback": upstream_feedback or [],
                            "round": round_number,
                            "pipeline_smoke_test": self.settings.pipeline_smoke_test,
                            "trace_preparation": (
                                trace_preparation.model_dump(mode="json")
                                if trace_preparation is not None
                                else None
                            ),
                            "trace_reviewer_decisions": (
                                self._trace_reviewer_input_manifest(
                                    trace_reviewer_decisions
                                )
                                if trace_reviewer_decisions is not None
                                else None
                            ),
                        },
                        session_label=(
                            f"experimentor-{target_id}-isolated-round-{round_number}"
                        ),
                    )
                )
            generated = await asyncio.gather(*tasks) if tasks else []
            for item in generated:
                validation = validate_experimentor(
                    item,
                    selected_target_ids=set(selected),
                )
                item_id = self.store.save(
                    "experimentor-output",
                    item,
                    dependencies=[contract_id],
                    metadata={"round": round_number, "isolated": True},
                )
                self.store.save(
                    "experimentor-validation",
                    compact_validation(validation),
                    dependencies=[item_id],
                )
                if not validation.valid:
                    continue
                current_outputs[item.hypothesis_id] = item
                current_output_artifacts[item.hypothesis_id] = item_id
                try:
                    execution = self.runner.run(
                        item,
                        round_number=round_number,
                        input_files=(
                            {
                                "reviewer-decisions.json": (
                                    trace_reviewer_decisions.model_dump_json(indent=2)
                                )
                            }
                            if trace_reviewer_decisions is not None
                            else None
                        ),
                    )
                except UnsafeExperimentError as exc:
                    execution = ExecutionResult(
                        hypothesis_id=item.hypothesis_id,
                        experiment_id=item.experiment_id,
                        exit_code=126,
                        stdout="",
                        stderr=str(exc),
                        output_files={},
                        result_ids=[],
                        code_hash="unsafe",
                        workspace="",
                    )
                current_executions[item.hypothesis_id] = execution
                execution_id = self.store.save(
                    "execution-result",
                    execution,
                    dependencies=[item_id, contract_id],
                    metadata={"round": round_number},
                )
                current_execution_artifacts[item.hypothesis_id] = execution_id
            execution_validation = validate_execution_bundle(
                selected_target_ids=set(selected),
                experimentor_outputs=list(current_outputs.values()),
                executions=list(current_executions.values()),
                expected_trace_study_contract=(
                    research_contract.trace_study_contract
                ),
                expected_trace_reviewer_decisions=trace_reviewer_decisions,
                pipeline_smoke_test=self.settings.pipeline_smoke_test,
            )
            self.store.save(
                "execution-bundle-validation",
                compact_validation(execution_validation),
                dependencies=[
                    contract_id,
                    *current_output_artifacts.values(),
                    *current_execution_artifacts.values(),
                ],
                metadata={"round": round_number},
            )
            evidence_audit = None
            evidence_audit_id: str | None = None
            if self.evidence_audit_pipeline is not None:
                evidence_audit = await self.evidence_audit_pipeline.run(
                    research_contract=research_contract,
                    experiment_contract=contract,
                    experimentor_outputs=list(current_outputs.values()),
                    executions=list(current_executions.values()),
                    trace_preparation=trace_preparation,
                    trace_reviewer_decisions=trace_reviewer_decisions,
                    round_number=round_number,
                )
                manifest_id = self.store.save(
                    "evidence-audit-manifest",
                    evidence_audit.manifest,
                    dependencies=[
                        contract_id,
                        *current_output_artifacts.values(),
                        *current_execution_artifacts.values(),
                    ],
                    metadata={"round": round_number},
                )
                critic_ids = [
                    self.store.save(
                        "evidence-critic-report",
                        report,
                        dependencies=[manifest_id],
                        metadata={
                            "round": round_number,
                            "critic_lens": report.critic_lens,
                            "isolated": True,
                        },
                    )
                    for report in evidence_audit.critic_reports
                ]
                resolution_ids = [
                    self.store.save(
                        "evidence-concern-resolution",
                        resolution,
                        dependencies=[manifest_id, *critic_ids],
                        metadata={
                            "round": round_number,
                            "concern_id": resolution.concern_id,
                            "isolated": True,
                        },
                    )
                    for resolution in evidence_audit.resolutions
                ]
                global_id = self.store.save(
                    "evidence-global-audit",
                    evidence_audit.global_audit,
                    dependencies=resolution_ids or [manifest_id],
                    metadata={"round": round_number, "isolated": True},
                )
                evidence_audit_id = self.store.save(
                    "evidence-audit-outcome",
                    evidence_audit,
                    dependencies=[global_id],
                    metadata={
                        "round": round_number,
                        "paper_eligible": evidence_audit.paper_eligible,
                    },
                )
                self.store.event(
                    "experiment.evidence_audit_completed",
                    {
                        "round": round_number,
                        "concerns": len(evidence_audit.concerns),
                        "unresolved_major_ids": evidence_audit.unresolved_major_ids,
                        "unresolved_fatal_ids": evidence_audit.unresolved_fatal_ids,
                        "paper_eligible": evidence_audit.paper_eligible,
                    },
                )
            evaluation_payload = {
                "question": research_contract.original_question,
                "research_contract": research_contract.model_dump(mode="json"),
                "frozen_contract": contract.model_dump(mode="json"),
                "research_targets": targets,
                "experimentor_outputs": [
                    item.model_dump(mode="json") for item in current_outputs.values()
                ],
                "execution_results": [
                    item.model_dump(mode="json")
                    for item in current_executions.values()
                ],
                "python_execution_validation": compact_validation(
                    execution_validation
                ),
                "previous_evaluation": prior_evaluation,
                "pipeline_smoke_test": self.settings.pipeline_smoke_test,
                "research_profile": research_contract.research_profile,
                "trace_study_contract": (
                    research_contract.trace_study_contract.model_dump(mode="json")
                    if research_contract.trace_study_contract is not None
                    else None
                ),
                "trace_preparation": (
                    trace_preparation.model_dump(mode="json")
                    if trace_preparation is not None
                    else None
                ),
                "trace_reviewer_decisions": (
                    trace_reviewer_decisions.model_dump(mode="json")
                    if trace_reviewer_decisions is not None
                    else None
                ),
                "thresholds": {
                    "minimum_passing_score": self.settings.minimum_gate_score,
                    "score_drop_absolute": self.settings.score_drop_absolute,
                    "score_drop_relative": self.settings.score_drop_relative,
                    "critical_dimension_drop": self.settings.critical_dimension_drop,
                },
                "evidence_audit": (
                    evidence_audit.model_dump(mode="json")
                    if evidence_audit is not None
                    else None
                ),
            }
            evaluation = await self.evaluator.run(
                evaluation_payload,
                session_label=f"ex-evaluator-round-{round_number}",
            )
            evaluation = _apply_evidence_audit_gate(evaluation, evidence_audit)
            evaluator_attempt = 0
            previous_evaluation_id: str | None = None
            while True:
                evaluation_id = self.store.save(
                    "ex-evaluator-report",
                    evaluation,
                    dependencies=[
                        contract_id,
                        *current_execution_artifacts.values(),
                    ]
                    + ([evidence_audit_id] if evidence_audit_id else [])
                    + ([previous_evaluation_id] if previous_evaluation_id else []),
                    metadata={
                        "round": round_number,
                        "repair_attempt": evaluator_attempt,
                    },
                )
                quality_delta = compare_quality(
                    evaluation.criteria,
                    prior_scores,
                    critical_dimensions={
                        "Protocol Compliance",
                        "Execution Integrity",
                        "Reproducibility",
                        "Result Traceability",
                    },
                    settings=self.settings,
                )
                validation = validate_ex_evaluator(
                    evaluation,
                    required_criteria=EX_EVALUATOR_CRITERIA,
                    hard_gate_criteria=EX_EVALUATOR_HARD_GATES,
                    selected_target_ids=set(selected),
                    experimentor_outputs=list(current_outputs.values()),
                    executions=list(current_executions.values()),
                    rubric_version=self.settings.rubric_version,
                    minimum_passing_score=self.settings.minimum_gate_score,
                    significant_degradation=(
                        quality_delta.significant_degradation
                    ),
                    expected_trace_study_contract=(
                        research_contract.trace_study_contract
                    ),
                    pipeline_smoke_test=self.settings.pipeline_smoke_test,
                )
                self.store.save(
                    "ex-evaluator-validation",
                    {"issues": compact_validation(validation)},
                    dependencies=[evaluation_id],
                    metadata={
                        "round": round_number,
                        "repair_attempt": evaluator_attempt,
                    },
                )
                if validation.valid:
                    break
                if repair_budget_exhausted(
                    evaluator_attempt, self.settings.max_component_repair_attempts
                ):
                    raise RuntimeError(
                        "Ex-Evaluator failed its Harness repair budget"
                    )
                evaluator_attempt += 1
                previous_evaluation_id = evaluation_id
                evaluation = await self.evaluator.run(
                    {
                        **evaluation_payload,
                        "repair_only": {
                            "prior_output": evaluation.model_dump(mode="json"),
                            "validation_issues": compact_validation(validation),
                            "python_quality_comparison": quality_delta.to_dict(),
                            "instruction": (
                                "Return a corrected complete Ex-Evaluator report. "
                                "Do not alter or reinterpret the frozen executions."
                            ),
                        },
                    },
                    session_label=(
                        f"ex-evaluator-round-{round_number}-"
                        f"repair-{evaluator_attempt}"
                    ),
                )
                evaluation = _apply_evidence_audit_gate(evaluation, evidence_audit)
            self.store.save(
                "ex-evaluator-quality",
                {"quality_delta": quality_delta.to_dict()},
                dependencies=[evaluation_id],
                metadata={"round": round_number},
            )
            passed = validation.valid and evaluation.action == WorkflowAction.PASS
            last_result = ExperimentStageResult(
                contract=contract,
                experimentor_outputs=list(current_outputs.values()),
                executions=list(current_executions.values()),
                evaluation=evaluation,
                trace_preparation=trace_preparation,
                trace_reviewer_decisions=trace_reviewer_decisions,
                evidence_audit=evidence_audit,
                round_number=round_number,
                passed=passed,
                failure_reasons=(
                    []
                    if passed
                    else [
                        f"Ex-Evaluator action: {evaluation.action.value}",
                        evaluation.rationale,
                    ]
                ),
            )
            self.store.checkpoint("experiment", last_result)
            progress_record = self.progress.record(
                experiment_progress_vector(
                    list(current_executions.values()),
                    evaluation,
                ),
                dependencies=[evaluation_id],
                round_number=round_number,
                scope_id=contract_id,
            )
            if passed:
                self.store.event(
                    "experiment.passed",
                    {
                        "round": round_number,
                        "best_supported": evaluation.best_supported_hypothesis_id,
                    },
                )
                return last_result
            if self.progress.should_stop(progress_record):
                last_result = last_result.model_copy(
                    update={
                        "failure_reasons": [
                            *last_result.failure_reasons,
                            "NO_PROGRESS: experiment loop made no measurable forward progress",
                        ]
                    }
                )
                self.store.checkpoint("experiment", last_result)
                self.store.event(
                    "experiment.stalled",
                    {
                        "round": round_number,
                        "consecutive_stagnant_rounds": (
                            progress_record.consecutive_stagnant_rounds
                        ),
                    },
                )
                return last_result
            if evaluation.action == WorkflowAction.RETURN_TO_HYPOTHESIS:
                return last_result
            prior_evaluation = evaluation.model_dump(mode="json")
            prior_scores = evaluation.criteria
            affected = set(evaluation.affected_hypothesis_ids)
            stale_roots = [
                artifact_id
                for target_id in affected
                for artifact_id in (
                    current_output_artifacts.get(target_id),
                    current_execution_artifacts.get(target_id),
                )
                if artifact_id is not None
            ]
            if stale_roots:
                self.store.invalidate(
                    stale_roots,
                    reason="Ex-Evaluator required target-level experiment repair",
                    cascade=True,
                )
            for contamination in evaluation.contamination_by_experimentor:
                if contamination.status.value == "CONTAMINATED":
                    affected.add(contamination.hypothesis_id)
                    self.store.event(
                        "session.replaced",
                        {
                            "role": f"experimentor:{contamination.hypothesis_id}",
                            "round": round_number + 1,
                        },
                    )
        if last_result is None:
            raise RuntimeError("No experiment result was produced")
        if deadline_stop_reason is not None:
            last_result = last_result.model_copy(
                update={
                    "failure_reasons": [
                        *last_result.failure_reasons,
                        f"DEADLINE: {deadline_stop_reason}",
                    ]
                }
            )
            self.store.checkpoint("experiment", last_result)
        self.store.event(
            "experiment.failed_hard_gate",
            {
                "round": last_result.round_number,
                "action": last_result.evaluation.action,
                "reasons": last_result.failure_reasons,
            },
        )
        return last_result

    @staticmethod
    def _initial_affected_targets(
        *,
        selected: set[str],
        resume_checkpoint: ExperimentStageResult | None,
        requested: set[str] | None,
    ) -> set[str]:
        if requested is not None:
            affected = set(requested) & selected
            if not affected:
                raise ValueError(
                    "A targeted experiment repair must identify a selected target"
                )
            return affected
        if resume_checkpoint is not None:
            return set(
                resume_checkpoint.evaluation.affected_hypothesis_ids or selected
            )
        return set(selected)

    @staticmethod
    def _trace_reviewer_input_manifest(
        value: TraceReviewerDecisionBatch,
    ) -> dict[str, Any]:
        serialized = value.model_dump_json(indent=2)
        return {
            "batch_version": value.batch_version,
            "trace_contract_fingerprint": value.trace_contract_fingerprint,
            "reviewer_models": value.reviewer_models,
            "corruption_manifest_hash": value.corruption_manifest_hash,
            "leakage_check_passed": value.leakage_check_passed,
            "measurement_notes": value.measurement_notes,
            "decision_count": len(value.decisions),
            "workspace_input_file": "reviewer-decisions.json",
            "workspace_input_bytes_preserved": True,
            "workspace_input_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _targets_to_generate(
        *,
        selected: list[str],
        affected: set[str],
        current_output_ids: set[str],
    ) -> list[str]:
        return [
            target_id
            for target_id in selected
            if target_id in affected or target_id not in current_output_ids
        ]
