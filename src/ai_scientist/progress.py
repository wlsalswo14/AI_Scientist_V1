from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactStore
from .schemas import (
    ComposerReport,
    ContractComposerReport,
    DirectorOutput,
    EvaluatorReport,
    ExEvaluatorReport,
    ExecutionResult,
    PaperDraft,
    ResearchContract,
    ReviewReport,
)
from .runtime import RuntimeStateStore


class ProgressVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    metrics: dict[str, float]


class ProgressDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_available: bool
    improvements: dict[str, float] = Field(default_factory=dict)
    regressions: dict[str, float] = Field(default_factory=dict)
    unchanged: list[str] = Field(default_factory=list)
    forward_progress: bool
    stagnant: bool


class ProgressRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector: ProgressVector
    delta: ProgressDelta
    consecutive_stagnant_rounds: int


def compare_progress(
    current: ProgressVector,
    previous: ProgressVector | None,
    *,
    lower_is_better: set[str] | None = None,
    tolerance: float = 1e-9,
    minimum_change: dict[str, float] | None = None,
) -> ProgressDelta:
    if previous is None:
        return ProgressDelta(
            previous_available=False,
            forward_progress=True,
            stagnant=False,
        )
    lower = lower_is_better or set()
    thresholds = {"average_score": 0.25, **(minimum_change or {})}
    improvements: dict[str, float] = {}
    regressions: dict[str, float] = {}
    unchanged: list[str] = []
    for name in sorted(set(current.metrics).intersection(previous.metrics)):
        raw_delta = current.metrics[name] - previous.metrics[name]
        directional_delta = -raw_delta if name in lower else raw_delta
        threshold = thresholds.get(name, tolerance)
        if directional_delta >= threshold:
            improvements[name] = raw_delta
        elif directional_delta <= -threshold:
            regressions[name] = raw_delta
        else:
            unchanged.append(name)
    forward = bool(improvements)
    return ProgressDelta(
        previous_available=True,
        improvements=improvements,
        regressions=regressions,
        unchanged=unchanged,
        forward_progress=forward,
        stagnant=not forward,
    )


class ProgressTracker:
    """Persisted no-progress detection shared by all iterative stages."""

    def __init__(
        self,
        store: ArtifactStore,
        stage: str,
        *,
        lower_is_better: set[str],
        max_stagnant_rounds: int,
        state_store: RuntimeStateStore | None = None,
    ) -> None:
        self.store = store
        self.stage = stage
        self.kind = f"{stage}-progress"
        self.lower_is_better = lower_is_better
        self.max_stagnant_rounds = max_stagnant_rounds
        self.state_store = state_store

    def record(
        self,
        vector: ProgressVector,
        *,
        dependencies: list[str] | None = None,
        round_number: int | None = None,
        scope_id: str | None = None,
    ) -> ProgressRecord:
        # Stale attempts remain useful as historical baselines even though they may
        # no longer support downstream scientific claims.
        previous_envelope = self.store.latest_envelope(self.kind, valid_only=False)
        if (
            previous_envelope is not None
            and scope_id is not None
            and previous_envelope.get("metadata", {}).get("scope_id") != scope_id
        ):
            previous_envelope = None
        previous_record = (
            ProgressRecord.model_validate(previous_envelope["payload"])
            if previous_envelope is not None
            else None
        )
        delta = compare_progress(
            vector,
            previous_record.vector if previous_record else None,
            lower_is_better=self.lower_is_better,
        )
        consecutive = (
            previous_record.consecutive_stagnant_rounds + 1
            if delta.stagnant and previous_record
            else (1 if delta.stagnant else 0)
        )
        record = ProgressRecord(
            vector=vector,
            delta=delta,
            consecutive_stagnant_rounds=consecutive,
        )
        self.store.save(
            self.kind,
            record,
            dependencies=dependencies,
            metadata={"round": round_number, "scope_id": scope_id},
        )
        self.store.event(
            "progress.measured",
            {
                "stage": self.stage,
                "round": round_number,
                "metrics": vector.metrics,
                "improvements": delta.improvements,
                "regressions": delta.regressions,
                "consecutive_stagnant_rounds": consecutive,
            },
        )
        if self.state_store is not None:
            self.state_store.heartbeat(
                action=f"progress:{self.stage}",
                progress=vector.metrics,
                stage=self.stage,
            )
        return record

    def should_stop(self, record: ProgressRecord) -> bool:
        return (
            record.consecutive_stagnant_rounds >= self.max_stagnant_rounds
        )


