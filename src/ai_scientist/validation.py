from __future__ import annotations

import ast
import json
import re

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .config import Settings
from .schemas import (
    ComposerReport,
    ClaimLedger,
    ContaminationStatus,
    ContractComposerReport,
    DirectorOutput,
    EvaluatorReport,
    ExEvaluatorReport,
    ExecutionResult,
    ExperimentContract,
    ExperimentorOutput,
    PaperDraft,
    ResearchContract,
    ResearchMode,
    ResearchProfile,
    ResearchResultStatus,
    ReviewReport,
    SupportRelation,
    TraceAuditResultPayload,
    TraceReviewerDecisionBatch,
    TraceStudyContract,
    WorkflowAction,
)
from .trace_audit import trace_execution_issues, trace_study_contract_issues


@dataclass(slots=True)
class ValidationIssue:
    path: str
    code: str
    message: str
    repair_scope: str = "FIELD"
    fatal: bool = False


@dataclass(slots=True)
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.fatal for issue in self.issues)

    def add(
        self,
        path: str,
        code: str,
        message: str,
        *,
        repair_scope: str = "FIELD",
        fatal: bool = False,
    ) -> None:
        self.issues.append(
            ValidationIssue(path, code, message, repair_scope, fatal)
        )


def validate_director(output: DirectorOutput, settings: Settings) -> ValidationReport:
    report = ValidationReport()
    if not settings.min_hypotheses <= len(output.hypotheses) <= settings.max_hypotheses:
        report.add(
            "/hypotheses",
            "INVALID_CANDIDATE_COUNT",
            f"Expected {settings.min_hypotheses}-{settings.max_hypotheses} hypotheses",
            repair_scope="COLLECTION",
            fatal=True,
        )
    evidence_ids = {item.evidence_id for item in output.evidence}
    critical_claims = [claim for claim in output.claims if claim.importance == "CRITICAL"]
    for index, claim in enumerate(critical_claims):
        if not claim.evidence_ids:
            report.add(
                f"/claims/{index}/evidence_ids",
                "MISSING_CRITICAL_EVIDENCE",
                f"Critical claim {claim.claim_id} has no evidence",
                fatal=True,
            )
        if claim.support_relation in {
            SupportRelation.UNSUPPORTED,
            SupportRelation.UNVERIFIED,
        }:
            report.add(
                f"/claims/{index}/support_relation",
                "UNSUPPORTED_CRITICAL_CLAIM",
                f"Critical claim {claim.claim_id} is not verified",
                fatal=True,
            )
        unknown = set(claim.evidence_ids) - evidence_ids
        if unknown:
            report.add(
                f"/claims/{index}/evidence_ids",
                "UNKNOWN_EVIDENCE_ID",
                f"Unknown evidence IDs: {sorted(unknown)}",
                fatal=True,
            )
    for condition_index, condition in enumerate(output.prediction_matrix):
        patterns = {
            (item.direction.strip().lower(), item.expected_pattern.strip().lower())
            for item in condition.predictions
        }
        if len(patterns) < 2:
            report.add(
                f"/prediction_matrix/{condition_index}/predictions",
                "NON_DISCRIMINATING_PREDICTION",
                "All hypotheses predict the same observable pattern",
                repair_scope="CONDITION",
                fatal=True,
            )
        if not condition.controlled_variables or not condition.manipulated_variables:
            report.add(
                f"/prediction_matrix/{condition_index}",
                "MISSING_CONTROL_OR_MANIPULATION",
                "A discriminating condition needs controls and manipulations",
                repair_scope="CONDITION",
                fatal=True,
            )
    return report


