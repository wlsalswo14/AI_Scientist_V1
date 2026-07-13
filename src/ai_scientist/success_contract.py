from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    ResearchContract,
    ResearchMode,
    ResearchProfile,
    ResearchReadiness,
    VerificationStatus,
)
from .trace_audit import trace_study_contract_issues


class ExecutableCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    description: str
    validator: str
    required_artifacts: list[str]
    hard_gate: bool = True


class ExecutableSuccessContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    source_research_contract_version: str
    research_mode: ResearchMode
    criteria: list[ExecutableCriterion] = Field(min_length=1)


class CriterionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    validator: str
    passed: bool
    issues: list[str]
    affected_target_ids: list[str] = Field(default_factory=list)


class SuccessContractReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    passed: bool
    results: list[CriterionVerification]

    @property
    def failure_messages(self) -> list[str]:
        return [
            f"{result.criterion_id}: {issue}"
            for result in self.results
            if not result.passed
            for issue in result.issues
        ]


def build_executable_success_contract(
    contract: ResearchContract,
) -> ExecutableSuccessContract:
    criteria = [
        ExecutableCriterion(
            criterion_id="SC-READY",
            description="The contract is promoted and has selected targets.",
            validator="validate_ready_contract",
            required_artifacts=["research-contract-final"],
        ),
        ExecutableCriterion(
            criterion_id="SC-EVIDENCE",
            description=(
                "Critical claims and selected targets resolve to verified, "
                "source-located Evidence IDs."
            ),
            validator="validate_evidence_traceability",
            required_artifacts=["research-contract-final"],
        ),
        ExecutableCriterion(
            criterion_id="SC-FALSIFIABLE",
            description="Every selected target has an explicit rejection condition.",
            validator="validate_falsification_contract",
            required_artifacts=["research-contract-final"],
        ),
        ExecutableCriterion(
            criterion_id="SC-INFORMATIVE",
            description=(
                "Positive, negative, and null outcomes all leave interpretable "
                "scientific information."
            ),
            validator="validate_informative_outcomes",
            required_artifacts=["research-contract-final"],
        ),
        ExecutableCriterion(
            criterion_id="SC-FEASIBLE",
            description="Data, compute, and a minimum executable experiment exist.",
            validator="validate_compute_feasibility",
            required_artifacts=["research-contract-final"],
        ),
        ExecutableCriterion(
            criterion_id="SC-PREDICTION-MATRIX",
            description=(
                "Every target is covered by controlled conditions and decision rules."
            ),
            validator="validate_prediction_matrix",
            required_artifacts=["research-contract-final"],
        ),
    ]
    if contract.research_mode in {
        ResearchMode.EXPLANATORY_RESEARCH,
        ResearchMode.HYBRID_RESEARCH,
    }:
        criteria.append(
            ExecutableCriterion(
                criterion_id="SC-DISTINCTIVE",
                description=(
                    "At least one controlled condition makes competing explanations "
                    "predict observably different outcomes."
                ),
                validator="validate_distinctive_predictions",
                required_artifacts=["research-contract-final"],
            )
        )
    if contract.research_profile == ResearchProfile.TRACE_AUDIT:
        criteria.append(
            ExecutableCriterion(
                criterion_id="SC-TRACE-AUDIT",
                description=(
                    "The frozen study separates C0-C3, covers registered fault "
                    "types, and binds every selected claim."
                ),
                validator="validate_trace_audit_protocol",
                required_artifacts=[
                    "research-contract-final",
                    "trace-study-contract",
                ],
            )
        )
    if contract.research_mode in {
        ResearchMode.DIRECT_TEST,
        ResearchMode.BENCHMARK_AUDIT,
        ResearchMode.HYBRID_RESEARCH,
    }:
        criteria.append(
            ExecutableCriterion(
                criterion_id="SC-CONCRETE",
                description=(
                    "Direct comparisons resolve placeholder entities into a concrete "
                    "optimizer mapping and executable workload."
                ),
                validator="validate_concrete_operationalization",
                required_artifacts=["research-contract-final"],
            )
        )
    return ExecutableSuccessContract(
        contract_version="1.0",
        source_research_contract_version=contract.contract_version,
        research_mode=contract.research_mode,
        criteria=criteria,
    )


