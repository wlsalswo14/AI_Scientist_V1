from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from pydantic import BaseModel

from .config import repair_attempts
from .agents.experiment import (
    EvidenceCriticAgent,
    EvidenceGlobalAuditAgent,
    EvidenceResolverAgent,
)
from .schemas import (
    EvidenceAuditManifest,
    EvidenceAuditOutcome,
    EvidenceAuditUnit,
    EvidenceConcern,
    EvidenceConcernCategory,
    EvidenceConcernResolution,
    EvidenceConcernSeverity,
    EvidenceCriticReport,
    EvidenceGlobalAuditReport,
    EvidenceResolutionStatus,
    ExecutionResult,
    ExperimentContract,
    ExperimentorOutput,
    ResearchContract,
    TracePreparationStageResult,
    TraceReviewerDecisionBatch,
    WorkflowAction,
)


CRITIC_LENSES: dict[str, set[str]] = {
    "construct": {"research-contract", "experiment-contract", "target", "execution"},
    "data-population": {
        "research-contract",
        "experiment-contract",
        "target",
        "trace-preparation",
        "reviewer-decisions",
        "execution",
    },
    "independence": {
        "research-contract",
        "experiment-contract",
        "trace-preparation",
        "reviewer-decisions",
        "experimentor-output",
    },
    "circularity": {
        "research-contract",
        "experiment-contract",
        "trace-preparation",
        "reviewer-decisions",
        "execution",
    },
    "baseline-attribution": {"experiment-contract", "target", "execution"},
    "statistics": {"experiment-contract", "reviewer-decisions", "execution"},
    "generalization": {"research-contract", "target", "execution"},
}


def accumulate_repair_issues(
    history: list[Any], current: list[Any]
) -> list[Any]:
    """Append distinct repair issues while preserving their first-seen order."""

    merged = list(history)
    seen = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        for item in history
    }
    for item in current:
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen:
            seen.add(signature)
            merged.append(item)
    return merged

CRITIC_CATEGORIES: dict[str, set[EvidenceConcernCategory]] = {
    "construct": {
        EvidenceConcernCategory.CONSTRUCT_VALIDITY,
        EvidenceConcernCategory.TARGET_EVIDENCE_ALIGNMENT,
    },
    "data-population": {
        EvidenceConcernCategory.DATA_PROVENANCE,
        EvidenceConcernCategory.TARGET_EVIDENCE_ALIGNMENT,
        EvidenceConcernCategory.GOLD_LABEL_CREDIBILITY,
    },
    "independence": {
        EvidenceConcernCategory.EVALUATION_INDEPENDENCE,
        EvidenceConcernCategory.GOLD_LABEL_CREDIBILITY,
        EvidenceConcernCategory.DATA_PROVENANCE,
    },
    "circularity": {
        EvidenceConcernCategory.METHOD_BENCHMARK_CIRCULARITY,
        EvidenceConcernCategory.GOLD_LABEL_CREDIBILITY,
    },
    "baseline-attribution": {EvidenceConcernCategory.BASELINE_AND_ATTRIBUTION},
    "statistics": {EvidenceConcernCategory.STATISTICAL_VALIDITY},
    "generalization": {
        EvidenceConcernCategory.EXTERNAL_VALIDITY,
        EvidenceConcernCategory.CLAIM_SCOPE,
        EvidenceConcernCategory.TARGET_EVIDENCE_ALIGNMENT,
    },
}


