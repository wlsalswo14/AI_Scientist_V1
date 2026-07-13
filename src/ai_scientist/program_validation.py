from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import (
    ClaimDirectorOutput,
    ClaimDependency,
    ClaimDependencyRelation,
    ClaimEvaluatorReport,
    ClaimUnit,
    DirectorRole,
    EvaluationDecision,
    ProgramClaimType,
    ResearchBrief,
    ResearchCondition,
    ResearchContract,
    ResearchMode,
    ResearchModeAssessment,
    ResearchProfile,
    ResearchPredictionCell,
    ResearchProgramComposition,
    ResearchDepth,
    ResearchReadiness,
    ResearchTarget,
    ResearchTargetType,
    SupportRelation,
    WorkflowAction,
)
from .trace_audit import build_trace_study_contract
from .validation import ValidationReport


EVALUATOR_A_GATES = {
    "Question Alignment",
    "Evidence Support",
    "Contribution Value",
}

EVALUATOR_B_GATES = {
    "Testability",
    "Feasibility",
    "Falsifiability",
    "Discriminating Power",
}

TRACE_EVALUATOR_A_GATES = EVALUATOR_A_GATES | {
    "Tension Grounding",
    "Nearest-Work Difference",
}


@dataclass(frozen=True, slots=True)
class ClaimPromotionDecision:
    promoted_ids: frozenset[str]
    failed_ids: frozenset[str]
    rejected_ids: frozenset[str]