Validator = Callable[[ResearchContract], list[str]]


def verify_success_contract(
    executable: ExecutableSuccessContract,
    research_contract: ResearchContract,
) -> SuccessContractReport:
    results: list[CriterionVerification] = []
    for criterion in executable.criteria:
        validator = VALIDATORS.get(criterion.validator)
        if validator is None:
            issues = [f"Unknown validator: {criterion.validator}"]
        else:
            issues = validator(research_contract)
        affected_target_ids = sorted(
            target_id
            for target_id in research_contract.selected_target_ids
            if any(target_id in issue for issue in issues)
        )
        if issues and not affected_target_ids:
            affected_target_ids = list(research_contract.selected_target_ids)
        results.append(
            CriterionVerification(
                criterion_id=criterion.criterion_id,
                validator=criterion.validator,
                passed=not issues,
                issues=issues,
                affected_target_ids=affected_target_ids,
            )
        )
    passed = all(
        result.passed
        for criterion, result in zip(executable.criteria, results)
        if criterion.hard_gate
    )
    return SuccessContractReport(
        contract_version=executable.contract_version,
        passed=passed,
        results=results,
    )


def validate_ready_contract(contract: ResearchContract) -> list[str]:
    issues: list[str] = []
    if contract.readiness == ResearchReadiness.PROPOSED:
        issues.append("Research Contract is still PROPOSED")
    known = {item.target_id for item in contract.targets}
    if not contract.selected_target_ids:
        issues.append("No research target was selected")
    unknown = set(contract.selected_target_ids) - known
    if unknown:
        issues.append(f"Unknown selected target IDs: {sorted(unknown)}")
    return issues


def validate_evidence_traceability(contract: ResearchContract) -> list[str]:
    issues: list[str] = []
    evidence = {item.evidence_id: item for item in contract.evidence}
    selected = {
        item.target_id: item
        for item in contract.targets
        if item.target_id in contract.selected_target_ids
    }
    references: dict[str, list[str]] = {}
    for claim in contract.claims:
        if claim.importance.upper() == "CRITICAL":
            references[f"claim {claim.claim_id}"] = claim.evidence_ids
    for target_id, target in selected.items():
        references[f"target {target_id}"] = target.evidence_ids
    for owner, evidence_ids in references.items():
        if not evidence_ids:
            issues.append(f"{owner} has no Evidence ID")
            continue
        for evidence_id in evidence_ids:
            unit = evidence.get(evidence_id)
            if unit is None:
                issues.append(f"{owner} references unknown Evidence ID {evidence_id}")
                continue
            location = unit.location
            anchored = bool(
                location.section.strip()
                or location.page is not None
                or location.paragraph is not None
                or location.sentence is not None
                or location.figure
                or location.table
                or location.equation
            )
            if not anchored:
                issues.append(f"Evidence {evidence_id} has no source location")
            if not unit.verbatim_excerpt.strip():
                issues.append(f"Evidence {evidence_id} has no exact excerpt")
            if unit.verification_status == VerificationStatus.UNVERIFIED:
                issues.append(f"Evidence {evidence_id} is UNVERIFIED")
    return list(dict.fromkeys(issues))


def validate_falsification_contract(contract: ResearchContract) -> list[str]:
    return [
        f"Target {item.target_id} has no falsification condition"
        for item in contract.targets
        if item.target_id in contract.selected_target_ids
        and not item.falsification_condition.strip()
    ]


def validate_informative_outcomes(contract: ResearchContract) -> list[str]:
    issues: list[str] = []
    for item in contract.targets:
        if item.target_id not in contract.selected_target_ids:
            continue
        for field, value in {
            "positive_result_value": item.positive_result_value,
            "negative_result_value": item.negative_result_value,
            "null_result_value": item.null_result_value,
        }.items():
            if not value.strip():
                issues.append(f"Target {item.target_id} is missing {field}")
    return issues


def validate_compute_feasibility(contract: ResearchContract) -> list[str]:
    issues: list[str] = []
    for item in contract.targets:
        if item.target_id not in contract.selected_target_ids:
            continue
        for field, value in {
            "minimum_experiment": item.minimum_experiment,
            "required_data": item.required_data,
            "compute_estimate": item.compute_estimate,
        }.items():
            if not value.strip():
                issues.append(f"Target {item.target_id} is missing {field}")
    return issues