class EvidenceAuditPipeline:
    """Ralph-style divergence/resolution audit for experimental evidence."""

    def __init__(
        self,
        critic: EvidenceCriticAgent,
        resolver: EvidenceResolverAgent,
        global_auditor: EvidenceGlobalAuditAgent,
        *,
        max_repair_attempts: int | None = None,
    ) -> None:
        self.critic = critic
        self.resolver = resolver
        self.global_auditor = global_auditor
        self.max_repair_attempts = max_repair_attempts

    async def run(
        self,
        *,
        research_contract: ResearchContract,
        experiment_contract: ExperimentContract,
        experimentor_outputs: list[ExperimentorOutput],
        executions: list[ExecutionResult],
        trace_preparation: TracePreparationStageResult | None,
        trace_reviewer_decisions: TraceReviewerDecisionBatch | None,
        round_number: int,
    ) -> EvidenceAuditOutcome:
        semaphore = asyncio.Semaphore(4)
        manifest = build_evidence_manifest(
            research_contract=research_contract,
            experiment_contract=experiment_contract,
            experimentor_outputs=experimentor_outputs,
            executions=executions,
            trace_preparation=trace_preparation,
            trace_reviewer_decisions=trace_reviewer_decisions,
        )
        allowed_targets = set(experiment_contract.hypothesis_ids)
        critic_reports = await asyncio.gather(
            *[
                _bounded(
                    semaphore,
                    self._run_critic(
                        lens,
                        manifest,
                        allowed_targets,
                        round_number=round_number,
                    ),
                )
                for lens in CRITIC_LENSES
            ]
        )
        concerns = materialize_concerns(critic_reports, allowed_targets)
        resolutions = await asyncio.gather(
            *[
                _bounded(
                    semaphore,
                    self._run_resolver(
                        concern,
                        manifest,
                        round_number=round_number,
                    ),
                )
                for concern in concerns
            ]
        )
        promoted = [
            item for item in resolutions if item.status == EvidenceResolutionStatus.PROMOTED
        ]
        global_audit = await self._run_global_audit(
            concerns,
            promoted,
            round_number=round_number,
        )
        kept = set(global_audit.kept_concern_ids)
        kept_resolutions = [item for item in promoted if item.concern_id in kept]
        major = sorted(
            item.concern_id
            for item in kept_resolutions
            if item.severity == EvidenceConcernSeverity.MAJOR
        )
        fatal = sorted(
            item.concern_id
            for item in kept_resolutions
            if item.severity == EvidenceConcernSeverity.FATAL
        )
        blocked = bool(major or fatal)
        return EvidenceAuditOutcome(
            manifest=manifest,
            critic_reports=list(critic_reports),
            concerns=concerns,
            resolutions=list(resolutions),
            global_audit=global_audit,
            unresolved_major_ids=major,
            unresolved_fatal_ids=fatal,
            recommended_action=(
                WorkflowAction.RETURN_TO_HYPOTHESIS if blocked else WorkflowAction.PASS
            ),
            paper_eligible=not blocked,
            complete=len(resolutions) == len(concerns),
        )

    async def _run_critic(
        self,
        lens: str,
        manifest: EvidenceAuditManifest,
        allowed_targets: set[str],
        *,
        round_number: int,
    ) -> EvidenceCriticReport:
        visible = [
            unit for unit in manifest.units if unit.unit_type in CRITIC_LENSES[lens]
        ]
        payload = {
            "critic_lens": lens,
            "allowed_target_ids": sorted(allowed_targets),
            "visible_unit_ids": [item.unit_id for item in visible],
            "audit_units": [item.model_dump(mode="json") for item in visible],
        }
        issue_history: list[Any] = []
        for attempt in repair_attempts(self.max_repair_attempts):
            report = await self.critic.run(
                payload,
                session_label=f"evidence-critic-{lens}-round-{round_number}-attempt-{attempt}",
            )
            issues = critic_report_issues(report, lens, allowed_targets)
            if not issues:
                return report
            issue_history = accumulate_repair_issues(issue_history, issues)
            payload = {
                **payload,
                "repair_only": {
                    "issues": issues,
                    "cumulative_issues": issue_history,
                    "instruction": (
                        "Satisfy every cumulative issue and do not regress fields "
                        "fixed in earlier attempts."
                    ),
                },
            }
        raise RuntimeError(f"Evidence Critic {lens} failed validation: {issues}")

    async def _run_resolver(
        self,
        concern: EvidenceConcern,
        manifest: EvidenceAuditManifest,
        *,
        round_number: int,
    ) -> EvidenceConcernResolution:
        allowed_units = {item.unit_id for item in manifest.units}
        payload = {
            "concern": concern.model_dump(mode="json"),
            "evidence_manifest": manifest.model_dump(mode="json"),
            "allowed_evidence_unit_ids": sorted(allowed_units),
        }
        issue_history: list[Any] = []
        for attempt in repair_attempts(self.max_repair_attempts):
            resolution = await self.resolver.run(
                payload,
                session_label=(
                    f"evidence-resolver-{concern.concern_id}-round-{round_number}-"
                    f"attempt-{attempt}"
                ),
            )
            issues = resolution_issues(resolution, concern.concern_id, allowed_units)
            if not issues:
                return resolution
            issue_history = accumulate_repair_issues(issue_history, issues)
            payload = {
                **payload,
                "repair_only": {
                    "issues": issues,
                    "cumulative_issues": issue_history,
                    "instruction": (
                        "Satisfy every cumulative issue and do not regress fields "
                        "fixed in earlier attempts."
                    ),
                },
            }
        raise RuntimeError(
            f"Evidence Resolver {concern.concern_id} failed validation: {issues}"
        )

    async def _run_global_audit(
        self,
        concerns: list[EvidenceConcern],
        promoted: list[EvidenceConcernResolution],
        *,
        round_number: int,
    ) -> EvidenceGlobalAuditReport:
        if not promoted:
            return EvidenceGlobalAuditReport(
                kept_concern_ids=[],
                discarded=[],
                rationale="No independently promoted evidence concerns remained.",
            )
        concern_by_id = {item.concern_id: item for item in concerns}
        promoted_ids = {item.concern_id for item in promoted}
        payload = {
            "promoted_concerns": [
                {
                    "concern": concern_by_id[item.concern_id].model_dump(mode="json"),
                    "resolution": item.model_dump(mode="json"),
                }
                for item in promoted
            ]
        }
        issue_history: list[Any] = []
        for attempt in repair_attempts(self.max_repair_attempts):
            report = await self.global_auditor.run(
                payload,
                session_label=(
                    f"evidence-global-audit-round-{round_number}-attempt-{attempt}"
                ),
            )
            issues = global_audit_issues(report, promoted_ids)
            if not issues:
                return report
            issue_history = accumulate_repair_issues(issue_history, issues)
            payload = {
                **payload,
                "repair_only": {
                    "issues": issues,
                    "cumulative_issues": issue_history,
                    "instruction": (
                        "Satisfy every cumulative issue and do not regress fields "
                        "fixed in earlier attempts."
                    ),
                },
            }
        raise RuntimeError(f"Evidence global audit failed validation: {issues}")


