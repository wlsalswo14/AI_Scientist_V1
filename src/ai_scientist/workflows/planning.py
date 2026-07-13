from __future__ import annotations

import asyncio
from typing import Any

from ..agents import (
    ContractComposerAgent,
    ContractEvaluatorAAgent,
    ContractEvaluatorBAgent,
    DirectStudyDirectorAgent,
    ResearchModeDirectorAgent,
)
from ..artifacts import ArtifactStore
from ..config import Settings, hypothesis_rounds, repair_budget_exhausted
from ..quality import compare_quality
from ..progress import ProgressTracker, contract_progress_vector
from ..runtime import DeadlinePolicy, RuntimePhase
from ..schemas import (
    ContractComposerReport,
    EvaluatorReport,
    HypothesisStageResult,
    ResearchCondition,
    ResearchContract,
    ResearchMode,
    ResearchModeAssessment,
    ResearchProfile,
    ResearchDepth,
    ResearchPredictionCell,
    ResearchReadiness,
    ResearchStageResult,
    ResearchTarget,
    ResearchTargetType,
    WorkflowAction,
)
from ..validation import (
    compact_validation,
    validate_contract_composer,
    validate_evaluator,
    validate_research_contract,
)
from .errors import ResearchModeReclassificationError


CONTRACT_EVALUATOR_A_CRITERIA = {
    "Mode Fit",
    "Literature Coverage",
    "Evidence Entailment",
    "Citation Accuracy",
    "Nearest-work Coverage",
    "Comparison Precedent",
    "Contradictory Evidence Coverage",
    "Evidence Quality",
}

CONTRACT_EVALUATOR_B_CRITERIA = {
    "Mode Fit",
    "Claim Testability",
    "Comparison Fairness",
    "Metric Validity",
    "Confound Control",
    "Statistical Adequacy",
    "Informative Outcomes",
    "Feasibility",
}

CONTRACT_EVALUATOR_A_HARD_GATES = {
    "Mode Fit",
    "Evidence Entailment",
    "Citation Accuracy",
    "Nearest-work Coverage",
    "Evidence Quality",
}

CONTRACT_EVALUATOR_B_HARD_GATES = set(CONTRACT_EVALUATOR_B_CRITERIA)

CONTRACT_EVALUATOR_A_TARGET_GATES = {
    "Evidence Support",
    "Literature Differentiation",
}

CONTRACT_EVALUATOR_B_TARGET_GATES = {
    "Claim Testability",
    "Comparison Fairness",
    "Decision Rule",
    "Feasibility",
}

CONTRACT_EVALUATOR_A_EVIDENCE_GATES = (
    CONTRACT_EVALUATOR_A_HARD_GATES - {"Mode Fit"}
)
CONTRACT_EVALUATOR_A_EVIDENCE_TARGET_GATES = set(
    CONTRACT_EVALUATOR_A_TARGET_GATES
)


class ResearchPlanningError(RuntimeError):
    pass


class ResearchModeWorkflow:
    def __init__(
        self,
        store: ArtifactStore,
        director: ResearchModeDirectorAgent,
    ) -> None:
        self.store = store
        self.director = director

    async def run(
        self,
        question: str,
        *,
        research_objective: str = "",
        research_depth: ResearchDepth = ResearchDepth.THESIS,
        research_profile: ResearchProfile = ResearchProfile.GENERAL,
        prior_assessment: ResearchModeAssessment | None = None,
        reclassification_feedback: list[str] | None = None,
    ) -> ResearchModeAssessment:
        assessment = await self.director.run(
            {
                "question": question,
                "research_objective": research_objective or question,
                "research_depth": research_depth,
                "research_profile": research_profile,
                "prior_assessment": (
                    prior_assessment.model_dump(mode="json")
                    if prior_assessment
                    else None
                ),
                "reclassification_feedback": reclassification_feedback or [],
            },
            session_label="director-research-mode",
        )
        assessment = assessment.model_copy(
            update={
                "original_question": question,
                "research_depth": research_depth,
                "research_profile": research_profile,
                "surface_mode": assessment.surface_mode or assessment.proposed_mode,
            }
        )
        artifact_id = self.store.save("research-mode-assessment", assessment)
        self.store.checkpoint("research-mode", assessment)
        self.store.event(
            "research_mode.selected",
            {
                "artifact_id": artifact_id,
                "mode": assessment.proposed_mode,
                "confidence": assessment.confidence,
            },
        )
        return assessment


class DirectResearchWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        director: DirectStudyDirectorAgent,
        evaluator_a: ContractEvaluatorAAgent,
        evaluator_b: ContractEvaluatorBAgent,
        composer: ContractComposerAgent,
        deadline_policy: DeadlinePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.director = director
        self.evaluator_a = evaluator_a
        self.evaluator_b = evaluator_b
        self.composer = composer
        self.deadline_policy = deadline_policy
        self.progress = ProgressTracker(
            store,
            "research-planning",
            lower_is_better={"fatal_issues", "failed_gates", "evidence_failures"},
            max_stagnant_rounds=settings.max_stagnant_rounds,
            state_store=(
                deadline_policy.state_store if deadline_policy is not None else None
            ),
        )

    async def run(
        self,
        assessment: ResearchModeAssessment,
        *,
        upstream_feedback: list[str] | None = None,
        seed_contract: ResearchContract | None = None,
        initial_locked_target_ids: set[str] | None = None,
    ) -> ResearchStageResult:
        if assessment.proposed_mode == ResearchMode.EXPLANATORY_RESEARCH:
            raise ResearchPlanningError(
                "DirectResearchWorkflow cannot run explanatory research"
            )
        prior_contract: dict[str, Any] | None = (
            seed_contract.model_dump(mode="json") if seed_contract else None
        )
        prior_contract_model: ResearchContract | None = seed_contract
        prior_composer: dict[str, Any] | None = None
        prior_scores = None
        locked_target_ids: set[str] = set(initial_locked_target_ids or set())
        last_contract: ResearchContract | None = None
        last_evaluator_a: EvaluatorReport | None = None
        last_round_number = 0
        for round_number in hypothesis_rounds(
            self.settings.max_hypothesis_rounds
        ):
            if self.deadline_policy is not None:
                deadline = self.deadline_policy.can_start(
                    RuntimePhase.RESEARCH_PLANNING
                )
                if not deadline.allowed:
                    self.store.event(
                        "deadline.round_blocked",
                        {
                            "stage": "research-planning",
                            "round": round_number,
                            "reason": deadline.reason,
                            "remaining_seconds": deadline.remaining_seconds,
                        },
                    )
                    break
            director_payload = {
                "question": assessment.original_question,
                "mode_assessment": assessment.model_dump(mode="json"),
                "round": round_number,
                "upstream_feedback": upstream_feedback or [],
                "prior_contract": prior_contract,
                "failure_notebook": (
                    prior_composer.get("failure_notebook", [])
                    if prior_composer
                    else []
                ),
                "locked_targets": (
                    [
                        item.model_dump(mode="json")
                        for item in prior_contract_model.targets
                        if item.target_id in locked_target_ids
                    ]
                    if prior_contract_model
                    else []
                ),
                "revision_target_ids": (
                    [
                        item.target_id
                        for item in prior_contract_model.targets
                        if item.target_id not in locked_target_ids
                    ]
                    if prior_contract_model
                    else []
                ),
            }
            contract = await self.director.run(
                director_payload,
                session_label=f"direct-study-director-round-{round_number}",
            )
            contract = contract.model_copy(
                update={
                    "original_question": assessment.original_question,
                    "claim_ceiling": assessment.claim_ceiling,
                }
            )
            if prior_contract_model is not None and locked_target_ids:
                contract = _preserve_locked_targets(
                    prior_contract_model,
                    contract,
                    locked_target_ids,
                )
            contract_id = self.store.save(
                "research-contract-draft",
                contract,
                metadata={"round": round_number, "mode": assessment.proposed_mode},
            )
            contract_validation = validate_research_contract(
                contract,
                expected_mode=assessment.proposed_mode,
                require_proposed=True,
            )
            self.store.save(
                "research-contract-validation",
                {
                    "valid": contract_validation.valid,
                    "issues": compact_validation(contract_validation),
                },
                dependencies=[contract_id],
                metadata={"round": round_number},
            )
            if not contract_validation.valid:
                prior_contract = contract.model_dump(mode="json")
                prior_contract_model = contract
                prior_composer = {
                    "failure_notebook": [],
                    "validation_repairs": compact_validation(contract_validation),
                }
                continue

            common_payload = {
                "question": assessment.original_question,
                "mode_assessment": assessment.model_dump(mode="json"),
                "research_contract": contract.model_dump(mode="json"),
                "rubric_version": self.settings.rubric_version,
                "minimum_passing_score": self.settings.minimum_gate_score,
            }
            evaluator_a_output, evaluator_b_output = await asyncio.gather(
                self.evaluator_a.run(
                    common_payload,
                    session_label=f"contract-evaluator-a-isolated-round-{round_number}",
                ),
                self.evaluator_b.run(
                    common_payload,
                    session_label=f"contract-evaluator-b-isolated-round-{round_number}",
                ),
            )
            evaluator_a_output, evaluator_a_id = await self._repair_evaluator(
                role="a",
                output=evaluator_a_output,
                agent=self.evaluator_a,
                common_payload=common_payload,
                required_criteria=CONTRACT_EVALUATOR_A_CRITERIA,
                required_target_gates=CONTRACT_EVALUATOR_A_TARGET_GATES,
                target_ids={item.target_id for item in contract.targets},
                evidence_ids={item.evidence_id for item in contract.evidence},
                contract_id=contract_id,
                round_number=round_number,
            )
            evaluator_b_output, evaluator_b_id = await self._repair_evaluator(
                role="b",
                output=evaluator_b_output,
                agent=self.evaluator_b,
                common_payload=common_payload,
                required_criteria=CONTRACT_EVALUATOR_B_CRITERIA,
                required_target_gates=CONTRACT_EVALUATOR_B_TARGET_GATES,
                target_ids={item.target_id for item in contract.targets},
                evidence_ids={item.evidence_id for item in contract.evidence},
                contract_id=contract_id,
                round_number=round_number,
            )

            quality_delta = compare_quality(
                evaluator_a_output.criteria + evaluator_b_output.criteria,
                prior_scores,
                critical_dimensions={
                    "Mode Fit",
                    "Comparison Fairness",
                    "Claim Testability",
                    "Evidence Quality",
                },
                settings=self.settings,
            )
            composer_payload = {
                "mode_assessment": assessment.model_dump(mode="json"),
                "research_contract": contract.model_dump(mode="json"),
                "evaluator_a": evaluator_a_output.model_dump(mode="json"),
                "evaluator_b": evaluator_b_output.model_dump(mode="json"),
                "previous_composer": prior_composer,
                "previous_contract": prior_contract,
                "locked_target_ids": sorted(locked_target_ids),
                "python_quality_comparison": quality_delta.to_dict(),
                "minimum_passing_score": self.settings.minimum_gate_score,
            }
            composer_output = await self.composer.run(
                composer_payload,
                session_label=f"contract-composer-round-{round_number}",
            )
            composer_output, composer_id = await self._repair_composer(
                output=composer_output,
                payload=composer_payload,
                reports=[evaluator_a_output, evaluator_b_output],
                target_ids={item.target_id for item in contract.targets},
                dependencies=[contract_id, evaluator_a_id, evaluator_b_id],
                round_number=round_number,
            )
            if composer_output.action == WorkflowAction.RECLASSIFY_MODE:
                self.store.event(
                    "research_mode.reclassification_requested",
                    {"round": round_number, "rationale": composer_output.rationale},
                )
                raise ResearchModeReclassificationError(composer_output.rationale)
            valid_target_ids = {item.target_id for item in contract.targets}
            newly_promoted = {
                item
                for item in composer_output.promoted_target_ids
                if item in valid_target_ids and item not in locked_target_ids
            }
            locked_target_ids.update(newly_promoted)
            progress_record = self.progress.record(
                contract_progress_vector(
                    contract,
                    evaluator_a_output,
                    evaluator_b_output,
                    composer_output,
                ),
                dependencies=[composer_id],
                round_number=round_number,
                scope_id=self.store.content_hash(
                    assessment.original_question
                    + "\n"
                    + "\n".join(upstream_feedback or [])
                ),
            )
            last_evaluator_a = evaluator_a_output
            last_round_number = round_number
            if newly_promoted:
                self.store.event(
                    "research_target.partially_promoted",
                    {
                        "round": round_number,
                        "new_ids": sorted(newly_promoted),
                        "locked_ids": sorted(locked_target_ids),
                    },
                )
            last_contract = contract
            if composer_output.action == WorkflowAction.PROMOTE and locked_target_ids:
                return self._finalize(
                    assessment,
                    contract,
                    locked_target_ids,
                    evaluator_a_output,
                    round_number,
                    dependencies=[composer_id],
                )
            if self.progress.should_stop(progress_record) and not newly_promoted:
                self.store.event(
                    "research_planning.stalled",
                    {
                        "round": round_number,
                        "consecutive_stagnant_rounds": (
                            progress_record.consecutive_stagnant_rounds
                        ),
                    },
                )
                break
            prior_contract = contract.model_dump(mode="json")
            prior_contract_model = contract
            prior_composer = composer_output.model_dump(mode="json")
            prior_scores = evaluator_a_output.criteria + evaluator_b_output.criteria
            if composer_output.contamination_status.value == "CONTAMINATED":
                self.store.event(
                    "session.replaced",
                    {"role": "direct_study_director", "round": round_number + 1},
                )

        if last_contract is not None and locked_target_ids:
            return self._finalize(
                assessment,
                last_contract,
                locked_target_ids,
                last_evaluator_a,
                last_round_number,
                dependencies=[],
            )
        raise ResearchPlanningError(
            "The direct-study planning budget ended without a TEST_READY or "
            "AUDIT_READY target"
        )

    async def _repair_evaluator(
        self,
        *,
        role: str,
        output: EvaluatorReport,
        agent,
        common_payload: dict[str, Any],
        required_criteria: set[str],
        required_target_gates: set[str],
        target_ids: set[str],
        evidence_ids: set[str],
        contract_id: str,
        round_number: int,
    ) -> tuple[EvaluatorReport, str]:
        attempt = 0
        previous_id: str | None = None
        while True:
            artifact_id = self.store.save(
                f"contract-evaluator-{role}-report",
                output,
                dependencies=[contract_id] + ([previous_id] if previous_id else []),
                metadata={
                    "round": round_number,
                    "isolated": True,
                    "repair_attempt": attempt,
                },
            )
            validation = validate_evaluator(
                output,
                required_criteria=required_criteria,
                evidence_ids=evidence_ids,
                rubric_version=self.settings.rubric_version,
                target_ids=target_ids,
                required_target_gates=required_target_gates,
                minimum_passing_score=self.settings.minimum_gate_score,
            )
            self.store.save(
                f"contract-evaluator-{role}-validation",
                compact_validation(validation),
                dependencies=[artifact_id],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            if validation.valid:
                return output, artifact_id
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                raise ResearchPlanningError(
                    f"Contract Evaluator {role.upper()} failed its repair budget"
                )
            attempt += 1
            previous_id = artifact_id
            output = await agent.run(
                {
                    **common_payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": compact_validation(validation),
                        "instruction": (
                            "Return a complete corrected report and change only "
                            "the invalid fields. The contract is frozen."
                        ),
                    },
                },
                session_label=(
                    f"contract-evaluator-{role}-isolated-round-{round_number}-"
                    f"repair-{attempt}"
                ),
            )

    async def _repair_composer(
        self,
        *,
        output: ContractComposerReport,
        payload: dict[str, Any],
        reports: list[EvaluatorReport],
        target_ids: set[str],
        dependencies: list[str],
        round_number: int,
    ) -> tuple[ContractComposerReport, str]:
        attempt = 0
        previous_id: str | None = None
        while True:
            artifact_id = self.store.save(
                "contract-composer-report",
                output,
                dependencies=dependencies + ([previous_id] if previous_id else []),
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            validation = validate_contract_composer(
                output,
                reports,
                target_ids=target_ids,
                evaluator_a_hard_criteria=CONTRACT_EVALUATOR_A_HARD_GATES,
                evaluator_b_hard_criteria=CONTRACT_EVALUATOR_B_HARD_GATES,
                evaluator_a_target_gates=CONTRACT_EVALUATOR_A_TARGET_GATES,
                evaluator_b_target_gates=CONTRACT_EVALUATOR_B_TARGET_GATES,
                evaluator_a_evidence_criteria=(
                    CONTRACT_EVALUATOR_A_EVIDENCE_GATES
                ),
                evaluator_a_evidence_target_gates=(
                    CONTRACT_EVALUATOR_A_EVIDENCE_TARGET_GATES
                ),
                minimum_passing_score=self.settings.minimum_gate_score,
            )
            self.store.save(
                "contract-composer-validation",
                compact_validation(validation),
                dependencies=[artifact_id],
                metadata={"round": round_number, "repair_attempt": attempt},
            )
            if validation.valid:
                return output, artifact_id
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                raise ResearchPlanningError(
                    "Contract Composer failed its repair budget"
                )
            attempt += 1
            previous_id = artifact_id
            output = await self.composer.run(
                {
                    **payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": compact_validation(validation),
                        "instruction": (
                            "Return a corrected Composer report. Keep all scientific "
                            "inputs frozen."
                        ),
                    },
                },
                session_label=(
                    f"contract-composer-round-{round_number}-repair-{attempt}"
                ),
            )

    def _finalize(
        self,
        assessment: ResearchModeAssessment,
        contract: ResearchContract,
        selected_ids: set[str],
        evaluator_a: EvaluatorReport | None,
        round_number: int,
        *,
        dependencies: list[str],
    ) -> ResearchStageResult:
        evidence = _merge_evidence(
            contract.evidence,
            evaluator_a.discovered_evidence if evaluator_a else [],
        )
        readiness = {
            ResearchMode.DIRECT_TEST: ResearchReadiness.TEST_READY,
            ResearchMode.BENCHMARK_AUDIT: ResearchReadiness.AUDIT_READY,
        }[assessment.proposed_mode]
        selected = sorted(selected_ids)[: self.settings.target_promoted_hypotheses]
        final_contract = ResearchContract.model_validate(
            {
                **contract.model_dump(mode="json"),
                "readiness": readiness,
                "selected_target_ids": selected,
                "evidence": [item.model_dump(mode="json") for item in evidence],
            }
        )
        final_id = self.store.save(
            "research-contract-final",
            final_contract,
            dependencies=dependencies,
            metadata={"round": round_number, "mode": assessment.proposed_mode},
        )
        result = ResearchStageResult(
            mode_assessment=assessment,
            contract=final_contract,
            source_stage="DIRECT_STUDY",
            round_number=round_number,
        )
        self.store.checkpoint("research", result)
        self.store.event(
            "research_contract.ready",
            {
                "artifact_id": final_id,
                "mode": assessment.proposed_mode,
                "readiness": readiness,
                "selected_target_ids": selected,
            },
        )
        return result