def validate_claim_director(
    output: ClaimDirectorOutput,
    *,
    brief: ResearchBrief,
    role: DirectorRole,
) -> ValidationReport:
    report = ValidationReport()
    if output.director_role != role:
        report.add(
            "/director_role",
            "DIRECTOR_ROLE_MISMATCH",
            f"Expected {role.value}, got {output.director_role.value}",
            fatal=True,
        )
    if output.core_question.strip() != brief.core_question.strip():
        report.add(
            "/core_question",
            "CORE_QUESTION_DRIFT",
            "A Director must preserve the core research question",
            repair_scope="CONTRACT",
            fatal=True,
        )
    if output.research_objective.strip() != brief.research_objective.strip():
        report.add(
            "/research_objective",
            "OBJECTIVE_DRIFT",
            "A Director must preserve the research objective",
            repair_scope="CONTRACT",
            fatal=True,
        )
    expected_prefix = "A" if role == DirectorRole.ANCHOR else "X"
    if brief.research_profile == ResearchProfile.TRACE_AUDIT:
        evidence_ids = {item.evidence_id for item in output.evidence}
        tension_ids = {item.tension_id for item in output.trace_tensions}
        if not output.trace_tensions:
            report.add(
                "/trace_tensions",
                "MISSING_TRACE_TENSION",
                "TRACE_AUDIT Directors must produce source-grounded tension cards",
                fatal=True,
            )
        if not output.selected_trace_tension_ids:
            report.add(
                "/selected_trace_tension_ids",
                "MISSING_SELECTED_TRACE_TENSION",
                "Each TRACE_AUDIT Director must select at least one tension",
                fatal=True,
            )
        for index, tension in enumerate(output.trace_tensions):
            if not tension.tension_id.startswith(f"{expected_prefix}-T"):
                report.add(
                    f"/trace_tensions/{index}/tension_id",
                    "INVALID_TENSION_ID",
                    f"{role.value} tension IDs must use prefix {expected_prefix}-T",
                    fatal=True,
                )
            if not tension.agreements or not tension.conflicts:
                report.add(
                    f"/trace_tensions/{index}",
                    "INCOMPLETE_TENSION_CONFLICT",
                    "A tension needs explicit agreements and conflicts",
                    fatal=True,
                )
            if not tension.nearest_work:
                report.add(
                    f"/trace_tensions/{index}/nearest_work",
                    "MISSING_NEAREST_WORK",
                    "A TRACE_AUDIT tension needs a nearest-work comparison",
                    fatal=True,
                )
            if any(item.evidence_id not in evidence_ids for item in tension.nearest_work):
                report.add(
                    f"/trace_tensions/{index}/nearest_work",
                    "UNKNOWN_NEAREST_WORK_EVIDENCE",
                    "Nearest-work comparisons must resolve to local Evidence IDs",
                    fatal=True,
                )
            for field_name, value in {
                "statement": tension.statement,
                "unexplained_phenomenon": tension.unexplained_phenomenon,
                "importance": tension.importance,
                "why_now": tension.why_now,
                "falsifiable_probe": tension.falsifiable_probe,
            }.items():
                if not value.strip():
                    report.add(
                        f"/trace_tensions/{index}/{field_name}",
                        "INCOMPLETE_TRACE_TENSION",
                        f"Trace tension is missing {field_name}",
                        fatal=True,
                    )
        if not set(output.selected_trace_tension_ids).issubset(tension_ids):
            report.add(
                "/selected_trace_tension_ids",
                "UNKNOWN_SELECTED_TRACE_TENSION",
                "Selected tension IDs must exist in the same Director output",
                fatal=True,
            )
    for index, claim in enumerate(output.claims):
        if not re.fullmatch(rf"{expected_prefix}[1-9][0-9]*", claim.claim_id):
            report.add(
                f"/claims/{index}/claim_id",
                "INVALID_ROLE_CLAIM_ID",
                f"{role.value} claim IDs must use prefix {expected_prefix}",
                fatal=True,
            )
        for field_name, value in {
            "statement": claim.statement,
            "null_statement": claim.null_statement,
            "distinctive_prediction": claim.distinctive_prediction,
            "falsification_condition": claim.falsification_condition,
            "minimum_experiment": claim.minimum_experiment,
            "measurement": claim.measurement,
            "decision_threshold": claim.decision_threshold,
        }.items():
            if not value.strip():
                report.add(
                    f"/claims/{index}/{field_name}",
                    "MISSING_CLAIM_FIELD",
                    f"Claim {claim.claim_id} is missing {field_name}",
                    fatal=True,
                )
        if not claim.controlled_variables or not claim.manipulated_variables:
            report.add(
                f"/claims/{index}",
                "MISSING_CONTROL_OR_MANIPULATION",
                f"Claim {claim.claim_id} needs controls and a manipulation",
                fatal=True,
            )
        if not claim.evidence_ids:
            report.add(
                f"/claims/{index}/evidence_ids",
                "MISSING_CLAIM_EVIDENCE",
                f"Claim {claim.claim_id} must cite at least one source-located evidence unit",
                fatal=True,
            )
        if (
            brief.research_profile == ResearchProfile.TRACE_AUDIT
            and not claim.tension_ids
        ):
            report.add(
                f"/claims/{index}/tension_ids",
                "CLAIM_WITHOUT_TRACE_TENSION",
                f"TRACE_AUDIT claim {claim.claim_id} must link to a tension card",
                fatal=True,
            )
        if (
            role == DirectorRole.ANCHOR
            and claim.claim_type
            in {ProgramClaimType.EMPIRICAL, ProgramClaimType.BENCHMARK}
            and not re.search(r"\d", claim.decision_threshold)
        ):
            report.add(
                f"/claims/{index}/decision_threshold",
                "NON_NUMERIC_ANCHOR_THRESHOLD",
                "Empirical and benchmark anchor claims need a numeric decision rule",
                fatal=True,
            )
    if role == DirectorRole.ANCHOR and not output.claims:
        report.add(
            "/claims",
            "MISSING_ANCHOR_CLAIM",
            "At least one Anchor claim is required",
            fatal=True,
        )
    return report