def build_evidence_manifest(
    *,
    research_contract: ResearchContract,
    experiment_contract: ExperimentContract,
    experimentor_outputs: list[ExperimentorOutput],
    executions: list[ExecutionResult],
    trace_preparation: TracePreparationStageResult | None,
    trace_reviewer_decisions: TraceReviewerDecisionBatch | None,
) -> EvidenceAuditManifest:
    units: list[EvidenceAuditUnit] = []

    def add(unit_id: str, unit_type: str, target_ids: list[str], value: Any) -> None:
        units.append(
            EvidenceAuditUnit(
                unit_id=unit_id,
                unit_type=unit_type,
                target_ids=target_ids,
                content=_compact_mapping(value),
            )
        )

    add(
        "EA-RESEARCH-CONTRACT",
        "research-contract",
        list(research_contract.selected_target_ids),
        research_contract,
    )
    add(
        "EA-EXPERIMENT-CONTRACT",
        "experiment-contract",
        list(experiment_contract.hypothesis_ids),
        experiment_contract,
    )
    for target in research_contract.targets:
        if target.target_id in experiment_contract.hypothesis_ids:
            add(f"EA-TARGET-{target.target_id}", "target", [target.target_id], target)
    for output in experimentor_outputs:
        add(
            f"EA-OUTPUT-{output.hypothesis_id}",
            "experimentor-output",
            [output.hypothesis_id],
            output,
        )
    for execution in executions:
        add(
            f"EA-EXECUTION-{execution.hypothesis_id}",
            "execution",
            [execution.hypothesis_id],
            execution,
        )
    if trace_preparation is not None:
        add(
            "EA-TRACE-PREPARATION",
            "trace-preparation",
            list(experiment_contract.hypothesis_ids),
            trace_preparation,
        )
    if trace_reviewer_decisions is not None:
        add(
            "EA-REVIEWER-DECISIONS",
            "reviewer-decisions",
            list(experiment_contract.hypothesis_ids),
            trace_reviewer_decisions,
        )
    return EvidenceAuditManifest(manifest_version="1.0", units=units)