def research_stage_from_hypotheses(
    assessment: ResearchModeAssessment,
    stage: HypothesisStageResult,
    *,
    max_selected_targets: int = 2,
) -> ResearchStageResult:
    if assessment.proposed_mode != ResearchMode.EXPLANATORY_RESEARCH:
        raise ValueError("Hypothesis results require EXPLANATORY_RESEARCH mode")
    output = stage.director_output
    targets = [
        ResearchTarget(
            target_id=item.hypothesis_id,
            target_type=ResearchTargetType.MECHANISTIC_HYPOTHESIS,
            statement=item.statement,
            null_statement=(
                "The proposed mechanism does not produce its distinctive prediction "
                "under the controlled condition."
            ),
            rationale=(
                f"Nearest-work difference: {item.nearest_work_difference} "
                f"Knowledge change: {item.knowledge_change}"
            ),
            mechanism=item.mechanism,
            distinctive_prediction=item.distinctive_prediction,
            falsification_condition=item.falsification_condition,
            alternative_explanations=item.alternative_explanations,
            positive_result_value=item.positive_result_value,
            negative_result_value=item.negative_result_value,
            null_result_value=item.null_result_value,
            minimum_experiment=item.minimum_experiment,
            required_data=item.required_data,
            compute_estimate=item.compute_estimate,
            uncertainties=item.uncertainties,
            evidence_ids=item.evidence_ids,
        )
        for item in output.hypotheses
    ]
    prediction_matrix = [
        ResearchCondition(
            condition_id=condition.condition_id,
            description=condition.description,
            controlled_variables=condition.controlled_variables,
            manipulated_variables=condition.manipulated_variables,
            measurement=condition.measurement,
            decision_threshold=condition.decision_threshold,
            predictions=[
                ResearchPredictionCell(
                    target_id=prediction.hypothesis_id,
                    direction=prediction.direction,
                    expected_pattern=prediction.expected_pattern,
                    rejection_condition=prediction.rejection_condition,
                )
                for prediction in condition.predictions
            ],
        )
        for condition in output.prediction_matrix
    ]
    evidence = _merge_evidence(
        output.evidence,
        stage.evaluator_a.discovered_evidence,
    )
    contract = ResearchContract(
        contract_version="1.0",
        original_question=output.original_question,
        research_mode=ResearchMode.EXPLANATORY_RESEARCH,
        readiness=ResearchReadiness.THEORY_READY,
        selected_domain=output.selected_domain,
        scope=output.scope,
        mode_rationale=assessment.classification_reason,
        claim_ceiling=assessment.claim_ceiling,
        evidence=evidence,
        claims=output.claims,
        targets=targets,
        selected_target_ids=list(
            dict.fromkeys(stage.composer.promoted_hypothesis_ids)
        )[:max_selected_targets],
        prediction_matrix=prediction_matrix,
        search_limitations=output.search_limitations,
    )
    return ResearchStageResult(
        mode_assessment=assessment,
        contract=contract,
        source_stage="HYPOTHESIS",
        round_number=stage.round_number,
    )


