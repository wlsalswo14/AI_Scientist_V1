from __future__ import annotations

import asyncio
from typing import Any

from ..agents import ComposerAgent, DirectorAgent, EvaluatorAAgent, EvaluatorBAgent
from ..artifacts import ArtifactStore
from ..config import Settings, hypothesis_rounds, repair_budget_exhausted
from ..quality import compare_quality
from ..progress import ProgressTracker, hypothesis_progress_vector
from ..runtime import DeadlinePolicy, RuntimePhase
from ..schemas import (
    ComposerReport,
    DirectorOutput,
    HypothesisStageResult,
    ResearchModeAssessment,
    WorkflowAction,
)
from .errors import ResearchModeReclassificationError
from ..validation import (
    compact_validation,
    validate_composer,
    validate_director,
    validate_evaluator,
)


EVALUATOR_A_CRITERIA = {
    "Mode Fit",
    "Tension Validity",
    "Literature Coverage",
    "Evidence Entailment",
    "Citation Accuracy",
    "Novelty",
    "Nearest-work Differentiation",
    "Contradictory Evidence Coverage",
    "Evidence Quality",
}

EVALUATOR_B_CRITERIA = {
    "Mode Fit",
    "Importance",
    "Explanatory Gain",
    "Non-triviality",
    "Distinctive Prediction",
    "Falsifiability",
    "Alternative-explanation Coverage",
    "Informative Outcomes",
    "Feasibility",
}

EVALUATOR_A_HARD_GATES = {
    "Mode Fit",
    "Evidence Entailment",
    "Citation Accuracy",
    "Novelty",
    "Nearest-work Differentiation",
    "Evidence Quality",
}

EVALUATOR_B_HARD_GATES = {
    "Mode Fit",
    "Importance",
    "Distinctive Prediction",
    "Falsifiability",
    "Informative Outcomes",
    "Feasibility",
}

EVALUATOR_A_TARGET_GATES = {"Evidence Support", "Nearest-work Differentiation"}
EVALUATOR_B_TARGET_GATES = {
    "Distinctive Prediction",
    "Falsifiability",
    "Feasibility",
}

EVALUATOR_A_EVIDENCE_GATES = EVALUATOR_A_HARD_GATES - {"Mode Fit"}
EVALUATOR_A_EVIDENCE_TARGET_GATES = set(EVALUATOR_A_TARGET_GATES)


class HypothesisWorkflowError(RuntimeError):
    pass


class HypothesisWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        director: DirectorAgent,
        evaluator_a: EvaluatorAAgent,
        evaluator_b: EvaluatorBAgent,
        composer: ComposerAgent,
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
            "hypothesis",
            lower_is_better={"fatal_issues", "failed_gates", "evidence_failures"},
            max_stagnant_rounds=settings.max_stagnant_rounds,
            state_store=(
                deadline_policy.state_store if deadline_policy is not None else None
            ),
        )

    async def run(
        self,
        question: str,
        *,
        upstream_feedback: list[str] | None = None,
        mode_assessment: ResearchModeAssessment | None = None,
        initial_locked_hypothesis_ids: set[str] | None = None,
    ) -> HypothesisStageResult:
        prior_director: dict[str, Any] | None = None
        prior_director_model: DirectorOutput | None = None
        prior_composer: dict[str, Any] | None = None
        prior_scores = None
        locked_hypothesis_ids: set[str] = set()
        last_result: HypothesisStageResult | None = None
        start_round = 1
        if not upstream_feedback or initial_locked_hypothesis_ids is not None:
            checkpoint = self.store.load_checkpoint(
                "hypothesis",
                HypothesisStageResult,
            )
            if (
                checkpoint is not None
                and checkpoint.director_output.original_question == question
            ):
                last_result = checkpoint
                prior_director_model = checkpoint.director_output
                prior_director = checkpoint.director_output.model_dump(mode="json")
                prior_composer = checkpoint.composer.model_dump(mode="json")
                prior_scores = (
                    checkpoint.evaluator_a.criteria
                    + checkpoint.evaluator_b.criteria
                )
                locked_hypothesis_ids = set(
                    checkpoint.composer.promoted_hypothesis_ids
                )
                if initial_locked_hypothesis_ids is not None:
                    locked_hypothesis_ids.intersection_update(
                        initial_locked_hypothesis_ids
                    )
                if (
                    checkpoint.composer.action == WorkflowAction.PROMOTE
                    and locked_hypothesis_ids
                    and initial_locked_hypothesis_ids is None
                ):
                    self.store.event(
                        "hypothesis.resumed_promoted_checkpoint",
                        {"round": checkpoint.round_number},
                    )
                    return checkpoint
                start_round = (
                    1
                    if initial_locked_hypothesis_ids is not None
                    else checkpoint.round_number + 1
                )
                self.store.event(
                    "hypothesis.resumed_from_checkpoint",
                    {"next_round": start_round},
                )
        for round_number in hypothesis_rounds(
            self.settings.max_hypothesis_rounds,
            start=start_round,
        ):
            if self.deadline_policy is not None:
                deadline = self.deadline_policy.can_start(
                    RuntimePhase.RESEARCH_PLANNING
                )
                if not deadline.allowed:
                    self.store.event(
                        "deadline.round_blocked",
                        {
                            "stage": "hypothesis",
                            "round": round_number,
                            "reason": deadline.reason,
                            "remaining_seconds": deadline.remaining_seconds,
                        },
                    )
                    break
            director_payload = {
                "question": question,
                "mode_assessment": (
                    mode_assessment.model_dump(mode="json")
                    if mode_assessment
                    else None
                ),
                "round": round_number,
                "constraints": {
                    "hypothesis_candidates": [
                        self.settings.min_hypotheses,
                        self.settings.max_hypotheses,
                    ],
                    "single_input": True,
                    "computational_experiment_required": True,
                },
                "upstream_feedback": upstream_feedback or [],
                "prior_checkpoint": prior_director,
                "failure_notebook": (
                    prior_composer.get("failure_notebook", []) if prior_composer else []
                ),
                "locked_hypotheses": (
                    [
                        item.model_dump(mode="json")
                        for item in prior_director_model.hypotheses
                        if item.hypothesis_id in locked_hypothesis_ids
                    ]
                    if prior_director_model
                    else []
                ),
                "revision_target_ids": (
                    [
                        item.hypothesis_id
                        for item in prior_director_model.hypotheses
                        if item.hypothesis_id not in locked_hypothesis_ids
                    ]
                    if prior_director_model
                    else []
                ),
            }
            director_output = await self.director.run(
                director_payload,
                session_label=f"director-round-{round_number}",
            )
            if prior_director_model is not None and locked_hypothesis_ids:
                director_output = _preserve_locked_hypotheses(
                    prior_director_model,
                    director_output,
                    locked_hypothesis_ids,
                )
            director_id = self.store.save(
                "director-output",
                director_output,
                metadata={"round": round_number},
            )
            director_validation = validate_director(director_output, self.settings)
            self.store.save(
                "director-validation",
                {"valid": director_validation.valid, "issues": compact_validation(director_validation)},
                dependencies=[director_id],
            )
            if not director_validation.valid:
                prior_director = director_output.model_dump(mode="json")
                prior_director_model = director_output
                prior_composer = {
                    "failure_notebook": [],
                    "validation_repairs": compact_validation(director_validation),
                }
                self.store.event(
                    "hypothesis.director_repair",
                    {"round": round_number, "issues": compact_validation(director_validation)},
                )
                continue

            frozen = director_output.model_dump(mode="json")
            common_payload = {
                "question": question,
                "mode_assessment": (
                    mode_assessment.model_dump(mode="json")
                    if mode_assessment
                    else None
                ),
                "director_artifact": frozen,
                "rubric_version": self.settings.rubric_version,
                "minimum_passing_score": self.settings.minimum_gate_score,
            }
            evaluator_a_output, evaluator_b_output = await asyncio.gather(
                self.evaluator_a.run(
                    common_payload,
                    session_label=f"evaluator-a-isolated-round-{round_number}",
                ),
                self.evaluator_b.run(
                    common_payload,
                    session_label=f"evaluator-b-isolated-round-{round_number}",
                ),
            )
            evaluator_a_id = self.store.save(
                "evaluator-a-report",
                evaluator_a_output,
                dependencies=[director_id],
                metadata={"round": round_number, "isolated": True},
            )
            evaluator_b_id = self.store.save(
                "evaluator-b-report",
                evaluator_b_output,
                dependencies=[director_id],
                metadata={"round": round_number, "isolated": True},
            )
            evidence_ids = {item.evidence_id for item in director_output.evidence}
            hypothesis_ids = {
                item.hypothesis_id for item in director_output.hypotheses
            }
            validation_a = validate_evaluator(
                evaluator_a_output,
                required_criteria=EVALUATOR_A_CRITERIA,
                evidence_ids=evidence_ids,
                rubric_version=self.settings.rubric_version,
                target_ids=hypothesis_ids,
                required_target_gates=EVALUATOR_A_TARGET_GATES,
                minimum_passing_score=self.settings.minimum_gate_score,
            )
            validation_b = validate_evaluator(
                evaluator_b_output,
                required_criteria=EVALUATOR_B_CRITERIA,
                evidence_ids=evidence_ids,
                rubric_version=self.settings.rubric_version,
                target_ids=hypothesis_ids,
                required_target_gates=EVALUATOR_B_TARGET_GATES,
                minimum_passing_score=self.settings.minimum_gate_score,
            )
            self.store.save(
                "evaluator-validation",
                {
                    "a": compact_validation(validation_a),
                    "b": compact_validation(validation_b),
                },
                dependencies=[evaluator_a_id, evaluator_b_id],
                metadata={"round": round_number, "repair_attempt": 0},
            )
            repair_attempt = 0
            while not validation_a.valid or not validation_b.valid:
                if repair_budget_exhausted(
                    repair_attempt, self.settings.max_component_repair_attempts
                ):
                    issues = {
                        "a": compact_validation(validation_a),
                        "b": compact_validation(validation_b),
                    }
                    self.store.event(
                        "hypothesis.evaluator_repair_exhausted",
                        {"round": round_number, "issues": issues},
                    )
                    raise HypothesisWorkflowError(
                        "Evaluator Harness repair budget ended while the Director "
                        "artifact remained frozen"
                    )
                repair_attempt += 1
                repair_roles: list[str] = []
                repair_tasks = []
                if not validation_a.valid:
                    repair_roles.append("a")
                    repair_tasks.append(
                        self.evaluator_a.run(
                            {
                                **common_payload,
                                "repair_only": {
                                    "prior_output": evaluator_a_output.model_dump(
                                        mode="json"
                                    ),
                                    "validation_issues": compact_validation(validation_a),
                                    "instruction": (
                                        "Return a complete corrected Evaluator A report. "
                                        "Change only the fields needed to satisfy these "
                                        "Harness issues; the Director artifact is frozen."
                                    ),
                                },
                            },
                            session_label=(
                                f"evaluator-a-isolated-round-{round_number}-"
                                f"repair-{repair_attempt}"
                            ),
                        )
                    )
                if not validation_b.valid:
                    repair_roles.append("b")
                    repair_tasks.append(
                        self.evaluator_b.run(
                            {
                                **common_payload,
                                "repair_only": {
                                    "prior_output": evaluator_b_output.model_dump(
                                        mode="json"
                                    ),
                                    "validation_issues": compact_validation(validation_b),
                                    "instruction": (
                                        "Return a complete corrected Evaluator B report. "
                                        "Change only the fields needed to satisfy these "
                                        "Harness issues; the Director artifact is frozen."
                                    ),
                                },
                            },
                            session_label=(
                                f"evaluator-b-isolated-round-{round_number}-"
                                f"repair-{repair_attempt}"
                            ),
                        )
                    )
                repaired_outputs = await asyncio.gather(*repair_tasks)
                for role, repaired_output in zip(repair_roles, repaired_outputs):
                    if role == "a":
                        previous_id = evaluator_a_id
                        evaluator_a_output = repaired_output
                        evaluator_a_id = self.store.save(
                            "evaluator-a-report",
                            evaluator_a_output,
                            dependencies=[director_id, previous_id],
                            metadata={
                                "round": round_number,
                                "isolated": True,
                                "repair_attempt": repair_attempt,
                                "repair_only": True,
                            },
                        )
                        validation_a = validate_evaluator(
                            evaluator_a_output,
                            required_criteria=EVALUATOR_A_CRITERIA,
                            evidence_ids=evidence_ids,
                            rubric_version=self.settings.rubric_version,
                            target_ids=hypothesis_ids,
                            required_target_gates=EVALUATOR_A_TARGET_GATES,
                            minimum_passing_score=self.settings.minimum_gate_score,
                        )
                    else:
                        previous_id = evaluator_b_id
                        evaluator_b_output = repaired_output
                        evaluator_b_id = self.store.save(
                            "evaluator-b-report",
                            evaluator_b_output,
                            dependencies=[director_id, previous_id],
                            metadata={
                                "round": round_number,
                                "isolated": True,
                                "repair_attempt": repair_attempt,
                                "repair_only": True,
                            },
                        )
                        validation_b = validate_evaluator(
                            evaluator_b_output,
                            required_criteria=EVALUATOR_B_CRITERIA,
                            evidence_ids=evidence_ids,
                            rubric_version=self.settings.rubric_version,
                            target_ids=hypothesis_ids,
                            required_target_gates=EVALUATOR_B_TARGET_GATES,
                            minimum_passing_score=self.settings.minimum_gate_score,
                        )
                    self.store.event(
                        "hypothesis.evaluator_component_retried",
                        {
                            "round": round_number,
                            "evaluator": role.upper(),
                            "repair_attempt": repair_attempt,
                        },
                    )
                self.store.save(
                    "evaluator-validation",
                    {
                        "a": compact_validation(validation_a),
                        "b": compact_validation(validation_b),
                    },
                    dependencies=[evaluator_a_id, evaluator_b_id],
                    metadata={
                        "round": round_number,
                        "repair_attempt": repair_attempt,
                    },
                )

            composer_payload = {
                "mode_assessment": (
                    mode_assessment.model_dump(mode="json")
                    if mode_assessment
                    else None
                ),
                "director_artifact": frozen,
                "evaluator_a": evaluator_a_output.model_dump(mode="json"),
                "evaluator_b": evaluator_b_output.model_dump(mode="json"),
                "previous_composer": prior_composer,
                "previous_director": prior_director,
                "thresholds": {
                    "minimum_passing_score": self.settings.minimum_gate_score,
                    "score_drop_absolute": self.settings.score_drop_absolute,
                    "score_drop_relative": self.settings.score_drop_relative,
                    "critical_dimension_drop": self.settings.critical_dimension_drop,
                },
                "python_quality_comparison": compare_quality(
                    evaluator_a_output.criteria + evaluator_b_output.criteria,
                    prior_scores,
                    critical_dimensions={
                        "Mode Fit",
                        "Evidence Quality",
                        "Distinctive Prediction",
                        "Falsifiability",
                        "Explanatory Gain",
                    },
                    settings=self.settings,
                ).to_dict(),
            }
            composer_output = await self.composer.run(
                composer_payload,
                session_label=f"composer-round-{round_number}",
            )
            valid_ids = {item.hypothesis_id for item in director_output.hypotheses}
            composer_output = composer_output.model_copy(
                update={
                    "promoted_hypothesis_ids": sorted(
                        locked_hypothesis_ids.union(
                            item
                            for item in composer_output.promoted_hypothesis_ids
                            if item in valid_ids
                        )
                    )
                }
            )
            composer_id = self.store.save(
                "composer-report",
                composer_output,
                dependencies=[director_id, evaluator_a_id, evaluator_b_id],
                metadata={"round": round_number, "repair_attempt": 0},
            )
            composer_validation = validate_composer(
                composer_output,
                [evaluator_a_output, evaluator_b_output],
                target_ids=valid_ids,
                evaluator_a_hard_criteria=EVALUATOR_A_HARD_GATES,
                evaluator_b_hard_criteria=EVALUATOR_B_HARD_GATES,
                evaluator_a_target_gates=EVALUATOR_A_TARGET_GATES,
                evaluator_b_target_gates=EVALUATOR_B_TARGET_GATES,
                evaluator_a_evidence_criteria=EVALUATOR_A_EVIDENCE_GATES,
                evaluator_a_evidence_target_gates=(
                    EVALUATOR_A_EVIDENCE_TARGET_GATES
                ),
                minimum_passing_score=self.settings.minimum_gate_score,
            )
            self.store.save(
                "composer-validation",
                compact_validation(composer_validation),
                dependencies=[composer_id],
                metadata={"round": round_number, "repair_attempt": 0},
            )
            composer_repair_attempt = 0
            while not composer_validation.valid:
                if repair_budget_exhausted(
                    composer_repair_attempt,
                    self.settings.max_component_repair_attempts,
                ):
                    issues = compact_validation(composer_validation)
                    self.store.event(
                        "hypothesis.composer_repair_exhausted",
                        {"round": round_number, "issues": issues},
                    )
                    raise HypothesisWorkflowError(
                        "Composer Harness repair budget ended while Director and "
                        "Evaluator artifacts remained frozen"
                    )
                composer_repair_attempt += 1
                previous_composer_id = composer_id
                composer_output = await self.composer.run(
                    {
                        **composer_payload,
                        "repair_only": {
                            "prior_output": composer_output.model_dump(mode="json"),
                            "validation_issues": compact_validation(
                                composer_validation
                            ),
                            "instruction": (
                                "Return a complete corrected Composer report. Change "
                                "only fields required by the Harness; all scientific "
                                "inputs are frozen."
                            ),
                        },
                    },
                    session_label=(
                        f"composer-round-{round_number}-"
                        f"repair-{composer_repair_attempt}"
                    ),
                )
                composer_output = composer_output.model_copy(
                    update={
                        "promoted_hypothesis_ids": sorted(
                            locked_hypothesis_ids.union(
                                item
                                for item in composer_output.promoted_hypothesis_ids
                                if item in valid_ids
                            )
                        )
                    }
                )
                composer_id = self.store.save(
                    "composer-report",
                    composer_output,
                    dependencies=[
                        director_id,
                        evaluator_a_id,
                        evaluator_b_id,
                        previous_composer_id,
                    ],
                    metadata={
                        "round": round_number,
                        "repair_attempt": composer_repair_attempt,
                        "repair_only": True,
                    },
                )
                composer_validation = validate_composer(
                    composer_output,
                    [evaluator_a_output, evaluator_b_output],
                    target_ids=valid_ids,
                    evaluator_a_hard_criteria=EVALUATOR_A_HARD_GATES,
                    evaluator_b_hard_criteria=EVALUATOR_B_HARD_GATES,
                    evaluator_a_target_gates=EVALUATOR_A_TARGET_GATES,
                    evaluator_b_target_gates=EVALUATOR_B_TARGET_GATES,
                    evaluator_a_evidence_criteria=EVALUATOR_A_EVIDENCE_GATES,
                    evaluator_a_evidence_target_gates=(
                        EVALUATOR_A_EVIDENCE_TARGET_GATES
                    ),
                    minimum_passing_score=self.settings.minimum_gate_score,
                )
                self.store.save(
                    "composer-validation",
                    compact_validation(composer_validation),
                    dependencies=[composer_id],
                    metadata={
                        "round": round_number,
                        "repair_attempt": composer_repair_attempt,
                    },
                )
                self.store.event(
                    "hypothesis.composer_component_retried",
                    {
                        "round": round_number,
                        "repair_attempt": composer_repair_attempt,
                    },
                )

            newly_promoted = {
                item
                for item in composer_output.promoted_hypothesis_ids
                if item in valid_ids and item not in locked_hypothesis_ids
            }
            locked_hypothesis_ids.update(newly_promoted)
            if composer_output.action == WorkflowAction.RECLASSIFY_MODE:
                self.store.event(
                    "research_mode.reclassification_requested",
                    {"round": round_number, "rationale": composer_output.rationale},
                )
                raise ResearchModeReclassificationError(composer_output.rationale)

            last_result = HypothesisStageResult(
                director_output=director_output,
                evaluator_a=evaluator_a_output,
                evaluator_b=evaluator_b_output,
                composer=composer_output,
                round_number=round_number,
            )
            self.store.checkpoint("hypothesis", last_result)
            progress_record = self.progress.record(
                hypothesis_progress_vector(
                    director_output,
                    evaluator_a_output,
                    evaluator_b_output,
                    composer_output,
                ),
                dependencies=[composer_id],
                round_number=round_number,
                scope_id=self.store.content_hash(
                    question + "\n" + "\n".join(upstream_feedback or [])
                ),
            )
            if composer_output.action == WorkflowAction.PROMOTE:
                promoted = [
                    item
                    for item in composer_output.promoted_hypothesis_ids
                    if item in valid_ids
                ]
                if promoted:
                    self.store.event(
                        "hypothesis.promoted",
                        {"round": round_number, "hypothesis_ids": promoted},
                    )
                    return last_result
            elif newly_promoted:
                self.store.event(
                    "hypothesis.partially_promoted",
                    {
                        "round": round_number,
                        "new_ids": sorted(newly_promoted),
                        "locked_ids": sorted(locked_hypothesis_ids),
                    },
                )
            if self.progress.should_stop(progress_record) and not newly_promoted:
                self.store.event(
                    "hypothesis.stalled",
                    {
                        "round": round_number,
                        "consecutive_stagnant_rounds": (
                            progress_record.consecutive_stagnant_rounds
                        ),
                    },
                )
                break
            prior_director = frozen
            prior_director_model = director_output
            prior_composer = composer_output.model_dump(mode="json")
            prior_scores = evaluator_a_output.criteria + evaluator_b_output.criteria
            if composer_output.contamination_status.value == "CONTAMINATED":
                self.store.event(
                    "session.replaced",
                    {"role": "director", "round": round_number + 1},
                )
        if last_result is not None and locked_hypothesis_ids:
            self.store.event(
                "hypothesis.promoted_after_budget",
                {"hypothesis_ids": sorted(locked_hypothesis_ids)},
            )
            return last_result
        raise HypothesisWorkflowError(
            "The hypothesis budget ended without any hypothesis passing all hard gates"
        )