def validate_research_contract(
    value: ResearchContract,
    *,
    expected_mode: ResearchMode,
    require_proposed: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    if value.research_mode != expected_mode:
        report.add(
            "/research_mode",
            "MODE_MISMATCH",
            f"Expected {expected_mode.value}, got {value.research_mode.value}",
            repair_scope="CONTRACT",
            fatal=True,
        )
    if require_proposed and (
        value.readiness.value != "PROPOSED" or value.selected_target_ids
    ):
        report.add(
            "/readiness",
            "DRAFT_ALREADY_DECIDED",
            "A Director draft must be PROPOSED with no selected target IDs",
            repair_scope="CONTRACT",
            fatal=True,
        )
    if not value.claim_ceiling.strip():
        report.add(
            "/claim_ceiling",
            "MISSING_CLAIM_CEILING",
            "Every research mode requires an explicit claim ceiling",
            fatal=True,
        )
    evidence_ids = {item.evidence_id for item in value.evidence}
    for index, claim in enumerate(value.claims):
        if claim.importance == "CRITICAL" and not claim.evidence_ids:
            report.add(
                f"/claims/{index}/evidence_ids",
                "MISSING_CRITICAL_EVIDENCE",
                f"Critical claim {claim.claim_id} has no evidence",
                fatal=True,
            )
        if claim.importance == "CRITICAL" and claim.support_relation in {
            SupportRelation.UNSUPPORTED,
            SupportRelation.UNVERIFIED,
        }:
            report.add(
                f"/claims/{index}/support_relation",
                "UNSUPPORTED_CRITICAL_CLAIM",
                f"Critical claim {claim.claim_id} is not verified",
                fatal=True,
            )
        if set(claim.evidence_ids) - evidence_ids:
            report.add(
                f"/claims/{index}/evidence_ids",
                "UNKNOWN_EVIDENCE_ID",
                f"Claim {claim.claim_id} references unknown evidence",
                fatal=True,
            )
    target_ids = {item.target_id for item in value.targets}
    covered_target_ids: set[str] = set()
    for condition_index, condition in enumerate(value.prediction_matrix):
        if not condition.controlled_variables or not condition.manipulated_variables:
            report.add(
                f"/prediction_matrix/{condition_index}",
                "MISSING_CONTROL_OR_MANIPULATION",
                "A research condition needs controls and manipulations",
                repair_scope="CONDITION",
                fatal=True,
            )
        if not condition.measurement.strip() or not condition.decision_threshold.strip():
            report.add(
                f"/prediction_matrix/{condition_index}",
                "MISSING_DECISION_RULE",
                "A research condition needs a measurement and decision threshold",
                repair_scope="CONDITION",
                fatal=True,
            )
        elif (
            value.research_mode
            in {
                ResearchMode.DIRECT_TEST,
                ResearchMode.BENCHMARK_AUDIT,
                ResearchMode.HYBRID_RESEARCH,
            }
            and not re.search(r"\d", condition.decision_threshold)
        ):
            report.add(
                f"/prediction_matrix/{condition_index}/decision_threshold",
                "NON_NUMERIC_DECISION_RULE",
                "A direct comparison needs a numeric minimum effect, tolerance, "
                "confidence level, or other predeclared numeric threshold",
                repair_scope="CONDITION",
                fatal=True,
            )
        covered_target_ids.update(item.target_id for item in condition.predictions)
        if value.research_mode in {
            ResearchMode.EXPLANATORY_RESEARCH,
            ResearchMode.HYBRID_RESEARCH,
        }:
            patterns = {
                (item.direction.strip().lower(), item.expected_pattern.strip().lower())
                for item in condition.predictions
            }
            if len(patterns) < 2:
                report.add(
                    f"/prediction_matrix/{condition_index}/predictions",
                    "NON_DISCRIMINATING_PREDICTION",
                    "Explanatory hypotheses need different observable predictions",
                    repair_scope="CONDITION",
                    fatal=True,
                )
    missing_targets = target_ids - covered_target_ids
    if missing_targets:
        report.add(
            "/prediction_matrix",
            "TARGET_WITHOUT_PREDICTION",
            f"Targets without a prediction: {sorted(missing_targets)}",
            repair_scope="COLLECTION",
            fatal=True,
        )
    for index, target in enumerate(value.targets):
        if not target.null_statement.strip():
            report.add(
                f"/targets/{index}/null_statement",
                "MISSING_NULL_STATEMENT",
                "Every research target needs an explicit null statement",
                fatal=True,
            )
        if not target.falsification_condition.strip():
            report.add(
                f"/targets/{index}/falsification_condition",
                "MISSING_FALSIFICATION_CONDITION",
                "Every research target needs a rejection condition",
                fatal=True,
            )
    if value.research_profile == ResearchProfile.TRACE_AUDIT:
        if value.trace_study_contract is None:
            report.add(
                "/trace_study_contract",
                "MISSING_TRACE_STUDY_CONTRACT",
                "TRACE_AUDIT requires a frozen C0-C3 study contract",
                fatal=True,
            )
        else:
            for issue in trace_study_contract_issues(
                value.trace_study_contract,
                expected_claim_ids=set(value.selected_target_ids),
            ):
                report.add(
                    "/trace_study_contract",
                    "INVALID_TRACE_STUDY_CONTRACT",
                    issue,
                    repair_scope="CONTRACT",
                    fatal=True,
                )
        if not value.trace_tensions or not value.selected_trace_tension_ids:
            report.add(
                "/trace_tensions",
                "MISSING_FROZEN_TRACE_TENSION",
                "TRACE_AUDIT requires source-grounded selected tension cards",
                fatal=True,
            )
    return report


def validate_evaluator(
    report_value: EvaluatorReport,
    *,
    required_criteria: set[str],
    evidence_ids: set[str],
    rubric_version: str,
    target_ids: set[str] | None = None,
    required_target_gates: set[str] | None = None,
    minimum_passing_score: int = 3,
) -> ValidationReport:
    report = ValidationReport()
    discovered_ids = [item.evidence_id for item in report_value.discovered_evidence]
    duplicate_discovered_ids = {
        evidence_id
        for evidence_id in discovered_ids
        if discovered_ids.count(evidence_id) > 1
    }
    if duplicate_discovered_ids:
        report.add(
            "/discovered_evidence",
            "DUPLICATE_DISCOVERED_EVIDENCE_ID",
            f"Duplicate discovered evidence IDs: {sorted(duplicate_discovered_ids)}",
            repair_scope="COLLECTION",
            fatal=True,
        )
    collisions = set(discovered_ids).intersection(evidence_ids)
    if collisions:
        report.add(
            "/discovered_evidence",
            "EVIDENCE_ID_COLLISION",
            f"Discovered evidence IDs collide with Director IDs: {sorted(collisions)}",
            repair_scope="COLLECTION",
            fatal=True,
        )
    known_evidence_ids = evidence_ids.union(discovered_ids)
    if report_value.rubric_version != rubric_version:
        report.add(
            "/rubric_version",
            "RUBRIC_VERSION_MISMATCH",
            f"Expected {rubric_version}, got {report_value.rubric_version}",
            repair_scope="EVALUATION",
            fatal=True,
        )
    present = {item.criterion for item in report_value.criteria}
    criterion_names = [item.criterion for item in report_value.criteria]
    duplicate_criteria = {
        name for name in criterion_names if criterion_names.count(name) > 1
    }
    if duplicate_criteria:
        report.add(
            "/criteria",
            "DUPLICATE_CRITERION",
            f"Duplicate criteria: {sorted(duplicate_criteria)}",
            repair_scope="COLLECTION",
            fatal=True,
        )
    for criterion in sorted(required_criteria - present):
        report.add(
            f"/criteria/{criterion}",
            "MISSING_CRITERION",
            f"Missing criterion: {criterion}",
            fatal=True,
        )
    unexpected_criteria = present - required_criteria
    if unexpected_criteria:
        report.add(
            "/criteria",
            "UNEXPECTED_CRITERION",
            f"Unexpected criteria: {sorted(unexpected_criteria)}",
            repair_scope="COLLECTION",
            fatal=True,
        )
    for index, criterion in enumerate(report_value.criteria):
        if not criterion.reason or not criterion.counterargument:
            report.add(
                f"/criteria/{index}",
                "UNSUPPORTED_SCORE",
                "Every score requires a reason and counterargument",
                fatal=True,
            )
        unknown = set(criterion.evidence_ids) - known_evidence_ids
        if unknown:
            report.add(
                f"/criteria/{index}/evidence_ids",
                "UNKNOWN_EVIDENCE_ID",
                f"Unknown evidence IDs: {sorted(unknown)}",
                fatal=True,
            )
        if criterion.fatal_issue and report_value.recommended_decision == "PROMOTE":
            report.add(
                f"/criteria/{index}/fatal_issue",
                "CRITERION_FATAL_PROMOTE_CONTRADICTION",
                "A fatal criterion is incompatible with a PROMOTE recommendation",
                repair_scope="DECISION",
                fatal=True,
            )
    if target_ids is not None:
        required_gates = required_target_gates or set()
        evaluation_ids = [item.target_id for item in report_value.target_evaluations]
        duplicate_targets = {
            target_id
            for target_id in evaluation_ids
            if evaluation_ids.count(target_id) > 1
        }
        if duplicate_targets:
            report.add(
                "/target_evaluations",
                "DUPLICATE_TARGET_EVALUATION",
                f"Duplicate target evaluations: {sorted(duplicate_targets)}",
                repair_scope="COLLECTION",
                fatal=True,
            )
        missing_targets = target_ids - set(evaluation_ids)
        unknown_targets = set(evaluation_ids) - target_ids
        if missing_targets:
            report.add(
                "/target_evaluations",
                "MISSING_TARGET_EVALUATION",
                f"Missing target evaluations: {sorted(missing_targets)}",
                repair_scope="COLLECTION",
                fatal=True,
            )
        if unknown_targets:
            report.add(
                "/target_evaluations",
                "UNKNOWN_TARGET_EVALUATION",
                f"Unknown target evaluations: {sorted(unknown_targets)}",
                repair_scope="COLLECTION",
                fatal=True,
            )
        for target_index, target in enumerate(report_value.target_evaluations):
            gate_names = [item.gate for item in target.gates]
            duplicate_gates = {
                gate for gate in gate_names if gate_names.count(gate) > 1
            }
            present_gates = set(gate_names)
            if duplicate_gates:
                report.add(
                    f"/target_evaluations/{target_index}/gates",
                    "DUPLICATE_TARGET_GATE",
                    f"Duplicate target gates: {sorted(duplicate_gates)}",
                    repair_scope="COLLECTION",
                    fatal=True,
                )
            missing_gates = required_gates - present_gates
            unexpected_gates = present_gates - required_gates
            if missing_gates:
                report.add(
                    f"/target_evaluations/{target_index}/gates",
                    "MISSING_TARGET_GATE",
                    f"Missing target gates: {sorted(missing_gates)}",
                    repair_scope="COLLECTION",
                    fatal=True,
                )
            if unexpected_gates:
                report.add(
                    f"/target_evaluations/{target_index}/gates",
                    "UNEXPECTED_TARGET_GATE",
                    f"Unexpected target gates: {sorted(unexpected_gates)}",
                    repair_scope="COLLECTION",
                    fatal=True,
                )
            for gate_index, gate in enumerate(target.gates):
                unknown = set(gate.evidence_ids) - known_evidence_ids
                if unknown:
                    report.add(
                        (
                            f"/target_evaluations/{target_index}/gates/"
                            f"{gate_index}/evidence_ids"
                        ),
                        "UNKNOWN_EVIDENCE_ID",
                        f"Unknown evidence IDs: {sorted(unknown)}",
                        fatal=True,
                    )
                expected_pass = (
                    gate.score >= minimum_passing_score and not gate.fatal_issue
                )
                if gate.passed != expected_pass:
                    report.add(
                        (
                            f"/target_evaluations/{target_index}/gates/"
                            f"{gate_index}/passed"
                        ),
                        "TARGET_GATE_PASS_MISMATCH",
                        (
                            f"passed must be {expected_pass} for score {gate.score} "
                            f"and fatal_issue={gate.fatal_issue}"
                        ),
                        repair_scope="FIELD",
                        fatal=True,
                    )
            if (
                target.recommended_decision == "PROMOTE"
                and (
                    target.fatal_issues
                    or any(not item.passed for item in target.gates)
                )
            ):
                report.add(
                    f"/target_evaluations/{target_index}/recommended_decision",
                    "TARGET_FATAL_PROMOTE_CONTRADICTION",
                    "A target with a failed gate cannot recommend PROMOTE",
                    repair_scope="DECISION",
                    fatal=True,
                )
    if report_value.fatal_issues and report_value.recommended_decision == "PROMOTE":
        report.add(
            "/recommended_decision",
            "FATAL_PROMOTE_CONTRADICTION",
            "A report with fatal issues cannot recommend PROMOTE",
            repair_scope="DECISION",
            fatal=True,
        )
    return report


def evaluator_allows_target_promotion(
    report: EvaluatorReport,
    target_id: str,
    *,
    hard_gate_criteria: set[str],
    required_target_gates: set[str],
    evidence_required_criteria: set[str] | None = None,
    evidence_required_target_gates: set[str] | None = None,
    minimum_passing_score: int = 3,
    require_report_hard_gates: bool = True,
) -> bool:
    if require_report_hard_gates and report.fatal_issues:
        return False
    criteria_by_name = {item.criterion: item for item in report.criteria}
    evidence_criteria = evidence_required_criteria or set()
    evidence_target_gates = evidence_required_target_gates or set()
    if require_report_hard_gates:
        for name in hard_gate_criteria:
            criterion = criteria_by_name.get(name)
            if (
                criterion is None
                or criterion.score < minimum_passing_score
                or criterion.fatal_issue
                or (name in evidence_criteria and not criterion.evidence_ids)
            ):
                return False
    target = next(
        (item for item in report.target_evaluations if item.target_id == target_id),
        None,
    )
    if target is None or target.fatal_issues:
        return False
    gates_by_name = {item.gate: item for item in target.gates}
    for name in required_target_gates:
        gate = gates_by_name.get(name)
        if (
            gate is None
            or gate.score < minimum_passing_score
            or gate.fatal_issue
            or not gate.passed
            or (name in evidence_target_gates and not gate.evidence_ids)
        ):
            return False
    return target.recommended_decision == "PROMOTE"


def validate_composer(
    value: ComposerReport,
    reports: list[EvaluatorReport],
    *,
    target_ids: set[str],
    evaluator_a_hard_criteria: set[str],
    evaluator_b_hard_criteria: set[str],
    evaluator_a_target_gates: set[str],
    evaluator_b_target_gates: set[str],
    evaluator_a_evidence_criteria: set[str] | None = None,
    evaluator_b_evidence_criteria: set[str] | None = None,
    evaluator_a_evidence_target_gates: set[str] | None = None,
    evaluator_b_evidence_target_gates: set[str] | None = None,
    minimum_passing_score: int = 3,
) -> ValidationReport:
    report = ValidationReport()
    unknown = set(value.promoted_hypothesis_ids) - target_ids
    if unknown:
        report.add(
            "/promoted_hypothesis_ids",
            "UNKNOWN_PROMOTED_TARGET",
            f"Unknown promoted target IDs: {sorted(unknown)}",
            fatal=True,
        )
    if any(item.fatal_issues for item in reports) and value.action == WorkflowAction.PROMOTE:
        report.add(
            "/action",
            "FATAL_PROMOTION",
            "Composer cannot promote while an evaluator has unresolved fatal issues",
            repair_scope="DECISION",
            fatal=True,
        )
    if value.action == WorkflowAction.PROMOTE and not value.promoted_hypothesis_ids:
        report.add(
            "/promoted_hypothesis_ids",
            "EMPTY_PROMOTION",
            "PROMOTE requires at least one hypothesis",
            fatal=True,
        )
    if len(reports) == 2:
        evaluator_a, evaluator_b = reports
        for target_id in value.promoted_hypothesis_ids:
            if target_id not in target_ids:
                continue
            if not evaluator_allows_target_promotion(
                evaluator_a,
                target_id,
                hard_gate_criteria=evaluator_a_hard_criteria,
                required_target_gates=evaluator_a_target_gates,
                evidence_required_criteria=evaluator_a_evidence_criteria,
                evidence_required_target_gates=(
                    evaluator_a_evidence_target_gates
                ),
                minimum_passing_score=minimum_passing_score,
                require_report_hard_gates=(
                    value.action == WorkflowAction.PROMOTE
                ),
            ) or not evaluator_allows_target_promotion(
                evaluator_b,
                target_id,
                hard_gate_criteria=evaluator_b_hard_criteria,
                required_target_gates=evaluator_b_target_gates,
                evidence_required_criteria=evaluator_b_evidence_criteria,
                evidence_required_target_gates=(
                    evaluator_b_evidence_target_gates
                ),
                minimum_passing_score=minimum_passing_score,
                require_report_hard_gates=(
                    value.action == WorkflowAction.PROMOTE
                ),
            ):
                report.add(
                    "/promoted_hypothesis_ids",
                    "TARGET_HARD_GATE_FAILED",
                    f"Target {target_id} did not pass both evaluators' hard gates",
                    repair_scope="DECISION",
                    fatal=True,
                )
    return report


def validate_contract_composer(
    value: ContractComposerReport,
    reports: list[EvaluatorReport],
    *,
    target_ids: set[str],
    evaluator_a_hard_criteria: set[str],
    evaluator_b_hard_criteria: set[str],
    evaluator_a_target_gates: set[str],
    evaluator_b_target_gates: set[str],
    evaluator_a_evidence_criteria: set[str] | None = None,
    evaluator_b_evidence_criteria: set[str] | None = None,
    evaluator_a_evidence_target_gates: set[str] | None = None,
    evaluator_b_evidence_target_gates: set[str] | None = None,
    minimum_passing_score: int = 3,
) -> ValidationReport:
    report = ValidationReport()
    unknown = set(value.promoted_target_ids) - target_ids
    if unknown:
        report.add(
            "/promoted_target_ids",
            "UNKNOWN_PROMOTED_TARGET",
            f"Unknown promoted target IDs: {sorted(unknown)}",
            fatal=True,
        )
    if any(item.fatal_issues for item in reports) and value.action == WorkflowAction.PROMOTE:
        report.add(
            "/action",
            "FATAL_PROMOTION",
            "Composer cannot promote while an evaluator has unresolved fatal issues",
            repair_scope="DECISION",
            fatal=True,
        )
    if value.action == WorkflowAction.PROMOTE and not value.promoted_target_ids:
        report.add(
            "/promoted_target_ids",
            "EMPTY_PROMOTION",
            "PROMOTE requires at least one research target",
            fatal=True,
        )
    if len(reports) == 2:
        evaluator_a, evaluator_b = reports
        for target_id in value.promoted_target_ids:
            if target_id not in target_ids:
                continue
            if not evaluator_allows_target_promotion(
                evaluator_a,
                target_id,
                hard_gate_criteria=evaluator_a_hard_criteria,
                required_target_gates=evaluator_a_target_gates,
                evidence_required_criteria=evaluator_a_evidence_criteria,
                evidence_required_target_gates=(
                    evaluator_a_evidence_target_gates
                ),
                minimum_passing_score=minimum_passing_score,
                require_report_hard_gates=(
                    value.action == WorkflowAction.PROMOTE
                ),
            ) or not evaluator_allows_target_promotion(
                evaluator_b,
                target_id,
                hard_gate_criteria=evaluator_b_hard_criteria,
                required_target_gates=evaluator_b_target_gates,
                evidence_required_criteria=evaluator_b_evidence_criteria,
                evidence_required_target_gates=(
                    evaluator_b_evidence_target_gates
                ),
                minimum_passing_score=minimum_passing_score,
                require_report_hard_gates=(
                    value.action == WorkflowAction.PROMOTE
                ),
            ):
                report.add(
                    "/promoted_target_ids",
                    "TARGET_HARD_GATE_FAILED",
                    f"Target {target_id} did not pass both evaluators' hard gates",
                    repair_scope="DECISION",
                    fatal=True,
                )
    return report


def validate_experimentor(
    value: ExperimentorOutput,
    *,
    selected_target_ids: set[str] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    path_list = [item.path for item in value.files]
    file_paths = set(path_list)
    if len(path_list) != len(file_paths):
        report.add(
            "/files",
            "DUPLICATE_GENERATED_PATH",
            "Generated file paths must be unique",
            fatal=True,
        )
    if selected_target_ids is not None and value.hypothesis_id not in selected_target_ids:
        report.add(
            "/hypothesis_id",
            "UNKNOWN_EXPERIMENT_TARGET",
            f"Unknown experiment target: {value.hypothesis_id}",
            fatal=True,
        )
    if value.entrypoint not in file_paths:
        report.add(
            "/entrypoint",
            "MISSING_ENTRYPOINT",
            "The entrypoint must be one of the generated files",
            fatal=True,
        )
    if not value.entrypoint.endswith(".py"):
        report.add(
            "/entrypoint",
            "NON_PYTHON_ENTRYPOINT",
            "Only Python entrypoints are allowed",
            fatal=True,
        )
    for index, generated in enumerate(value.files):
        if not generated.path.endswith(".py"):
            continue
        try:
            ast.parse(generated.content, filename=generated.path)
        except SyntaxError as exc:
            report.add(
                f"/files/{index}/content",
                "INVALID_PYTHON_SYNTAX",
                f"Invalid Python in {generated.path}: {exc.msg} at line {exc.lineno}",
                fatal=True,
            )
    if (
        not value.expected_result_file.strip()
        or value.expected_result_file == value.entrypoint
        or value.expected_result_file.startswith(("/", "\\"))
        or ".." in value.expected_result_file.replace("\\", "/").split("/")
    ):
        report.add(
            "/expected_result_file",
            "INVALID_EXPECTED_RESULT_PATH",
            "expected_result_file must be a safe relative output path",
            fatal=True,
        )
    return report


def validate_experiment_contract(
    value: ExperimentContract,
    *,
    selected_target_ids: set[str],
    expected_trace_study_contract: TraceStudyContract | None = None,
) -> ValidationReport:
    report = ValidationReport()
    contract_ids = value.hypothesis_ids
    if len(contract_ids) != len(set(contract_ids)):
        report.add(
            "/hypothesis_ids",
            "DUPLICATE_TARGET_ID",
            "Experiment contract target IDs must be unique",
            fatal=True,
        )
    if set(contract_ids) != selected_target_ids:
        report.add(
            "/hypothesis_ids",
            "TARGET_SET_MISMATCH",
            (
                f"Expected targets {sorted(selected_target_ids)}, "
                f"got {sorted(set(contract_ids))}"
            ),
            repair_scope="CONTRACT",
            fatal=True,
        )
    spec_ids = [item.hypothesis_id for item in value.hypothesis_specs]
    if len(spec_ids) != len(set(spec_ids)):
        report.add(
            "/hypothesis_specs",
            "DUPLICATE_TARGET_SPEC",
            "Each target must have exactly one experiment specification",
            fatal=True,
        )
    if set(spec_ids) != selected_target_ids:
        report.add(
            "/hypothesis_specs",
            "TARGET_SPEC_SET_MISMATCH",
            "Experiment specifications must cover exactly the selected targets",
            fatal=True,
        )
    if not value.seeds or len(value.seeds) != len(set(value.seeds)):
        report.add(
            "/seeds",
            "INVALID_SEEDS",
            "The frozen contract needs at least one unique deterministic seed",
            fatal=True,
        )
    required_text = {
        "/dataset_plan": value.dataset_plan,
        "/statistical_plan": value.statistical_plan,
        "/stopping_rule": value.stopping_rule,
    }
    for path, text in required_text.items():
        if not text.strip():
            report.add(
                path,
                "MISSING_PROTOCOL_FIELD",
                f"{path} cannot be empty",
                fatal=True,
            )
    if not value.metrics or not value.shared_protocol:
        report.add(
            "/shared_protocol",
            "INCOMPLETE_SHARED_PROTOCOL",
            "Metrics and shared protocol must be non-empty",
            fatal=True,
        )
    for index, spec in enumerate(value.hypothesis_specs):
        fields = {
            "unique_prediction": spec.unique_prediction,
            "manipulation": spec.manipulation,
            "measurement": spec.measurement,
            "expected_pattern": spec.expected_pattern,
            "rejection_condition": spec.rejection_condition,
        }
        if not spec.controls:
            report.add(
                f"/hypothesis_specs/{index}/controls",
                "MISSING_TARGET_CONTROLS",
                "Every target experiment needs explicit controls",
                fatal=True,
            )
        for name, text in fields.items():
            if not text.strip():
                report.add(
                    f"/hypothesis_specs/{index}/{name}",
                    "MISSING_TARGET_PROTOCOL_FIELD",
                    f"Target experiment field {name} cannot be empty",
                    fatal=True,
                )
    if expected_trace_study_contract is not None:
        if value.trace_study_contract is None:
            report.add(
                "/trace_study_contract",
                "TRACE_CONTRACT_NOT_COPIED",
                "Experiment Designer must copy the frozen Trace Study Contract",
                fatal=True,
            )
        elif value.trace_study_contract != expected_trace_study_contract:
            report.add(
                "/trace_study_contract",
                "TRACE_CONTRACT_DRIFT",
                "Experiment Designer changed the frozen Trace Study Contract",
                repair_scope="CONTRACT",
                fatal=True,
            )
    elif value.trace_study_contract is not None:
        report.add(
            "/trace_study_contract",
            "UNEXPECTED_TRACE_CONTRACT",
            "A general experiment cannot introduce a TRACE_AUDIT contract",
            fatal=True,
        )
    return report


def validate_execution_bundle(
    *,
    selected_target_ids: set[str],
    experimentor_outputs: list[ExperimentorOutput],
    executions: list[ExecutionResult],
    expected_trace_study_contract: TraceStudyContract | None = None,
    expected_trace_reviewer_decisions: TraceReviewerDecisionBatch | None = None,
    pipeline_smoke_test: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    outputs_by_target = {item.hypothesis_id: item for item in experimentor_outputs}
    executions_by_target = {item.hypothesis_id: item for item in executions}
    if len(outputs_by_target) != len(experimentor_outputs):
        report.add(
            "/experimentor_outputs",
            "DUPLICATE_EXPERIMENTOR_OUTPUT",
            "Each selected target must have one Experimentor output",
            fatal=True,
        )
    if len(executions_by_target) != len(executions):
        report.add(
            "/executions",
            "DUPLICATE_EXECUTION",
            "Each selected target must have one execution result",
            fatal=True,
        )
    missing_outputs = selected_target_ids - set(outputs_by_target)
    missing_executions = selected_target_ids - set(executions_by_target)
    unknown_outputs = set(outputs_by_target) - selected_target_ids
    unknown_executions = set(executions_by_target) - selected_target_ids
    for path, code, ids in (
        ("/experimentor_outputs", "MISSING_EXPERIMENTOR_OUTPUT", missing_outputs),
        ("/executions", "MISSING_EXECUTION", missing_executions),
        ("/experimentor_outputs", "UNKNOWN_EXPERIMENTOR_OUTPUT", unknown_outputs),
        ("/executions", "UNKNOWN_EXECUTION", unknown_executions),
    ):
        if ids:
            report.add(
                path,
                code,
                f"Affected target IDs: {sorted(ids)}",
                fatal=True,
            )
    for target_id in selected_target_ids.intersection(executions_by_target):
        execution = executions_by_target[target_id]
        output = outputs_by_target.get(target_id)
        if execution.exit_code != 0 or execution.timed_out:
            report.add(
                f"/executions/{target_id}",
                "EXECUTION_FAILED",
                (
                    f"exit_code={execution.exit_code}, "
                    f"timed_out={execution.timed_out}"
                ),
                fatal=True,
            )
        if output is None:
            continue
        expected_path = output.expected_result_file
        if expected_path not in execution.output_files:
            report.add(
                f"/executions/{target_id}/output_files",
                "EXPECTED_RESULT_MISSING",
                f"Missing expected result file: {expected_path}",
                fatal=True,
            )
        expected_result_id = f"{execution.experiment_id}:{expected_path}"
        if expected_result_id not in execution.result_ids:
            report.add(
                f"/executions/{target_id}/result_ids",
                "EXPECTED_RESULT_ID_MISSING",
                f"Missing canonical result ID: {expected_result_id}",
                fatal=True,
            )
        if (
            expected_trace_study_contract is not None
            and expected_path in execution.output_files
        ):
            try:
                trace_payload = json.loads(execution.output_files[expected_path])
            except (json.JSONDecodeError, TypeError):
                report.add(
                    f"/executions/{target_id}/output_files/{expected_path}",
                    "INVALID_TRACE_RESULT_JSON",
                    "TRACE_AUDIT result must be a JSON object",
                    fatal=True,
                )
                continue
            if not isinstance(trace_payload, dict):
                report.add(
                    f"/executions/{target_id}/output_files/{expected_path}",
                    "INVALID_TRACE_RESULT_JSON",
                    "TRACE_AUDIT result must be a JSON object",
                    fatal=True,
                )
                continue
            for issue in trace_execution_issues(
                trace_payload,
                expected_trace_study_contract,
                expected_target_id=target_id,
                pipeline_smoke_test=pipeline_smoke_test,
            ):
                report.add(
                    f"/executions/{target_id}/output_files/{expected_path}",
                    "TRACE_RESULT_HARD_GATE_FAILED",
                    issue,
                    repair_scope="EXPERIMENT",
                    fatal=True,
                )
            if expected_trace_reviewer_decisions is not None:
                try:
                    observed = TraceAuditResultPayload.model_validate(trace_payload)
                except ValueError:
                    continue
                expected_rows = sorted(
                    (
                        item.model_dump(mode="json")
                        for item in expected_trace_reviewer_decisions.decisions
                    ),
                    key=lambda item: (
                        item["reviewer_model"],
                        item["case_id"],
                        item["condition_id"],
                    ),
                )
                observed_rows = sorted(
                    (item.model_dump(mode="json") for item in observed.decisions),
                    key=lambda item: (
                        item["reviewer_model"],
                        item["case_id"],
                        item["condition_id"],
                    ),
                )
                if expected_rows != observed_rows:
                    report.add(
                        f"/executions/{target_id}/output_files/{expected_path}",
                        "TRACE_EXTERNAL_DECISION_DRIFT",
                        "Executed analysis changed, omitted, or invented frozen external reviewer decisions",
                        repair_scope="EXPERIMENT",
                        fatal=True,
                    )
                if (
                    observed.corruption_manifest_hash
                    != expected_trace_reviewer_decisions.corruption_manifest_hash
                ):
                    report.add(
                        f"/executions/{target_id}/output_files/{expected_path}",
                        "TRACE_MANIFEST_HASH_DRIFT",
                        "Executed analysis changed the frozen corruption manifest hash",
                        repair_scope="EXPERIMENT",
                        fatal=True,
                    )
    return report


def validate_ex_evaluator(
    value: ExEvaluatorReport,
    *,
    required_criteria: set[str],
    hard_gate_criteria: set[str],
    selected_target_ids: set[str],
    experimentor_outputs: list[ExperimentorOutput],
    executions: list[ExecutionResult],
    rubric_version: str,
    minimum_passing_score: int = 3,
    significant_degradation: bool = False,
    expected_trace_study_contract: TraceStudyContract | None = None,
    pipeline_smoke_test: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    if value.rubric_version != rubric_version:
        report.add(
            "/rubric_version",
            "RUBRIC_VERSION_MISMATCH",
            f"Expected {rubric_version}, got {value.rubric_version}",
            fatal=True,
        )
    criterion_names = [item.criterion for item in value.criteria]
    present = set(criterion_names)
    duplicate_criteria = {
        name for name in criterion_names if criterion_names.count(name) > 1
    }
    missing_criteria = required_criteria - present
    unexpected_criteria = present - required_criteria
    if duplicate_criteria:
        report.add(
            "/criteria",
            "DUPLICATE_CRITERION",
            f"Duplicate criteria: {sorted(duplicate_criteria)}",
            fatal=True,
        )
    if missing_criteria:
        report.add(
            "/criteria",
            "MISSING_CRITERION",
            f"Missing criteria: {sorted(missing_criteria)}",
            fatal=True,
        )
    if unexpected_criteria:
        report.add(
            "/criteria",
            "UNEXPECTED_CRITERION",
            f"Unexpected criteria: {sorted(unexpected_criteria)}",
            fatal=True,
        )
    known_trace_ids: set[str] = set()
    known_result_ids: set[str] = set()
    for execution in executions:
        known_result_ids.update(execution.result_ids)
        known_trace_ids.update(execution.result_ids)
        known_trace_ids.add(execution.experiment_id)
        known_trace_ids.add(execution.code_hash)
    for index, criterion in enumerate(value.criteria):
        if not criterion.reason or not criterion.counterargument:
            report.add(
                f"/criteria/{index}",
                "UNSUPPORTED_SCORE",
                "Every score requires a reason and counterargument",
                fatal=True,
            )
        if not criterion.evidence_ids:
            report.add(
                f"/criteria/{index}/evidence_ids",
                "MISSING_RESULT_TRACE",
                "Every experiment score requires a result or execution trace ID",
                fatal=True,
            )
        unknown = set(criterion.evidence_ids) - known_trace_ids
        if unknown:
            report.add(
                f"/criteria/{index}/evidence_ids",
                "UNKNOWN_RESULT_TRACE",
                f"Unknown result or execution trace IDs: {sorted(unknown)}",
                fatal=True,
            )
    judgment_ids = [item.hypothesis_id for item in value.judgments]
    if len(judgment_ids) != len(set(judgment_ids)):
        report.add(
            "/judgments",
            "DUPLICATE_TARGET_JUDGMENT",
            "Each selected target must have one result judgment",
            fatal=True,
        )
    missing_judgments = selected_target_ids - set(judgment_ids)
    unknown_judgments = set(judgment_ids) - selected_target_ids
    if missing_judgments:
        report.add(
            "/judgments",
            "MISSING_TARGET_JUDGMENT",
            f"Missing judgments: {sorted(missing_judgments)}",
            fatal=True,
        )
    if unknown_judgments:
        report.add(
            "/judgments",
            "UNKNOWN_TARGET_JUDGMENT",
            f"Unknown judgments: {sorted(unknown_judgments)}",
            fatal=True,
        )
    for index, judgment in enumerate(value.judgments):
        unknown = set(judgment.result_ids) - known_result_ids
        if unknown:
            report.add(
                f"/judgments/{index}/result_ids",
                "UNKNOWN_RESULT_ID",
                f"Unknown result IDs: {sorted(unknown)}",
                fatal=True,
            )
        if value.action == WorkflowAction.PASS and not judgment.result_ids:
            report.add(
                f"/judgments/{index}/result_ids",
                "PASS_WITHOUT_RESULT",
                "PASS requires traceable results for every selected target",
                fatal=True,
            )
    affected = set(value.affected_hypothesis_ids)
    if affected - selected_target_ids:
        report.add(
            "/affected_hypothesis_ids",
            "UNKNOWN_AFFECTED_TARGET",
            f"Unknown affected targets: {sorted(affected - selected_target_ids)}",
            fatal=True,
        )
    failure_note_ids = [item.target_id for item in value.failure_notebook]
    if len(failure_note_ids) != len(set(failure_note_ids)):
        report.add(
            "/failure_notebook",
            "DUPLICATE_EXPERIMENT_FAILURE_NOTE",
            "Each affected experiment target may have only one failure note",
            fatal=True,
        )
    unknown_failure_notes = set(failure_note_ids) - selected_target_ids
    if unknown_failure_notes:
        report.add(
            "/failure_notebook",
            "UNKNOWN_EXPERIMENT_FAILURE_NOTE_TARGET",
            f"Unknown failure-note targets: {sorted(unknown_failure_notes)}",
            fatal=True,
        )
    contamination_ids = {
        item.hypothesis_id for item in value.contamination_by_experimentor
    }
    if contamination_ids != selected_target_ids:
        report.add(
            "/contamination_by_experimentor",
            "CONTAMINATION_COVERAGE_MISMATCH",
            "Contamination status must cover every selected Experimentor exactly",
            fatal=True,
        )
    contamination_statuses = {
        item.status for item in value.contamination_by_experimentor
    }
    if significant_degradation and contamination_statuses == {
        ContaminationStatus.CLEAN
    }:
        report.add(
            "/contamination_by_experimentor",
            "DEGRADATION_MARKED_CLEAN",
            (
                "A significant score degradation requires VALID_DOWNGRADE, "
                "REGRESSION, or CONTAMINATED classification"
            ),
            repair_scope="DECISION",
            fatal=True,
        )
    if (
        value.best_supported_hypothesis_id is not None
        and value.best_supported_hypothesis_id not in selected_target_ids
    ):
        report.add(
            "/best_supported_hypothesis_id",
            "UNKNOWN_BEST_SUPPORTED_TARGET",
            "best_supported_hypothesis_id must identify a selected target",
            fatal=True,
        )
    allowed_actions = {
        WorkflowAction.PASS,
        WorkflowAction.RERUN,
        WorkflowAction.REPAIR,
        WorkflowAction.ADD_CONTROL,
        WorkflowAction.RETURN_TO_HYPOTHESIS,
    }
    if value.action not in allowed_actions:
        report.add(
            "/action",
            "INVALID_EX_EVALUATOR_ACTION",
            f"Unsupported Ex-Evaluator action: {value.action.value}",
            fatal=True,
        )
    if value.action == WorkflowAction.PASS and any(
        judgment.status == ResearchResultStatus.PROTOCOL_VIOLATION
        for judgment in value.judgments
    ):
        report.add(
            "/action",
            "PASS_WITH_PROTOCOL_VIOLATION",
            "PASS is incompatible with a protocol violation",
            repair_scope="DECISION",
            fatal=True,
        )
    execution_validation = validate_execution_bundle(
        selected_target_ids=selected_target_ids,
        experimentor_outputs=experimentor_outputs,
        executions=executions,
        expected_trace_study_contract=expected_trace_study_contract,
        pipeline_smoke_test=pipeline_smoke_test,
    )
    if value.action == WorkflowAction.PASS:
        for issue in execution_validation.issues:
            report.issues.append(issue)
        criteria_by_name = {item.criterion: item for item in value.criteria}
        failed_hard_gates = [
            name
            for name in hard_gate_criteria
            if (
                name not in criteria_by_name
                or criteria_by_name[name].score < minimum_passing_score
                or criteria_by_name[name].fatal_issue
            )
        ]
        if failed_hard_gates:
            report.add(
                "/criteria",
                "PASS_HARD_GATE_FAILED",
                f"Failed experiment hard gates: {sorted(failed_hard_gates)}",
                repair_scope="DECISION",
                fatal=True,
            )
        if affected:
            report.add(
                "/affected_hypothesis_ids",
                "PASS_WITH_AFFECTED_TARGETS",
                "PASS requires no targets awaiting repair or rerun",
                repair_scope="DECISION",
                fatal=True,
            )
        if contamination_statuses.intersection(
            {ContaminationStatus.REGRESSION, ContaminationStatus.CONTAMINATED}
        ):
            report.add(
                "/contamination_by_experimentor",
                "PASS_WITH_CONTAMINATED_EXPERIMENTOR",
                "PASS is incompatible with regression or contamination",
                repair_scope="DECISION",
                fatal=True,
            )
    elif value.action in {
        WorkflowAction.RERUN,
        WorkflowAction.REPAIR,
        WorkflowAction.ADD_CONTROL,
    }:
        if not affected:
            report.add(
                "/affected_hypothesis_ids",
                "REPAIR_WITHOUT_TARGET",
                "A repair/rerun action must identify affected targets",
                repair_scope="DECISION",
                fatal=True,
            )
        if set(failure_note_ids) != affected:
            report.add(
                "/failure_notebook",
                "EXPERIMENT_REPAIR_NOTE_COVERAGE",
                "Repair targets and individual failure-note targets must match exactly",
                repair_scope="COLLECTION",
                fatal=True,
            )
    return report


def validate_paper_draft(
    value: PaperDraft,
    *,
    expected_mode: ResearchMode,
    expected_claim_ceiling: str,
    evidence_ids: set[str],
    result_ids: set[str],
    negative_results_required: bool,
    result_payloads: dict[str, Any] | None = None,
    expected_profile: ResearchProfile = ResearchProfile.GENERAL,
    claim_ledger: ClaimLedger | None = None,
) -> ValidationReport:
    report = ValidationReport()
    if value.research_mode != expected_mode:
        report.add(
            "/research_mode",
            "PAPER_MODE_MISMATCH",
            f"Expected {expected_mode.value}, got {value.research_mode.value}",
            fatal=True,
        )
    if value.research_profile != expected_profile:
        report.add(
            "/research_profile",
            "PAPER_PROFILE_MISMATCH",
            f"Expected {expected_profile.value}, got {value.research_profile.value}",
            fatal=True,
        )
    if value.claim_ceiling != expected_claim_ceiling:
        report.add(
            "/claim_ceiling",
            "CLAIM_CEILING_CHANGED",
            "Writer must copy the frozen claim ceiling exactly",
            fatal=True,
        )
    if not value.title.strip() or not value.abstract.strip() or not value.markdown.strip():
        report.add(
            "/markdown",
            "INCOMPLETE_PAPER_DRAFT",
            "Title, abstract, and paper Markdown must be non-empty",
            fatal=True,
        )
    claim_ids = [item.claim_id for item in value.linked_claims]
    if len(claim_ids) != len(set(claim_ids)):
        report.add(
            "/linked_claims",
            "DUPLICATE_PAPER_CLAIM_ID",
            "Every linked paper claim needs a unique claim_id",
            fatal=True,
        )
    if not value.linked_claims:
        report.add(
            "/linked_claims",
            "MISSING_LINKED_CLAIMS",
            "A paper must expose its traceable claims",
            fatal=True,
        )
    for index, claim in enumerate(value.linked_claims):
        if not claim.evidence_ids and not claim.result_ids:
            report.add(
                f"/linked_claims/{index}",
                "UNTRACEABLE_PAPER_CLAIM",
                "Every paper claim needs an Evidence ID or Result ID",
                fatal=True,
            )
        unknown_evidence = set(claim.evidence_ids) - evidence_ids
        unknown_results = set(claim.result_ids) - result_ids
        if unknown_evidence:
            report.add(
                f"/linked_claims/{index}/evidence_ids",
                "UNKNOWN_PAPER_EVIDENCE_ID",
                f"Unknown Evidence IDs: {sorted(unknown_evidence)}",
                fatal=True,
            )
        if unknown_results:
            report.add(
                f"/linked_claims/{index}/result_ids",
                "UNKNOWN_PAPER_RESULT_ID",
                f"Unknown Result IDs: {sorted(unknown_results)}",
                fatal=True,
            )
    reference_ids = [item.evidence_id for item in value.references]
    if len(reference_ids) != len(set(reference_ids)):
        report.add(
            "/references",
            "DUPLICATE_PAPER_REFERENCE",
            "Each Evidence ID may have only one paper reference entry",
            fatal=True,
        )
    unknown_references = set(reference_ids) - evidence_ids
    if unknown_references:
        report.add(
            "/references",
            "UNKNOWN_PAPER_REFERENCE",
            f"Unknown paper reference IDs: {sorted(unknown_references)}",
            fatal=True,
        )
    used_evidence_ids = {
        evidence_id
        for claim in value.linked_claims
        for evidence_id in claim.evidence_ids
    }
    missing_references = used_evidence_ids - set(reference_ids)
    if missing_references:
        report.add(
            "/references",
            "MISSING_PAPER_REFERENCE",
            f"Missing rendered reference entries: {sorted(missing_references)}",
            fatal=True,
        )
    for index, reference in enumerate(value.references):
        if not reference.title.strip() or not reference.url.strip():
            report.add(
                f"/references/{index}",
                "INCOMPLETE_PAPER_REFERENCE",
                "Every reference needs a title and source URL",
                fatal=True,
            )
    for result_id, message in _paper_contrast_direction_issues(
        value.markdown,
        result_payloads or {},
    ):
        report.add(
            "/markdown",
            "RESULT_DIRECTION_CONTRADICTION",
            f"{result_id}: {message}",
            repair_scope="CLAIM",
            fatal=True,
        )
    if negative_results_required and not value.disclosed_negative_results:
        report.add(
            "/disclosed_negative_results",
            "NEGATIVE_RESULTS_OMITTED",
            "Negative, falsified, or inconclusive outcomes must be disclosed",
            fatal=True,
        )
    if expected_profile == ResearchProfile.TRACE_AUDIT:
        abstract_sentences = [
            item
            for item in re.split(r"(?<=[.!?])\s+", value.abstract.strip())
            if item.strip()
        ]
        if not 4 <= len(abstract_sentences) <= 6:
            report.add(
                "/abstract",
                "TRACE_ABSTRACT_LENGTH",
                "TRACE_AUDIT short-paper abstract must contain 4-6 sentences",
                fatal=True,
            )
        anonymous_text = "\n".join(
            [value.title, value.abstract, value.markdown]
        )
        if re.search(
            r"\\(?:icml)?(?:author|affiliation)\s*\{|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            anonymous_text,
            flags=re.IGNORECASE,
        ):
            report.add(
                "/markdown",
                "DOUBLE_BLIND_VIOLATION",
                "TRACE_AUDIT short paper contains author, affiliation, or email metadata",
                fatal=True,
            )
        if claim_ledger is None:
            report.add(
                "/linked_claims",
                "MISSING_CLAIM_LEDGER",
                "TRACE_AUDIT paper validation requires the frozen Claim Ledger",
                fatal=True,
            )
        else:
            ledger_by_id = {item.claim_id: item for item in claim_ledger.entries}
            paper_by_id = {item.claim_id: item for item in value.linked_claims}
            if set(paper_by_id) != set(ledger_by_id):
                report.add(
                    "/linked_claims",
                    "CLAIM_LEDGER_COVERAGE_MISMATCH",
                    "TRACE_AUDIT paper claims must cover the Claim Ledger exactly",
                    fatal=True,
                )
            for claim_id, paper_claim in paper_by_id.items():
                ledger_entry = ledger_by_id.get(claim_id)
                if ledger_entry is None:
                    continue
                if paper_claim.claim.strip() != ledger_entry.allowed_claim.strip():
                    report.add(
                        f"/linked_claims/{claim_id}/claim",
                        "CLAIM_LEDGER_TEXT_DRIFT",
                        f"Paper claim {claim_id} must copy its allowed ledger claim exactly",
                        fatal=True,
                    )
                if ledger_entry.effect_summary not in value.markdown:
                    report.add(
                        "/markdown",
                        "CLAIM_LEDGER_EFFECT_OMITTED",
                        f"Paper omits the verified effect summary for {claim_id}",
                        fatal=True,
                    )
                if not set(paper_claim.result_ids).issubset(
                    set(ledger_entry.result_ids)
                ):
                    report.add(
                        f"/linked_claims/{claim_id}/result_ids",
                        "CLAIM_LEDGER_RESULT_DRIFT",
                        f"Paper claim {claim_id} uses a Result ID outside its ledger entry",
                        fatal=True,
                    )
                if not set(paper_claim.evidence_ids).issubset(
                    set(ledger_entry.evidence_ids)
                ):
                    report.add(
                        f"/linked_claims/{claim_id}/evidence_ids",
                        "CLAIM_LEDGER_EVIDENCE_DRIFT",
                        f"Paper claim {claim_id} uses Evidence outside its ledger entry",
                        fatal=True,
                    )
            trace_text = "\n".join(
                [value.title, value.abstract, value.markdown]
            ).casefold().replace("-", " ").replace("_", " ")
            required_concepts = {
                "C0": "c0",
                "C1": "c1",
                "C2": "c2",
                "C3": "c3",
                "false acceptance": "false acceptance",
                "clean acceptance": "clean acceptance",
                "leakage control": "leakage",
                "review cost": "cost",
                "research tension": "tension",
            }
            missing_concepts = [
                label
                for label, token in required_concepts.items()
                if token not in trace_text
            ]
            if missing_concepts:
                report.add(
                    "/markdown",
                    "TRACE_SHORT_PAPER_COMPONENTS_MISSING",
                    f"TRACE_AUDIT paper omits {missing_concepts}",
                    fatal=True,
                )
    return report


def _paper_contrast_direction_issues(
    markdown: str,
    result_payloads: dict[str, Any],
) -> list[tuple[str, str]]:
    """Catch common lower-is-better prose inversions from named A-vs-B results."""

    issues: list[tuple[str, str]] = []
    sentences = [
        item.strip().lower()
        for item in re.split(r"(?<=[.!?])\s+|\n+", markdown)
        if item.strip()
    ]
    for result_id, payload in result_payloads.items():
        if not isinstance(payload, dict):
            continue
        metric = str(payload.get("primary_metric", "")).lower()
        lower_is_better = payload.get("higher_is_better") is not True and any(
            token in metric
            for token in (
                "loss",
                "error",
                "perplexity",
                "cross_entropy",
                "cross-entropy",
                "negative_log_likelihood",
                "nll",
            )
        )
        comparisons = payload.get("paired_comparisons")
        if not lower_is_better or not isinstance(comparisons, dict):
            continue
        for name, summary in comparisons.items():
            if not isinstance(name, str) or "_vs_" not in name:
                continue
            if not isinstance(summary, dict):
                continue
            mean_diff = summary.get("mean_diff")
            if not isinstance(mean_diff, (int, float)) or mean_diff == 0:
                continue
            left, right = name.lower().split("_vs_", 1)
            left = left.replace("_", " ")
            right = right.replace("_", " ")
            winner, loser = (
                (left, right) if mean_diff < 0 else (right, left)
            )
            winner_pattern = re.escape(winner)
            loser_pattern = re.escape(loser)
            contradictory_patterns = (
                rf"\b{loser_pattern}\b(?:\s+\w+){{0,2}}\s+outperformed\s+\b{winner_pattern}\b",
                rf"\b{loser_pattern}\b\s+was\s+better\s+than\s+\b{winner_pattern}\b",
                rf"\b{loser_pattern}\b(?:\s+\w+){{0,2}}\s+(?:had|achieved|showed)\s+(?:a\s+)?lower\s+[^.]*?than\s+\b{winner_pattern}\b",
            )
            for sentence in sentences:
                if any(token in sentence for token in ("did not", "does not", "cannot")):
                    continue
                if any(re.search(pattern, sentence) for pattern in contradictory_patterns):
                    issues.append(
                        (
                            result_id,
                            f"{name} is defined as left-minus-right with mean "
                            f"{mean_diff:.6g}; for a lower-is-better metric this "
                            f"favors {winner}, not {loser}",
                        )
                    )
                    break
    return list(dict.fromkeys(issues))


def validate_review(
    value: ReviewReport,
    *,
    required_criteria: set[str],
    hard_gate_criteria: set[str],
    known_trace_ids: set[str],
    rubric_version: str,
    minimum_passing_score: int = 3,
    significant_degradation: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    if value.rubric_version != rubric_version:
        report.add(
            "/rubric_version",
            "RUBRIC_VERSION_MISMATCH",
            f"Expected {rubric_version}, got {value.rubric_version}",
            fatal=True,
        )
    criterion_names = [item.criterion for item in value.criteria]
    present = set(criterion_names)
    duplicate_criteria = {
        name for name in criterion_names if criterion_names.count(name) > 1
    }
    missing = required_criteria - present
    unexpected = present - required_criteria
    if duplicate_criteria:
        report.add(
            "/criteria",
            "DUPLICATE_CRITERION",
            f"Duplicate criteria: {sorted(duplicate_criteria)}",
            fatal=True,
        )
    if missing:
        report.add(
            "/criteria",
            "MISSING_CRITERION",
            f"Missing criteria: {sorted(missing)}",
            fatal=True,
        )
    if unexpected:
        report.add(
            "/criteria",
            "UNEXPECTED_CRITERION",
            f"Unexpected criteria: {sorted(unexpected)}",
            fatal=True,
        )
    for index, criterion in enumerate(value.criteria):
        if not criterion.reason or not criterion.counterargument:
            report.add(
                f"/criteria/{index}",
                "UNSUPPORTED_SCORE",
                "Every review score requires a reason and counterargument",
                fatal=True,
            )
        if not criterion.evidence_ids:
            report.add(
                f"/criteria/{index}/evidence_ids",
                "MISSING_REVIEW_TRACE",
                "Every review score requires a traceable ID",
                fatal=True,
            )
        unknown = set(criterion.evidence_ids) - known_trace_ids
        if unknown:
            report.add(
                f"/criteria/{index}/evidence_ids",
                "UNKNOWN_REVIEW_TRACE",
                f"Unknown review trace IDs: {sorted(unknown)}",
                fatal=True,
            )
    for collection_name, issues in (
        ("fatal_issues", value.fatal_issues),
        ("non_fatal_issues", value.non_fatal_issues),
    ):
        for index, issue in enumerate(issues):
            unknown = set(issue.evidence) - known_trace_ids
            if not issue.evidence:
                report.add(
                    f"/{collection_name}/{index}/evidence",
                    "MISSING_ISSUE_TRACE",
                    "Every review issue requires traceable evidence",
                    fatal=True,
                )
            elif unknown:
                report.add(
                    f"/{collection_name}/{index}/evidence",
                    "UNKNOWN_ISSUE_TRACE",
                    f"Unknown issue trace IDs: {sorted(unknown)}",
                    fatal=True,
                )
    if value.action == WorkflowAction.ACCEPT and value.fatal_issues:
        report.add(
            "/action",
            "ACCEPT_WITH_FATAL_ISSUES",
            "A paper with fatal issues cannot be accepted",
            repair_scope="DECISION",
            fatal=True,
        )
    if value.action == WorkflowAction.ACCEPT and value.acceptance_conditions:
        report.add(
            "/acceptance_conditions",
            "ACCEPT_WITH_OPEN_CONDITIONS",
            "ACCEPT requires no unresolved acceptance conditions",
            repair_scope="DECISION",
            fatal=True,
        )
    if significant_degradation and value.contamination_status == ContaminationStatus.CLEAN:
        report.add(
            "/contamination_status",
            "DEGRADATION_MARKED_CLEAN",
            (
                "A significant Writer score degradation requires VALID_DOWNGRADE, "
                "REGRESSION, or CONTAMINATED classification"
            ),
            repair_scope="DECISION",
            fatal=True,
        )
    if value.action == WorkflowAction.ACCEPT and value.contamination_status in {
        ContaminationStatus.REGRESSION,
        ContaminationStatus.CONTAMINATED,
    }:
        report.add(
            "/contamination_status",
            "ACCEPT_WITH_WRITER_CONTAMINATION",
            "ACCEPT is incompatible with Writer regression or contamination",
            repair_scope="DECISION",
            fatal=True,
        )
    if value.action == WorkflowAction.ACCEPT:
        criteria_by_name = {item.criterion: item for item in value.criteria}
        failed = [
            name
            for name in hard_gate_criteria
            if (
                name not in criteria_by_name
                or criteria_by_name[name].score < minimum_passing_score
                or criteria_by_name[name].fatal_issue
            )
        ]
        if failed:
            report.add(
                "/criteria",
                "ACCEPT_HARD_GATE_FAILED",
                f"Failed review hard gates: {sorted(failed)}",
                repair_scope="DECISION",
                fatal=True,
            )
    return report


def compact_validation(report: ValidationReport) -> list[dict[str, Any]]:
    return [
        {
            "path": issue.path,
            "code": issue.code,
            "message": issue.message,
            "repair_scope": issue.repair_scope,
            "fatal": issue.fatal,
        }
        for issue in report.issues
    ]


def model_payload(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")