def _preserve_locked_targets(
    previous: ResearchContract,
    current: ResearchContract,
    locked_ids: set[str],
) -> ResearchContract:
    previous_by_id = {item.target_id: item for item in previous.targets}
    locked = [
        previous_by_id[item.target_id]
        for item in previous.targets
        if item.target_id in locked_ids
    ]
    unlocked = [item for item in current.targets if item.target_id not in locked_ids]
    targets = (locked + unlocked)[:5]
    required_evidence_ids = {
        evidence_id for item in locked for evidence_id in item.evidence_ids
    }
    evidence_by_id = {item.evidence_id: item for item in current.evidence}
    for item in previous.evidence:
        if item.evidence_id in required_evidence_ids and item.evidence_id not in evidence_by_id:
            evidence_by_id[item.evidence_id] = item
    locked_conditions = [
        condition
        for condition in previous.prediction_matrix
        if any(
            prediction.target_id in locked_ids
            for prediction in condition.predictions
        )
    ]
    locked_condition_ids = {item.condition_id for item in locked_conditions}
    unlocked_conditions = [
        condition
        for condition in current.prediction_matrix
        if condition.condition_id not in locked_condition_ids
    ]
    return current.model_copy(
        update={
            "targets": targets,
            "evidence": list(evidence_by_id.values()),
            "prediction_matrix": locked_conditions + unlocked_conditions,
        }
    )


def _merge_evidence(first, second):
    by_id = {item.evidence_id: item for item in first}
    for item in second:
        if item.evidence_id not in by_id:
            by_id[item.evidence_id] = item
    return list(by_id.values())