def _preserve_locked_hypotheses(
    previous: DirectorOutput,
    current: DirectorOutput,
    locked_ids: set[str],
) -> DirectorOutput:
    previous_by_id = {item.hypothesis_id: item for item in previous.hypotheses}
    locked = [
        previous_by_id[item.hypothesis_id]
        for item in previous.hypotheses
        if item.hypothesis_id in locked_ids
    ]
    unlocked = [
        item for item in current.hypotheses if item.hypothesis_id not in locked_ids
    ]
    hypotheses = (locked + unlocked)[:5]

    evidence_by_id = {item.evidence_id: item for item in current.evidence}
    required_evidence_ids = {
        evidence_id for item in locked for evidence_id in item.evidence_ids
    }
    for item in previous.evidence:
        if item.evidence_id in required_evidence_ids and item.evidence_id not in evidence_by_id:
            evidence_by_id[item.evidence_id] = item

    tension_by_id = {item.tension_id: item for item in current.tensions}
    required_tension_ids = {item.tension_id for item in locked}
    for item in previous.tensions:
        if item.tension_id in required_tension_ids and item.tension_id not in tension_by_id:
            tension_by_id[item.tension_id] = item

    return current.model_copy(
        update={
            "hypotheses": hypotheses,
            "evidence": list(evidence_by_id.values()),
            "tensions": list(tension_by_id.values()),
        }
    )