def validate_claim_dependencies(
    anchor: ClaimDirectorOutput,
    expansion: ClaimDirectorOutput,
) -> ValidationReport:
    report = ValidationReport()
    claims = {item.claim_id: item for item in [*anchor.claims, *expansion.claims]}
    anchor_ids = {item.claim_id for item in anchor.claims}
    expansion_ids = {item.claim_id for item in expansion.claims}
    for claim in claims.values():
        for dependency in claim.dependencies:
            if dependency.claim_id not in claims:
                report.add(
                    f"/claims/{claim.claim_id}/dependencies",
                    "UNKNOWN_CLAIM_DEPENDENCY",
                    f"{claim.claim_id} depends on unknown {dependency.claim_id}",
                    fatal=True,
                )
            if dependency.claim_id == claim.claim_id:
                report.add(
                    f"/claims/{claim.claim_id}/dependencies",
                    "SELF_DEPENDENCY",
                    f"{claim.claim_id} cannot depend on itself",
                    fatal=True,
                )
            if claim.claim_id in anchor_ids and dependency.claim_id in expansion_ids:
                report.add(
                    f"/claims/{claim.claim_id}/dependencies",
                    "ANCHOR_DEPENDS_ON_EXPANSION",
                    f"Anchor claim {claim.claim_id} cannot depend on Expansion claim "
                    f"{dependency.claim_id}",
                    fatal=True,
                )

    graph = {
        claim_id: [
            dependency.claim_id
            for dependency in claim.dependencies
            if dependency.claim_id in claims
        ]
        for claim_id, claim in claims.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> bool:
        if claim_id in visiting:
            return True
        if claim_id in visited:
            return False
        visiting.add(claim_id)
        if any(visit(parent) for parent in graph[claim_id]):
            return True
        visiting.remove(claim_id)
        visited.add(claim_id)
        return False

    if any(visit(claim_id) for claim_id in graph if claim_id not in visited):
        report.add(
            "/claims/dependencies",
            "CYCLIC_CLAIM_DEPENDENCY",
            "Claim dependencies must form an acyclic graph",
            fatal=True,
        )

    def reaches_anchor(claim_id: str, seen: set[str] | None = None) -> bool:
        seen = set() if seen is None else seen
        if claim_id in seen:
            return False
        seen.add(claim_id)
        for parent in graph.get(claim_id, []):
            if parent in anchor_ids:
                return True
            if parent in expansion_ids and reaches_anchor(parent, seen):
                return True
        return False

    for claim_id in expansion_ids:
        if not reaches_anchor(claim_id):
            report.add(
                f"/claims/{claim_id}/dependencies",
                "UNANCHORED_EXPANSION_CLAIM",
                f"Expansion claim {claim_id} must connect directly or transitively "
                "to an Anchor claim",
                fatal=True,
            )
    return report


def validate_claim_evaluator(
    value: ClaimEvaluatorReport,
    *,
    claim_ids: set[str],
    claim_roles: dict[str, DirectorRole] | None = None,
    required_gates: set[str],
    evidence_ids: set[str],
    rubric_version: str,
    minimum_passing_score: int,
    locked_claim_ids: set[str] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    locked_claim_ids = locked_claim_ids or set()
    if not locked_claim_ids.issubset(claim_ids):
        report.add(
            "/claim_evaluations",
            "UNKNOWN_LOCKED_CLAIM",
            "Locked claims must be part of the evaluated claim set",
            fatal=True,
        )
    if value.rubric_version != rubric_version:
        report.add(
            "/rubric_version",
            "RUBRIC_VERSION_MISMATCH",
            f"Expected rubric {rubric_version}",
            fatal=True,
        )
    evaluation_ids = [item.claim_id for item in value.claim_evaluations]
    if len(evaluation_ids) != len(set(evaluation_ids)):
        report.add(
            "/claim_evaluations",
            "DUPLICATE_CLAIM_EVALUATION",
            "Each claim must be evaluated exactly once",
            fatal=True,
        )
    if set(evaluation_ids) != claim_ids:
        report.add(
            "/claim_evaluations",
            "CLAIM_EVALUATION_COVERAGE",
            f"Expected {sorted(claim_ids)}, got {sorted(set(evaluation_ids))}",
            fatal=True,
        )
    available_evidence = evidence_ids | {
        item.evidence_id for item in value.discovered_evidence
    }
    failed_ids: set[str] = set()
    rejected_ids: set[str] = set()
    for index, evaluation in enumerate(value.claim_evaluations):
        gate_names = [item.gate for item in evaluation.gates]
        if set(gate_names) != required_gates or len(gate_names) != len(set(gate_names)):
            report.add(
                f"/claim_evaluations/{index}/gates",
                "CLAIM_GATE_COVERAGE",
                f"Claim {evaluation.claim_id} must have gates {sorted(required_gates)}",
                fatal=True,
            )
        all_pass = not evaluation.fatal_issues
        has_fatal = bool(evaluation.fatal_issues)
        failed_gate_names: set[str] = set()
        for gate_index, gate in enumerate(evaluation.gates):
            expected_pass = gate.score >= minimum_passing_score and not gate.fatal_issue
            if gate.passed != expected_pass:
                report.add(
                    f"/claim_evaluations/{index}/gates/{gate_index}/passed",
                    "INCONSISTENT_GATE_PASS",
                    f"Claim {evaluation.claim_id} gate {gate.gate} pass flag disagrees with score",
                    fatal=True,
                )
            if set(gate.evidence_ids) - available_evidence:
                report.add(
                    f"/claim_evaluations/{index}/gates/{gate_index}/evidence_ids",
                    "UNKNOWN_GATE_EVIDENCE",
                    f"Claim {evaluation.claim_id} gate references unknown evidence",
                    fatal=True,
                )
            all_pass = all_pass and expected_pass
            has_fatal = has_fatal or gate.fatal_issue
            if not expected_pass:
                failed_gate_names.add(gate.gate)
            if gate.gate == "Evidence Support" and gate.passed and not gate.evidence_ids:
                report.add(
                    f"/claim_evaluations/{index}/gates/{gate_index}/evidence_ids",
                    "MISSING_EVIDENCE_SUPPORT_ID",
                    "A passing Evidence Support gate must cite evidence",
                    fatal=True,
                )
        expected_decision = (
            EvaluationDecision.PROMOTE
            if all_pass
            else (
                EvaluationDecision.REJECT
                if has_fatal
                else EvaluationDecision.REVISE
            )
        )
        if evaluation.recommended_decision != expected_decision:
            report.add(
                f"/claim_evaluations/{index}/recommended_decision",
                "INCONSISTENT_CLAIM_DECISION",
                f"Claim {evaluation.claim_id} must be {expected_decision.value}",
                fatal=True,
            )
        if (
            evaluation.claim_id in locked_claim_ids
            and expected_decision != EvaluationDecision.PROMOTE
        ):
            report.add(
                f"/claim_evaluations/{index}",
                "LOCKED_CLAIM_DOWNGRADED",
                f"Locked claim {evaluation.claim_id} must retain its prior promotion "
                "while its scientific content is unchanged",
                fatal=True,
            )
        if not all_pass:
            failed_ids.add(evaluation.claim_id)
        if expected_decision == EvaluationDecision.REJECT:
            rejected_ids.add(evaluation.claim_id)
    note_ids = [item.claim_id for item in value.error_notebook]
    if len(note_ids) != len(set(note_ids)):
        report.add(
            "/error_notebook",
            "DUPLICATE_ERROR_NOTE",
            "Each failed claim may have only one error note",
            fatal=True,
        )
    if set(note_ids) != failed_ids:
        report.add(
            "/error_notebook",
            "ERROR_NOTE_COVERAGE",
            f"Error notes must cover failed claims {sorted(failed_ids)}",
            fatal=True,
        )
    for index, note in enumerate(value.error_notebook):
        if claim_roles and note.source_role != claim_roles.get(note.claim_id):
            report.add(
                f"/error_notebook/{index}/source_role",
                "ERROR_NOTE_ROLE_MISMATCH",
                f"Error note {note.claim_id} is routed to the wrong Director",
                fatal=True,
            )
        evaluation = next(
            (
                item
                for item in value.claim_evaluations
                if item.claim_id == note.claim_id
            ),
            None,
        )
        if evaluation is not None:
            expected_failed = {
                gate.gate
                for gate in evaluation.gates
                if not gate.passed or gate.fatal_issue
            }
            if not expected_failed.issubset(set(note.failed_gates)):
                report.add(
                    f"/error_notebook/{index}/failed_gates",
                    "ERROR_NOTE_GATE_COVERAGE",
                    f"Error note {note.claim_id} must name failed gates {sorted(expected_failed)}",
                    fatal=True,
                )
    expected_overall = (
        EvaluationDecision.PROMOTE
        if not failed_ids
        else (
            EvaluationDecision.REJECT
            if rejected_ids == claim_ids
            else EvaluationDecision.REVISE
        )
    )
    if value.overall_decision != expected_overall:
        report.add(
            "/overall_decision",
            "INCONSISTENT_OVERALL_DECISION",
            f"Expected overall decision {expected_overall.value}",
            fatal=True,
        )
    return report


def compute_claim_promotions(
    evaluator_a: ClaimEvaluatorReport,
    evaluator_b: ClaimEvaluatorReport,
) -> ClaimPromotionDecision:
    decisions_a = {
        item.claim_id: item.recommended_decision
        for item in evaluator_a.claim_evaluations
    }
    decisions_b = {
        item.claim_id: item.recommended_decision
        for item in evaluator_b.claim_evaluations
    }
    ids = set(decisions_a) | set(decisions_b)
    promoted = {
        claim_id
        for claim_id in ids
        if decisions_a.get(claim_id) == EvaluationDecision.PROMOTE
        and decisions_b.get(claim_id) == EvaluationDecision.PROMOTE
    }
    rejected = {
        claim_id
        for claim_id in ids
        if EvaluationDecision.REJECT
        in {decisions_a.get(claim_id), decisions_b.get(claim_id)}
    }
    return ClaimPromotionDecision(
        promoted_ids=frozenset(promoted),
        failed_ids=frozenset(ids - promoted - rejected),
        rejected_ids=frozenset(rejected),
    )


def filter_promotions_by_dependencies(
    claims: dict[str, object],
    promoted_ids: set[str],
) -> set[str]:
    """Remove claims whose required parents did not pass deterministic gates."""

    eligible = set(promoted_ids)
    required_relations = {
        ClaimDependencyRelation.REQUIRES_SUPPORT,
        ClaimDependencyRelation.REQUIRES_TEST,
        ClaimDependencyRelation.CAN_SURVIVE_NULL,
        ClaimDependencyRelation.GENERALIZES,
    }
    changed = True
    while changed:
        changed = False
        for claim_id in list(eligible):
            claim = claims[claim_id]
            dependencies = getattr(claim, "dependencies", [])
            required = {
                item.claim_id
                for item in dependencies
                if item.relation in required_relations
            }
            if not required.issubset(eligible):
                eligible.remove(claim_id)
                changed = True
    return eligible


def validate_program_composition(
    value: ResearchProgramComposition,
    *,
    all_claim_ids: set[str],
    promoted_claim_ids: set[str],
    anchor_claim_ids: set[str],
    expansion_claim_ids: set[str],
    claim_dependencies: dict[str, list[ClaimDependency]],
    brief: ResearchBrief,
    max_integrated_claims: int,
    claim_tension_ids: dict[str, list[str]] | None = None,
    available_tension_ids: set[str] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    integrated = value.integrated_claim_ids
    if len(integrated) != len(set(integrated)):
        report.add(
            "/integrated_claim_ids",
            "DUPLICATE_INTEGRATED_CLAIM",
            "Integrated claim IDs must be unique",
            fatal=True,
        )
    if not set(integrated).issubset(promoted_claim_ids):
        report.add(
            "/integrated_claim_ids",
            "UNPROMOTED_CLAIM_IN_PROGRAM",
            "Composer may integrate only deterministically promoted claims",
            fatal=True,
        )
    if len(integrated) > max_integrated_claims:
        report.add(
            "/integrated_claim_ids",
            "PROGRAM_TARGET_BUDGET_EXCEEDED",
            f"At most {max_integrated_claims} claims may be integrated",
            fatal=True,
        )
    if not set(integrated).intersection(anchor_claim_ids):
        report.add(
            "/integrated_claim_ids",
            "MISSING_ANCHOR_IN_PROGRAM",
            "The research program must include an Anchor claim",
            fatal=True,
        )
    required_expansion_count = {
        ResearchDepth.QUICK: 0,
        ResearchDepth.COMPETITION: 1,
        ResearchDepth.THESIS: 1,
        ResearchDepth.PUBLICATION: 2,
    }[brief.research_depth]
    if len(set(integrated).intersection(expansion_claim_ids)) < required_expansion_count:
        report.add(
            "/integrated_claim_ids",
            "MISSING_EXPANSION_IN_DEEP_PROGRAM",
            f"{brief.research_depth.value} requires {required_expansion_count} "
            "integrated Expansion claim(s)",
            fatal=True,
        )
    deferred = set(value.deferred_claim_ids)
    if deferred != all_claim_ids - set(integrated):
        report.add(
            "/deferred_claim_ids",
            "DEFERRED_CLAIM_COVERAGE",
            "Deferred claims must be exactly all non-integrated claims",
            fatal=True,
        )
    stage_ids = [claim_id for stage in value.stages for claim_id in stage.claim_ids]
    if set(stage_ids) != set(integrated) or len(stage_ids) != len(set(stage_ids)):
        report.add(
            "/stages",
            "PROGRAM_STAGE_COVERAGE",
            "Program stages must cover every integrated claim exactly once",
            fatal=True,
        )
    stage_numbers = [stage.stage_number for stage in value.stages]
    if len(stage_numbers) != len(set(stage_numbers)):
        report.add(
            "/stages",
            "DUPLICATE_PROGRAM_STAGE_NUMBER",
            "Each research-program stage must have a unique number",
            fatal=True,
        )
    if stage_numbers and sorted(stage_numbers) != list(
        range(1, len(stage_numbers) + 1)
    ):
        report.add(
            "/stages",
            "NONCONTIGUOUS_PROGRAM_STAGES",
            "Research-program stage numbers must be contiguous from 1",
            fatal=True,
        )
    stage_position = {
        claim_id: stage.stage_number
        for stage in value.stages
        for claim_id in stage.claim_ids
    }
    required_relations = {
        ClaimDependencyRelation.REQUIRES_SUPPORT,
        ClaimDependencyRelation.REQUIRES_TEST,
        ClaimDependencyRelation.CAN_SURVIVE_NULL,
        ClaimDependencyRelation.GENERALIZES,
    }
    for claim_id in integrated:
        for dependency in claim_dependencies.get(claim_id, []):
            if dependency.relation not in required_relations:
                continue
            if dependency.claim_id not in set(integrated):
                report.add(
                    f"/integrated_claim_ids/{claim_id}",
                    "MISSING_REQUIRED_PARENT_CLAIM",
                    f"{claim_id} requires integrated parent {dependency.claim_id}",
                    fatal=True,
                )
            elif stage_position[dependency.claim_id] >= stage_position[claim_id]:
                report.add(
                    f"/stages/{claim_id}",
                    "DEPENDENCY_STAGE_ORDER",
                    f"{dependency.claim_id} must precede dependent claim {claim_id}",
                    fatal=True,
                )
    if value.action != WorkflowAction.PROMOTE:
        report.add(
            "/action",
            "PROGRAM_NOT_PROMOTED",
            "A ready research program requires Composer action PROMOTE",
            fatal=True,
        )
    for field_name, field_value in {
        "scope": value.scope,
        "mode_rationale": value.mode_rationale,
        "claim_ceiling": value.claim_ceiling,
    }.items():
        if not field_value.strip():
            report.add(
                f"/{field_name}",
                "MISSING_PROGRAM_FIELD",
                f"Program composition is missing {field_name}",
                fatal=True,
            )
    if brief.research_profile == ResearchProfile.TRACE_AUDIT:
        selected_tensions = set(value.selected_trace_tension_ids)
        available_tension_ids = available_tension_ids or set()
        claim_tension_ids = claim_tension_ids or {}
        if not selected_tensions:
            report.add(
                "/selected_trace_tension_ids",
                "PROGRAM_WITHOUT_TRACE_TENSION",
                "TRACE_AUDIT composition must retain its validated tension cards",
                fatal=True,
            )
        if selected_tensions - available_tension_ids:
            report.add(
                "/selected_trace_tension_ids",
                "UNKNOWN_PROGRAM_TRACE_TENSION",
                "Composer selected a tension not produced by either Director",
                fatal=True,
            )
        for claim_id in integrated:
            if not set(claim_tension_ids.get(claim_id, [])).intersection(
                selected_tensions
            ):
                report.add(
                    f"/integrated_claim_ids/{claim_id}",
                    "PROGRAM_CLAIM_TENSION_DISCONNECTED",
                    f"Integrated claim {claim_id} is not linked to a selected tension",
                    fatal=True,
                )
    return report


def build_research_contract_from_program(
    *,
    brief: ResearchBrief,
    assessment: ResearchModeAssessment,
    anchor: ClaimDirectorOutput,
    expansion: ClaimDirectorOutput,
    evaluator_a: ClaimEvaluatorReport,
    evaluator_b: ClaimEvaluatorReport,
    composition: ResearchProgramComposition,
    pipeline_smoke_test: bool = False,
) -> ResearchContract:
    all_claims = {item.claim_id: item for item in [*anchor.claims, *expansion.claims]}
    selected = [all_claims[claim_id] for claim_id in composition.integrated_claim_ids]
    evidence_by_id = {}
    for item in [
        *anchor.evidence,
        *expansion.evidence,
        *evaluator_a.discovered_evidence,
        *evaluator_b.discovered_evidence,
    ]:
        evidence_by_id.setdefault(item.evidence_id, item)

    target_type = {
        ProgramClaimType.EMPIRICAL: ResearchTargetType.TEST_CLAIM,
        ProgramClaimType.MECHANISTIC: ResearchTargetType.MECHANISTIC_HYPOTHESIS,
        ProgramClaimType.BOUNDARY_CONDITION: ResearchTargetType.BOUNDARY_CLAIM,
        ProgramClaimType.GENERALIZATION: ResearchTargetType.GENERALIZATION_CLAIM,
        ProgramClaimType.THEORETICAL: ResearchTargetType.THEORETICAL_CLAIM,
        ProgramClaimType.ENGINEERING: ResearchTargetType.ENGINEERING_CLAIM,
        ProgramClaimType.BENCHMARK: ResearchTargetType.BENCHMARK_CLAIM,
    }
    targets = [
        ResearchTarget(
            target_id=item.claim_id,
            target_type=target_type[item.claim_type],
            statement=item.statement,
            null_statement=item.null_statement,
            rationale=item.rationale,
            mechanism=item.mechanism,
            tension_ids=item.tension_ids,
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
        for item in selected
    ]
    claim_units = [
        ClaimUnit(
            claim_id=item.claim_id,
            text=item.statement,
            claim_type=item.claim_type.value,
            importance="CRITICAL",
            evidence_ids=item.evidence_ids,
            support_relation=SupportRelation.PARTIALLY_SUPPORTED,
            director_inference=True,
            inference_explanation=(
                f"Proposed by the {item.source_role.value} Director and promoted "
                "for experimental testing; not treated as an established result."
            ),
        )
        for item in selected
    ]
    condition = ResearchCondition(
        condition_id="PROGRAM-C1",
        description="Integrated conditions for the promoted research program",
        controlled_variables=list(
            dict.fromkeys(
                value for item in selected for value in item.controlled_variables
            )
        ),
        manipulated_variables=list(
            dict.fromkeys(
                value for item in selected for value in item.manipulated_variables
            )
        ),
        measurement="; ".join(
            f"{item.claim_id}: {item.measurement}" for item in selected
        ),
        decision_threshold="; ".join(
            f"{item.claim_id}: {item.decision_threshold}" for item in selected
        ),
        predictions=[
            ResearchPredictionCell(
                target_id=item.claim_id,
                direction="claim-specific",
                expected_pattern=item.distinctive_prediction,
                rejection_condition=item.falsification_condition,
            )
            for item in selected
        ],
    )
    has_expansion = any(item.source_role == DirectorRole.EXPANSION for item in selected)
    mode = (
        ResearchMode.HYBRID_RESEARCH
        if has_expansion
        else (
            assessment.surface_mode
            if assessment.surface_mode not in {None, ResearchMode.HYBRID_RESEARCH}
            else ResearchMode.DIRECT_TEST
        )
    )
    readiness = {
        ResearchMode.DIRECT_TEST: ResearchReadiness.TEST_READY,
        ResearchMode.EXPLANATORY_RESEARCH: ResearchReadiness.THEORY_READY,
        ResearchMode.BENCHMARK_AUDIT: ResearchReadiness.AUDIT_READY,
        ResearchMode.HYBRID_RESEARCH: ResearchReadiness.PROGRAM_READY,
    }[mode]
    trace_tensions_by_id = {
        item.tension_id: item
        for item in [*anchor.trace_tensions, *expansion.trace_tensions]
    }
    selected_trace_tensions = [
        trace_tensions_by_id[tension_id]
        for tension_id in composition.selected_trace_tension_ids
        if tension_id in trace_tensions_by_id
    ]
    trace_study_contract = (
        build_trace_study_contract(
            [item.claim_id for item in selected],
            pipeline_smoke_test=pipeline_smoke_test,
        )
        if brief.research_profile == ResearchProfile.TRACE_AUDIT
        else None
    )
    return ResearchContract(
        contract_version="2.0",
        original_question=brief.core_question,
        research_mode=mode,
        research_profile=brief.research_profile,
        readiness=readiness,
        selected_domain=composition.scope,
        scope=composition.scope,
        mode_rationale=composition.mode_rationale,
        claim_ceiling=composition.claim_ceiling,
        evidence=list(evidence_by_id.values()),
        trace_tensions=selected_trace_tensions,
        selected_trace_tension_ids=composition.selected_trace_tension_ids,
        claims=claim_units,
        targets=targets,
        selected_target_ids=[item.claim_id for item in selected],
        prediction_matrix=[condition],
        trace_study_contract=trace_study_contract,
        search_limitations=list(
            dict.fromkeys([*anchor.search_limitations, *expansion.search_limitations])
        ),
    )