def materialize_concerns(
    reports: list[EvidenceCriticReport],
    allowed_targets: set[str],
) -> list[EvidenceConcern]:
    concerns: list[EvidenceConcern] = []
    for report in reports:
        issues = critic_report_issues(report, report.critic_lens, allowed_targets)
        if issues:
            raise ValueError(f"Invalid critic report: {issues}")
        for index, question in enumerate(report.questions, start=1):
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "lens": report.critic_lens,
                        "question": question.question,
                        "targets": sorted(question.target_ids),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:10]
            concerns.append(
                EvidenceConcern(
                    concern_id=f"EC-{digest}-{index}",
                    critic_lens=report.critic_lens,
                    **question.model_dump(),
                )
            )
    return concerns


def critic_report_issues(
    report: EvidenceCriticReport,
    expected_lens: str,
    allowed_targets: set[str],
) -> list[str]:
    issues: list[str] = []
    if report.critic_lens != expected_lens:
        issues.append(f"critic_lens must be {expected_lens}")
    for index, question in enumerate(report.questions):
        unknown = set(question.target_ids) - allowed_targets
        if unknown:
            issues.append(f"questions[{index}] has unknown targets {sorted(unknown)}")
        if question.category not in CRITIC_CATEGORIES.get(expected_lens, set()):
            issues.append(
                f"questions[{index}] category {question.category.value} is outside "
                f"the {expected_lens} lens"
            )
    return issues


def resolution_issues(
    resolution: EvidenceConcernResolution,
    concern_id: str,
    allowed_units: set[str],
) -> list[str]:
    issues: list[str] = []
    if resolution.concern_id != concern_id:
        issues.append(f"concern_id must be {concern_id}")
    unknown = set(resolution.evidence_unit_ids) - allowed_units
    if unknown:
        issues.append(f"unknown evidence unit IDs: {sorted(unknown)}")
    if resolution.status == EvidenceResolutionStatus.PROMOTED:
        if not resolution.unresolved_gap.strip():
            issues.append("promoted concern requires an unresolved_gap")
        if resolution.recommended_action == WorkflowAction.PASS:
            issues.append("promoted concern cannot recommend PASS")
    return issues


def global_audit_issues(
    report: EvidenceGlobalAuditReport,
    promoted_ids: set[str],
) -> list[str]:
    kept = report.kept_concern_ids
    discarded = [item.concern_id for item in report.discarded]
    issues: list[str] = []
    if len(kept) != len(set(kept)) or len(discarded) != len(set(discarded)):
        issues.append("global audit IDs must be unique")
    if set(kept) & set(discarded):
        issues.append("a concern cannot be both kept and discarded")
    if set(kept) | set(discarded) != promoted_ids:
        issues.append("global audit must partition every promoted concern exactly once")
    for item in report.discarded:
        if item.canonical_id is None:
            issues.append(
                f"promoted concern {item.concern_id} may only be discarded as a duplicate"
            )
        if item.canonical_id is not None and item.canonical_id not in set(kept):
            issues.append(f"discarded {item.concern_id} has an unkept canonical ID")
    return issues


def _compact_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        raw: Any = value.model_dump(mode="json")
    elif isinstance(value, dict):
        raw = value
    else:
        raw = {"value": value}
    compacted = _compact_value(raw)
    return compacted if isinstance(compacted, dict) else {"value": compacted}


async def _bounded(semaphore: asyncio.Semaphore, awaitable: Any) -> Any:
    async with semaphore:
        return await awaitable


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 4_000 else value[:4_000] + "...[truncated]"
    if isinstance(value, list):
        if len(value) <= 12:
            return [_compact_value(item) for item in value]
        return {
            "_total_items": len(value),
            "_sample": [_compact_value(item) for item in value[:4]],
        }
    if isinstance(value, dict):
        return {str(key): _compact_value(item) for key, item in value.items()}
    return value