def validate_prediction_matrix(contract: ResearchContract) -> list[str]:
    issues: list[str] = []
    covered: set[str] = set()
    known = {item.target_id for item in contract.targets}
    for condition in contract.prediction_matrix:
        if not condition.controlled_variables:
            issues.append(f"Condition {condition.condition_id} has no controls")
        if not condition.manipulated_variables:
            issues.append(f"Condition {condition.condition_id} has no manipulation")
        if not condition.measurement.strip() or not condition.decision_threshold.strip():
            issues.append(
                f"Condition {condition.condition_id} has no executable decision rule"
            )
        elif (
            contract.research_mode
            in {
                ResearchMode.DIRECT_TEST,
                ResearchMode.BENCHMARK_AUDIT,
                ResearchMode.HYBRID_RESEARCH,
            }
            and not re.search(r"\d", condition.decision_threshold)
        ):
            issues.append(
                f"Condition {condition.condition_id} has no numeric decision threshold"
            )
        for prediction in condition.predictions:
            if prediction.target_id in known:
                covered.add(prediction.target_id)
            if not prediction.expected_pattern.strip():
                issues.append(
                    f"Condition {condition.condition_id} has an empty prediction"
                )
    missing = set(contract.selected_target_ids) - covered
    if missing:
        issues.append(f"Selected targets without predictions: {sorted(missing)}")
    return issues


def validate_distinctive_predictions(contract: ResearchContract) -> list[str]:
    for condition in contract.prediction_matrix:
        patterns = {
            (
                item.direction.strip().casefold(),
                item.expected_pattern.strip().casefold(),
            )
            for item in condition.predictions
            if item.direction.strip() and item.expected_pattern.strip()
        }
        if len(patterns) >= 2:
            return []
    return [
        "No controlled condition produces observably different predictions across "
        "competing explanations"
    ]


def validate_concrete_operationalization(contract: ResearchContract) -> list[str]:
    placeholder = re.compile(r"\boptimizer\s+[ab]\b", re.IGNORECASE)
    explicit_mapping = {
        label: re.compile(
            rf"\boptimizer\s+{label}\s*(?:=|is|means|maps?\s+to|defined\s+as)\s*"
            r"(?!optimizer\s+[ab]\b)[A-Za-z][A-Za-z0-9_.+-]*",
            re.IGNORECASE,
        )
        for label in ("a", "b")
    }
    issues: list[str] = []
    selected = [
        item
        for item in contract.targets
        if item.target_id in contract.selected_target_ids
    ]
    for target in selected:
        if placeholder.search(target.statement):
            issues.append(
                f"Target {target.target_id} still uses optimizer A/B placeholders"
            )
    scope_has_complete_mapping = all(
        pattern.search(contract.scope) for pattern in explicit_mapping.values()
    )
    if placeholder.search(contract.scope) and not scope_has_complete_mapping:
        issues.append("Research scope still uses optimizer A/B placeholders")
    return issues


def validate_trace_audit_protocol(contract: ResearchContract) -> list[str]:
    if contract.research_profile != ResearchProfile.TRACE_AUDIT:
        return []
    if contract.trace_study_contract is None:
        return ["TRACE_AUDIT has no frozen Trace Study Contract"]
    issues = trace_study_contract_issues(
        contract.trace_study_contract,
        expected_claim_ids=set(contract.selected_target_ids),
    )
    if not contract.trace_tensions or not contract.selected_trace_tension_ids:
        issues.append("TRACE_AUDIT has no selected source-grounded tension")
    return issues


VALIDATORS: dict[str, Validator] = {
    "validate_ready_contract": validate_ready_contract,
    "validate_evidence_traceability": validate_evidence_traceability,
    "validate_falsification_contract": validate_falsification_contract,
    "validate_informative_outcomes": validate_informative_outcomes,
    "validate_compute_feasibility": validate_compute_feasibility,
    "validate_prediction_matrix": validate_prediction_matrix,
    "validate_distinctive_predictions": validate_distinctive_predictions,
    "validate_concrete_operationalization": validate_concrete_operationalization,
    "validate_trace_audit_protocol": validate_trace_audit_protocol,
}