def hypothesis_progress_vector(
    director: DirectorOutput,
    evaluator_a: EvaluatorReport,
    evaluator_b: EvaluatorReport,
    composer: ComposerReport,
) -> ProgressVector:
    reports = [evaluator_a, evaluator_b]
    fatal_issues = sum(len(report.fatal_issues) for report in reports)
    fatal_issues += sum(
        len(target.fatal_issues)
        for report in reports
        for target in report.target_evaluations
    )
    failed_gates = sum(
        1
        for report in reports
        for target in report.target_evaluations
        for gate in target.gates
        if not gate.passed or gate.fatal_issue
    )
    distinctive = sum(
        1 for item in director.hypotheses if item.distinctive_prediction.strip()
    )
    average = sum(report.average_score for report in reports) / len(reports)
    return ProgressVector(
        stage="hypothesis",
        metrics={
            "promoted_targets": float(len(composer.promoted_hypothesis_ids)),
            "validated_evidence": float(len(director.evidence)),
            "distinctive_predictions": float(distinctive),
            "average_score": average,
            "fatal_issues": float(fatal_issues),
            "failed_gates": float(failed_gates),
            "evidence_failures": float(len(composer.evidence_failures)),
        },
    )


def contract_progress_vector(
    contract: ResearchContract,
    evaluator_a: EvaluatorReport,
    evaluator_b: EvaluatorReport,
    composer: ContractComposerReport,
) -> ProgressVector:
    reports = [evaluator_a, evaluator_b]
    failed_gates = sum(
        1
        for report in reports
        for target in report.target_evaluations
        for gate in target.gates
        if not gate.passed or gate.fatal_issue
    )
    fatal_issues = sum(len(report.fatal_issues) for report in reports)
    average = sum(report.average_score for report in reports) / len(reports)
    return ProgressVector(
        stage="research-planning",
        metrics={
            "promoted_targets": float(len(composer.promoted_target_ids)),
            "validated_evidence": float(len(contract.evidence)),
            "prediction_conditions": float(len(contract.prediction_matrix)),
            "average_score": average,
            "fatal_issues": float(fatal_issues),
            "failed_gates": float(failed_gates),
            "evidence_failures": float(len(composer.evidence_failures)),
        },
    )


def experiment_progress_vector(
    executions: list[ExecutionResult],
    evaluation: ExEvaluatorReport,
) -> ProgressVector:
    successful = [
        item
        for item in executions
        if item.exit_code == 0 and not item.timed_out and item.result_ids
    ]
    protocol_violations = sum(
        1 for item in evaluation.judgments if item.status.value == "PROTOCOL_VIOLATION"
    )
    contaminated = sum(
        1
        for item in evaluation.contamination_by_experimentor
        if item.status.value == "CONTAMINATED"
    )
    return ProgressVector(
        stage="experiment",
        metrics={
            "successful_executions": float(len(successful)),
            "validated_results": float(
                len({result for item in successful for result in item.result_ids})
            ),
            "completed_judgments": float(len(evaluation.judgments)),
            "average_score": evaluation.average_score,
            "affected_targets": float(len(evaluation.affected_hypothesis_ids)),
            "protocol_violations": float(protocol_violations),
            "contaminated_experimentors": float(contaminated),
        },
    )


def paper_progress_vector(draft: PaperDraft, review: ReviewReport) -> ProgressVector:
    supported_claims = sum(
        1 for item in draft.linked_claims if item.evidence_ids or item.result_ids
    )
    return ProgressVector(
        stage="paper",
        metrics={
            "supported_claims": float(supported_claims),
            "average_score": review.average_score,
            "fatal_issues": float(len(review.fatal_issues)),
            "non_fatal_issues": float(len(review.non_fatal_issues)),
            "acceptance_conditions": float(len(review.acceptance_conditions)),
        },
    )
