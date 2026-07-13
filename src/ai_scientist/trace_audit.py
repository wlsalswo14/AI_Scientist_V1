from __future__ import annotations

import json
import hashlib
import math
import random
import re
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from .schemas import (
    ClaimLedger,
    ClaimLedgerEntry,
    ExperimentStageResult,
    ResearchContract,
    ResearchProfile,
    ResearchResultStatus,
    TraceAuditResultPayload,
    TraceBenchmarkPlan,
    TraceConditionMetrics,
    TraceFaultConditionMetrics,
    TraceConditionSpec,
    TraceCorruptionPlan,
    TraceFaultType,
    TraceGateRule,
    TraceReviewCondition,
    TraceReviewerDecisionBatch,
    TracePairedComparison,
    TraceStudyContract,
)


TRACE_CONDITIONS = (
    TraceReviewCondition.PAPER_ONLY,
    TraceReviewCondition.RAW_ARTIFACTS,
    TraceReviewCondition.STRUCTURED_PROVENANCE,
    TraceReviewCondition.TRACE_GATE,
)

TRACE_FAULT_TYPES = tuple(TraceFaultType)

TRACE_COMPARISONS = (
    (
        "C3_vs_C0",
        TraceReviewCondition.TRACE_GATE,
        TraceReviewCondition.PAPER_ONLY,
    ),
    (
        "C1_vs_C0",
        TraceReviewCondition.RAW_ARTIFACTS,
        TraceReviewCondition.PAPER_ONLY,
    ),
    (
        "C2_vs_C1",
        TraceReviewCondition.STRUCTURED_PROVENANCE,
        TraceReviewCondition.RAW_ARTIFACTS,
    ),
    (
        "C3_vs_C2",
        TraceReviewCondition.TRACE_GATE,
        TraceReviewCondition.STRUCTURED_PROVENANCE,
    ),
)


def trace_contract_fingerprint(contract: TraceStudyContract) -> str:
    canonical = json.dumps(
        contract.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def benchmark_plan_issues(
    value: TraceBenchmarkPlan,
    contract: TraceStudyContract,
    *,
    pipeline_smoke_test: bool,
) -> list[str]:
    issues: list[str] = []
    expected_fingerprint = trace_contract_fingerprint(contract)
    if value.trace_contract_fingerprint != expected_fingerprint:
        issues.append("Benchmark Plan changed the frozen Trace Study Contract identity")
    if value.planned_case_count < contract.benchmark_min_cases:
        issues.append(
            f"Benchmark Plan needs at least {contract.benchmark_min_cases} cases"
        )
    if not pipeline_smoke_test and value.planned_case_count % 2:
        issues.append("The substantive paired benchmark must contain an even case count")
    if not pipeline_smoke_test and not value.main_split_frozen:
        issues.append("The substantive main benchmark split must be frozen")
    if not value.clean_case_definition.strip() or not value.paired_variant_policy.strip():
        issues.append("Clean-case and paired-variant policies are required")
    if not value.gold_label_policy.strip() or not value.adjudication_policy.strip():
        issues.append("Gold-label and adjudication policies are required")
    source_ids = [item.source_id for item in value.sources]
    if len(source_ids) != len(set(source_ids)):
        issues.append("Benchmark source IDs must be unique")
    if not value.inclusion_criteria or not value.exclusion_criteria:
        issues.append("Benchmark inclusion and exclusion criteria are required")
    return issues


def corruption_plan_issues(
    value: TraceCorruptionPlan,
    contract: TraceStudyContract,
) -> list[str]:
    issues: list[str] = []
    expected_fingerprint = trace_contract_fingerprint(contract)
    if value.trace_contract_fingerprint != expected_fingerprint:
        issues.append("Corruption Plan changed the frozen Trace Study Contract identity")
    recipe_ids = [item.recipe_id for item in value.recipes]
    if len(recipe_ids) != len(set(recipe_ids)):
        issues.append("Corruption recipe IDs must be unique")
    covered = {item.fault_type for item in value.recipes}
    missing = set(contract.fault_types) - covered
    if missing:
        issues.append(
            "Corruption recipes do not cover registered fault types: "
            + ", ".join(sorted(item.value for item in missing))
        )
    if not value.hidden_from_reviewer:
        issues.append("Corruption manifests and gold labels must be hidden from reviewers")
    if not value.deterministic_replay:
        issues.append("Every corruption must support deterministic replay")
    if not value.manifest_fields or not value.leakage_test.strip():
        issues.append("Manifest fields and a leakage test are required")
    for recipe in value.recipes:
        if not recipe.hidden_fields:
            issues.append(f"Recipe {recipe.recipe_id} exposes no hidden manifest fields")
    return issues


def reviewer_decision_batch_issues(
    value: TraceReviewerDecisionBatch,
    contract: TraceStudyContract,
) -> list[str]:
    issues: list[str] = []
    if value.trace_contract_fingerprint != trace_contract_fingerprint(contract):
        issues.append("Reviewer decision batch does not match the frozen trace contract")
    if not value.leakage_check_passed:
        issues.append("Reviewer decision batch failed its leakage check")
    if len(value.corruption_manifest_hash.strip()) < 8:
        issues.append("Reviewer decision batch needs a corruption manifest hash")
    declared_models = set(value.reviewer_models)
    observed_models = {item.reviewer_model for item in value.decisions}
    if observed_models != declared_models:
        issues.append("Declared and observed reviewer model sets differ")
    if len(declared_models) < contract.minimum_reviewer_families:
        issues.append(
            f"Reviewer batch needs {contract.minimum_reviewer_families} model families"
        )
    cases: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for decision in value.decisions:
        cases[(decision.reviewer_model, decision.case_id)].append(decision)
    unique_case_ids = {case_id for _, case_id in cases}
    if len(unique_case_ids) < contract.benchmark_min_cases:
        issues.append(
            f"Reviewer batch needs at least {contract.benchmark_min_cases} unique cases"
        )
    for (reviewer_model, case_id), decisions in cases.items():
        condition_ids = [item.condition_id for item in decisions]
        if set(condition_ids) != set(TRACE_CONDITIONS) or len(condition_ids) != 4:
            issues.append(
                f"Reviewer {reviewer_model} case {case_id} does not cover C0-C3 exactly"
            )
        if len({item.gold_faulty for item in decisions}) != 1:
            issues.append(
                f"Reviewer {reviewer_model} case {case_id} changes its hidden gold label"
            )
        if len({item.gold_fault_type for item in decisions}) != 1:
            issues.append(
                f"Reviewer {reviewer_model} case {case_id} changes its hidden fault type"
            )
        gold_faulty = decisions[0].gold_faulty
        gold_fault_type = decisions[0].gold_fault_type
        if gold_faulty and gold_fault_type is None:
            issues.append(
                f"Reviewer {reviewer_model} case {case_id} lacks its post-review fault stratum"
            )
        if not gold_faulty and gold_fault_type is not None:
            issues.append(
                f"Reviewer {reviewer_model} clean case {case_id} has a fault stratum"
            )
    covered_faults = {
        item.gold_fault_type for item in value.decisions if item.gold_fault_type is not None
    }
    missing_faults = set(contract.fault_types) - covered_faults
    if missing_faults and contract.benchmark_min_cases >= len(contract.fault_types):
        issues.append(
            "Reviewer batch omits registered fault strata: "
            + ", ".join(sorted(item.value for item in missing_faults))
        )
    return list(dict.fromkeys(issues))


def build_trace_study_contract(
    claim_ids: list[str],
    *,
    pipeline_smoke_test: bool,
) -> TraceStudyContract:
    """Build the frozen topic-specific protocol after claims pass both evaluators."""

    conditions = [
        TraceConditionSpec(
            condition_id=TraceReviewCondition.PAPER_ONLY,
            reviewer_inputs=["anonymous short paper"],
            structured_provenance=False,
            deterministic_gate=False,
        ),
        TraceConditionSpec(
            condition_id=TraceReviewCondition.RAW_ARTIFACTS,
            reviewer_inputs=["anonymous short paper", "raw code", "logs", "results"],
            structured_provenance=False,
            deterministic_gate=False,
        ),
        TraceConditionSpec(
            condition_id=TraceReviewCondition.STRUCTURED_PROVENANCE,
            reviewer_inputs=[
                "anonymous short paper",
                "claim-result-code provenance graph",
            ],
            structured_provenance=True,
            deterministic_gate=False,
        ),
        TraceConditionSpec(
            condition_id=TraceReviewCondition.TRACE_GATE,
            reviewer_inputs=[
                "anonymous short paper",
                "claim-result-code provenance graph",
                "deterministic TRACE-GATE report",
            ],
            structured_provenance=True,
            deterministic_gate=True,
        ),
    ]
    rule_text = {
        TraceFaultType.RESULT_DIRECTION: (
            "Recompute named contrast direction from the canonical metric and result."
        ),
        TraceFaultType.METRIC_CLAIM_MISMATCH: (
            "Require every empirical claim to name the metric represented by its Result ID."
        ),
        TraceFaultType.STALE_ARTIFACT: (
            "Reject claims whose dependency closure contains a STALE artifact."
        ),
        TraceFaultType.EXECUTION_HASH_MISMATCH: (
            "Match result, experiment ID, and code hash to one immutable execution."
        ),
        TraceFaultType.CONTRACT_DRIFT: (
            "Compare executed data, controls, metrics, and stopping rule to the frozen contract."
        ),
        TraceFaultType.NEGATIVE_RESULT_OMISSION: (
            "Require negative, null, falsified, and inconclusive outcomes in the paper ledger."
        ),
        TraceFaultType.CLAIM_CEILING: (
            "Reject paper claims that exceed the frozen claim ceiling or selected targets."
        ),
        TraceFaultType.CODE_INVARIANT: (
            "Apply registered topic-specific invariants to generated experiment code."
        ),
        TraceFaultType.UNSUPPORTED_MECHANISM: (
            "Require a direct intervention or discriminating measurement for mechanism claims."
        ),
        TraceFaultType.CITATION_CLAIM_MISMATCH: (
            "Require source-located evidence to entail each linked literature claim."
        ),
    }
    rules = [
        TraceGateRule(
            rule_id=f"TG-{index:02d}",
            fault_type=fault_type,
            description=rule_text[fault_type],
            required_artifacts=(
                ["paper", "research-contract-final", "provenance-graph"]
                if fault_type
                in {
                    TraceFaultType.CLAIM_CEILING,
                    TraceFaultType.CITATION_CLAIM_MISMATCH,
                    TraceFaultType.UNSUPPORTED_MECHANISM,
                }
                else [
                    "paper",
                    "research-contract-final",
                    "experimentor-output",
                    "execution-result",
                    "provenance-graph",
                ]
            ),
        )
        for index, fault_type in enumerate(TRACE_FAULT_TYPES, start=1)
    ]
    return TraceStudyContract(
        contract_version="1.0",
        profile=ResearchProfile.TRACE_AUDIT,
        primary_metric="false_acceptance_rate_on_faulty_packages",
        clean_case_metric="clean_package_acceptance_rate",
        conditions=conditions,
        fault_types=list(TRACE_FAULT_TYPES),
        gate_rules=rules,
        paired_design=True,
        blinded_review=True,
        minimum_reviewer_families=1 if pipeline_smoke_test else 2,
        reviewer_separation_policy=(
            "Experimental reviewers must be isolated from benchmark construction, "
            "fault injection, gold adjudication, and final paper writing; the same "
            "model session may not serve both sides."
        ),
        benchmark_min_cases=4 if pipeline_smoke_test else 30,
        gold_label_policy=(
            "Gold labels come from versioned corruption manifests plus deterministic "
            "replay; manifests are hidden from experimental reviewers."
        ),
        leakage_controls=[
            "Hide condition labels and corruption manifests from reviewers.",
            "Keep benchmark construction, reviewer inference, and paper writing sessions isolated.",
            "Randomize condition order with the same package and reviewer configuration.",
        ],
        primary_comparison="C3_TRACE_GATE versus C0_PAPER_ONLY",
        secondary_comparisons=[
            "C1_RAW_ARTIFACTS versus C0_PAPER_ONLY",
            "C2_STRUCTURED_PROVENANCE versus C1_RAW_ARTIFACTS",
            "C3_TRACE_GATE versus C2_STRUCTURED_PROVENANCE",
        ],
        statistical_plan=(
            "Use paired package-level decisions, exact McNemar tests, paired bootstrap "
            "confidence intervals, fault-type strata, and reviewer-model strata."
        ),
        cost_metrics=[
            "review_latency_seconds",
            "input_tokens",
            "output_tokens",
            "human_adjudication_minutes",
        ],
        stopping_rule=(
            "Freeze benchmark size before the main comparison; infrastructure pilots "
            "may test execution only and may not tune claims from treatment outcomes."
        ),
        claim_ids=list(dict.fromkeys(claim_ids)),
    )


def trace_study_contract_issues(
    value: TraceStudyContract,
    *,
    expected_claim_ids: set[str],
) -> list[str]:
    issues: list[str] = []
    condition_ids = [item.condition_id for item in value.conditions]
    if condition_ids != list(TRACE_CONDITIONS):
        issues.append("Trace conditions must be ordered exactly C0, C1, C2, C3")
    by_id = {item.condition_id: item for item in value.conditions}
    expected_flags = {
        TraceReviewCondition.PAPER_ONLY: (False, False),
        TraceReviewCondition.RAW_ARTIFACTS: (False, False),
        TraceReviewCondition.STRUCTURED_PROVENANCE: (True, False),
        TraceReviewCondition.TRACE_GATE: (True, True),
    }
    for condition_id, flags in expected_flags.items():
        condition = by_id.get(condition_id)
        if condition is None:
            continue
        actual = (condition.structured_provenance, condition.deterministic_gate)
        if actual != flags:
            issues.append(f"{condition_id.value} has invalid provenance/gate flags")
    if not value.paired_design or not value.blinded_review:
        issues.append("TRACE_AUDIT requires paired and blinded review")
    if not value.reviewer_separation_policy.strip():
        issues.append("Experimental reviewer separation policy is required")
    if set(value.claim_ids) != expected_claim_ids:
        issues.append(
            f"Trace claim IDs must equal selected claims {sorted(expected_claim_ids)}"
        )
    if len(value.fault_types) != len(set(value.fault_types)):
        issues.append("Trace fault types must be unique")
    rule_faults = {item.fault_type for item in value.gate_rules}
    missing_rules = set(value.fault_types) - rule_faults
    if missing_rules:
        issues.append(f"Fault types without deterministic rules: {sorted(missing_rules)}")
    if value.primary_metric != "false_acceptance_rate_on_faulty_packages":
        issues.append("The frozen primary metric must be false acceptance on faulty packages")
    if not value.gold_label_policy.strip() or not value.leakage_controls:
        issues.append("Gold-label and leakage controls are required")
    return issues


def trace_execution_issues(
    payload: dict[str, Any],
    contract: TraceStudyContract,
    *,
    expected_target_id: str,
    pipeline_smoke_test: bool,
) -> list[str]:
    try:
        result = TraceAuditResultPayload.model_validate(payload)
    except ValidationError as exc:
        return [f"Invalid TRACE_AUDIT result schema: {exc.errors(include_url=False)}"]
    issues: list[str] = []
    if result.study_type != "TRACE_AUDIT":
        issues.append("study_type must be TRACE_AUDIT")
    if result.analysis_target_id != expected_target_id:
        issues.append(
            f"analysis_target_id must be {expected_target_id}, got {result.analysis_target_id}"
        )
    if not result.leakage_check_passed:
        issues.append("The reviewer leakage check did not pass")
    if len(result.corruption_manifest_hash.strip()) < 8:
        issues.append("A versioned corruption_manifest_hash is required")
    if pipeline_smoke_test:
        if result.scientific_claim_valid:
            issues.append("A pipeline smoke test cannot mark its scientific claim valid")
    elif not result.scientific_claim_valid:
        issues.append("A substantive TRACE_AUDIT execution must mark scientific validity")
    cases: dict[str, list[Any]] = defaultdict(list)
    for decision in result.decisions:
        cases[decision.case_id].append(decision)
    if result.benchmark_case_count != len(cases):
        issues.append("benchmark_case_count does not match unique decision case IDs")
    if not pipeline_smoke_test and len(cases) < contract.benchmark_min_cases:
        issues.append(
            f"Main study requires at least {contract.benchmark_min_cases} cases"
        )
    reviewer_models = {item.reviewer_model for item in result.decisions}
    if len(reviewer_models) < contract.minimum_reviewer_families:
        issues.append(
            f"Study requires {contract.minimum_reviewer_families} reviewer model families"
        )
    reviewer_case_rows: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for decision in result.decisions:
        reviewer_case_rows[(decision.reviewer_model, decision.case_id)].append(
            decision
        )
    for (reviewer_model, case_id), decisions in reviewer_case_rows.items():
        condition_ids = [item.condition_id for item in decisions]
        if set(condition_ids) != set(TRACE_CONDITIONS) or len(condition_ids) != 4:
            issues.append(
                f"Reviewer {reviewer_model} case {case_id} does not cover C0-C3 "
                "exactly once"
            )
    for case_id, decisions in cases.items():
        if len({item.gold_faulty for item in decisions}) != 1:
            issues.append(f"Case {case_id} changes its gold label across reviewers or conditions")
        if len({item.gold_fault_type for item in decisions}) != 1:
            issues.append(
                f"Case {case_id} changes its fault type across reviewers or conditions"
            )
        gold_faulty = decisions[0].gold_faulty
        gold_fault_type = decisions[0].gold_fault_type
        if not pipeline_smoke_test and gold_faulty and gold_fault_type is None:
            issues.append(f"Faulty case {case_id} lacks its post-review fault stratum")
        if not pipeline_smoke_test and not gold_faulty and gold_fault_type is not None:
            issues.append(f"Clean case {case_id} has a fault stratum")
    if not pipeline_smoke_test:
        covered_faults = {
            item.gold_fault_type
            for item in result.decisions
            if item.gold_fault_type is not None
        }
        missing_faults = set(contract.fault_types) - covered_faults
        if missing_faults and contract.benchmark_min_cases >= len(contract.fault_types):
            issues.append(
                "Execution results omit registered fault strata: "
                + ", ".join(sorted(item.value for item in missing_faults))
            )
    metrics_by_condition = {item.condition_id: item for item in result.condition_metrics}
    if set(metrics_by_condition) != set(TRACE_CONDITIONS):
        issues.append("condition_metrics must cover C0-C3 exactly once")
    for condition_id in TRACE_CONDITIONS:
        decisions = [
            item for item in result.decisions if item.condition_id == condition_id
        ]
        faulty = [item for item in decisions if item.gold_faulty]
        clean = [item for item in decisions if not item.gold_faulty]
        if not faulty or not clean:
            issues.append(f"{condition_id.value} needs faulty and clean cases")
            continue
        expected_far = sum(item.accepted for item in faulty) / len(faulty)
        expected_clean = sum(item.accepted for item in clean) / len(clean)
        reported = metrics_by_condition.get(condition_id)
        if reported is None:
            continue
        if reported.faulty_cases != len(faulty) or reported.clean_cases != len(clean):
            issues.append(f"{condition_id.value} reports incorrect case counts")
        if abs(reported.false_acceptance_rate - expected_far) > 1e-9:
            issues.append(f"{condition_id.value} false acceptance is not reproducible")
        if abs(reported.clean_acceptance_rate - expected_clean) > 1e-9:
            issues.append(f"{condition_id.value} clean acceptance is not reproducible")
        expected_latency = sum(item.latency_seconds for item in decisions) / len(decisions)
        expected_input = sum(item.input_tokens for item in decisions) / len(decisions)
        expected_output = sum(item.output_tokens for item in decisions) / len(decisions)
        if abs(reported.mean_latency_seconds - expected_latency) > 1e-9:
            issues.append(f"{condition_id.value} latency cost is not reproducible")
        if abs(reported.mean_input_tokens - expected_input) > 1e-9:
            issues.append(f"{condition_id.value} input-token cost is not reproducible")
        if abs(reported.mean_output_tokens - expected_output) > 1e-9:
            issues.append(f"{condition_id.value} output-token cost is not reproducible")
    if not pipeline_smoke_test and contract.benchmark_min_cases >= len(
        contract.fault_types
    ):
        expected_fault_metrics = recomputed_trace_fault_metrics(result, contract)
        reported_fault_metrics = {
            (item.fault_type, item.condition_id): item
            for item in result.fault_type_metrics
        }
        expected_keys = {
            (item.fault_type, item.condition_id) for item in expected_fault_metrics
        }
        if set(reported_fault_metrics) != expected_keys:
            issues.append(
                "fault_type_metrics must cover every registered fault and C0-C3 condition"
            )
        for expected in expected_fault_metrics:
            reported = reported_fault_metrics.get(
                (expected.fault_type, expected.condition_id)
            )
            if reported is None:
                continue
            if reported.decisions != expected.decisions or abs(
                reported.false_acceptance_rate - expected.false_acceptance_rate
            ) > 1e-9:
                issues.append(
                    f"{expected.fault_type.value}/{expected.condition_id.value} "
                    "fault metric is not reproducible"
                )
    expected_comparisons = recomputed_trace_comparisons(result)
    reported_comparisons = {
        item.comparison_id: item for item in result.paired_comparisons
    }
    if set(reported_comparisons) != {
        comparison_id for comparison_id, _, _ in TRACE_COMPARISONS
    }:
        issues.append("paired_comparisons must cover C3/C0, C1/C0, C2/C1, and C3/C2")
    for expected in expected_comparisons:
        reported = reported_comparisons.get(expected.comparison_id)
        if reported is None:
            continue
        if (
            reported.treatment_id != expected.treatment_id
            or reported.baseline_id != expected.baseline_id
        ):
            issues.append(f"{expected.comparison_id} condition identity changed")
            continue
        exact_fields = ("improvement_pairs", "regression_pairs")
        for field_name in exact_fields:
            if getattr(reported, field_name) != getattr(expected, field_name):
                issues.append(
                    f"{expected.comparison_id} {field_name} is not reproducible"
                )
        numeric_fields = (
            "false_acceptance_difference",
            "clean_acceptance_difference",
            "mean_latency_difference",
            "mcnemar_exact_p_value",
            "bootstrap_ci_low",
            "bootstrap_ci_high",
        )
        for field_name in numeric_fields:
            if abs(getattr(reported, field_name) - getattr(expected, field_name)) > 1e-9:
                issues.append(
                    f"{expected.comparison_id} {field_name} is not reproducible"
                )
    return list(dict.fromkeys(issues))


def result_payloads_from_experiment(
    experiment_stage: ExperimentStageResult,
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for execution in experiment_stage.executions:
        for relative_path, content in execution.output_files.items():
            result_id = f"{execution.experiment_id}:{relative_path}"
            if result_id not in execution.result_ids or not relative_path.endswith(".json"):
                continue
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                payloads[result_id] = value
    return payloads


def build_claim_ledger(
    research_contract: ResearchContract,
    experiment_stage: ExperimentStageResult,
) -> ClaimLedger:
    targets = {item.target_id: item for item in research_contract.targets}
    judgments = {
        item.hypothesis_id: item for item in experiment_stage.evaluation.judgments
    }
    result_payloads = result_payloads_from_experiment(experiment_stage)
    entries: list[ClaimLedgerEntry] = []
    for target_id in research_contract.selected_target_ids:
        target = targets[target_id]
        judgment = judgments[target_id]
        if judgment.status in {
            ResearchResultStatus.BEST_SUPPORTED,
            ResearchResultStatus.SUPPORTED,
        }:
            allowed = target.statement
        elif judgment.status == ResearchResultStatus.PARTIALLY_SUPPORTED:
            allowed = f"The bounded evidence partially supports: {target.statement}"
        elif judgment.status in {
            ResearchResultStatus.NOT_SUPPORTED,
            ResearchResultStatus.FALSIFIED,
        }:
            allowed = f"The bounded study did not support: {target.statement}"
        elif judgment.status == ResearchResultStatus.INCONCLUSIVE:
            allowed = f"The bounded evidence is inconclusive for: {target.statement}"
        else:
            allowed = f"No scientific conclusion is permitted for {target_id}."
        forbidden = [
            "Do not generalize beyond the tested benchmark and reviewer models.",
            "Do not convert association or detection performance into a causal mechanism claim.",
            "Do not omit null, negative, falsified, or protocol-violation outcomes.",
        ]
        if research_contract.research_profile == ResearchProfile.TRACE_AUDIT:
            forbidden.append(
                "Do not claim that more context alone caused a TRACE-GATE effect; keep C1, C2, and C3 separate."
            )
        effect_summary = "See the linked canonical Result IDs."
        if research_contract.research_profile == ResearchProfile.TRACE_AUDIT:
            trace_result = None
            for result_id in judgment.result_ids:
                payload = result_payloads.get(result_id)
                if payload is None:
                    continue
                try:
                    trace_result = TraceAuditResultPayload.model_validate(payload)
                except ValidationError:
                    continue
                break
            if trace_result is not None:
                ledger_comparison_id = (
                    "C3_vs_C0" if target_id.startswith("A") else "C3_vs_C2"
                )
                primary = next(
                    (
                        item
                        for item in trace_result.paired_comparisons
                        if item.comparison_id == ledger_comparison_id
                    ),
                    None,
                )
                if primary is not None:
                    comparison_label = (
                        f"{primary.treatment_id.value.split('_', 1)[0]}-"
                        f"{primary.baseline_id.value.split('_', 1)[0]}"
                    )
                    effect_summary = (
                        f"{comparison_label} false-acceptance difference={primary.false_acceptance_difference:.4f}; "
                        f"95% case-clustered paired bootstrap CI=[{primary.bootstrap_ci_low:.4f}, "
                        f"{primary.bootstrap_ci_high:.4f}]; exact McNemar "
                        f"p={primary.mcnemar_exact_p_value:.4f}; clean-acceptance "
                        f"difference={primary.clean_acceptance_difference:.4f}; mean "
                        f"latency difference={primary.mean_latency_difference:.4f}s; "
                        f"human adjudication={trace_result.human_adjudication_minutes:.2f} min."
                    )
        entries.append(
            ClaimLedgerEntry(
                claim_id=target_id,
                target_id=target_id,
                status=judgment.status,
                allowed_claim=allowed,
                effect_summary=effect_summary,
                forbidden_generalizations=forbidden,
                evidence_ids=target.evidence_ids,
                result_ids=judgment.result_ids,
            )
        )
    return ClaimLedger(
        ledger_version="1.0",
        research_profile=research_contract.research_profile,
        claim_ceiling=research_contract.claim_ceiling,
        entries=entries,
    )


def claim_ledger_issues(
    ledger: ClaimLedger,
    research_contract: ResearchContract,
) -> list[str]:
    issues: list[str] = []
    entry_ids = [item.claim_id for item in ledger.entries]
    if len(entry_ids) != len(set(entry_ids)):
        issues.append("Claim Ledger IDs must be unique")
    if set(entry_ids) != set(research_contract.selected_target_ids):
        issues.append("Claim Ledger must cover every selected target exactly once")
    if ledger.claim_ceiling != research_contract.claim_ceiling:
        issues.append("Claim Ledger changed the frozen claim ceiling")
    if ledger.research_profile != research_contract.research_profile:
        issues.append("Claim Ledger changed the research profile")
    known_evidence = {item.evidence_id for item in research_contract.evidence}
    for entry in ledger.entries:
        if not entry.result_ids:
            issues.append(f"Claim Ledger entry {entry.claim_id} has no Result ID")
        if (
            research_contract.research_profile == ResearchProfile.TRACE_AUDIT
            and not re.match(
                r"C[0-3]-C[0-3] false-acceptance",
                entry.effect_summary,
            )
        ):
            issues.append(
                f"Claim Ledger entry {entry.claim_id} lacks verified TRACE_AUDIT effects"
            )
        unknown = set(entry.evidence_ids) - known_evidence
        if unknown:
            issues.append(
                f"Claim Ledger entry {entry.claim_id} has unknown Evidence IDs {sorted(unknown)}"
            )
    return issues


def recomputed_trace_metrics(
    result: TraceAuditResultPayload,
) -> list[TraceConditionMetrics]:
    metrics: list[TraceConditionMetrics] = []
    for condition_id in TRACE_CONDITIONS:
        rows = [item for item in result.decisions if item.condition_id == condition_id]
        faulty = [item for item in rows if item.gold_faulty]
        clean = [item for item in rows if not item.gold_faulty]
        metrics.append(
            TraceConditionMetrics(
                condition_id=condition_id,
                false_acceptance_rate=(
                    sum(item.accepted for item in faulty) / len(faulty) if faulty else 0
                ),
                clean_acceptance_rate=(
                    sum(item.accepted for item in clean) / len(clean) if clean else 0
                ),
                faulty_cases=len(faulty),
                clean_cases=len(clean),
                mean_latency_seconds=(
                    sum(item.latency_seconds for item in rows) / len(rows)
                    if rows
                    else 0
                ),
                mean_input_tokens=(
                    sum(item.input_tokens for item in rows) / len(rows)
                    if rows
                    else 0
                ),
                mean_output_tokens=(
                    sum(item.output_tokens for item in rows) / len(rows)
                    if rows
                    else 0
                ),
            )
        )
    return metrics


def recomputed_trace_fault_metrics(
    result: TraceAuditResultPayload,
    contract: TraceStudyContract,
) -> list[TraceFaultConditionMetrics]:
    metrics: list[TraceFaultConditionMetrics] = []
    for fault_type in contract.fault_types:
        for condition_id in TRACE_CONDITIONS:
            rows = [
                item
                for item in result.decisions
                if item.gold_fault_type == fault_type
                and item.condition_id == condition_id
            ]
            if not rows:
                continue
            metrics.append(
                TraceFaultConditionMetrics(
                    fault_type=fault_type,
                    condition_id=condition_id,
                    false_acceptance_rate=(
                        sum(item.accepted for item in rows) / len(rows)
                    ),
                    decisions=len(rows),
                )
            )
    return metrics


def recomputed_trace_comparisons(
    result: TraceAuditResultPayload,
) -> list[TracePairedComparison]:
    rows = {
        (item.reviewer_model, item.case_id, item.condition_id): item
        for item in result.decisions
    }
    pair_keys = sorted(
        {(item.reviewer_model, item.case_id) for item in result.decisions}
    )
    comparisons: list[TracePairedComparison] = []
    for comparison_id, treatment_id, baseline_id in TRACE_COMPARISONS:
        paired = [
            (
                rows[(reviewer, case_id, treatment_id)],
                rows[(reviewer, case_id, baseline_id)],
            )
            for reviewer, case_id in pair_keys
            if (reviewer, case_id, treatment_id) in rows
            and (reviewer, case_id, baseline_id) in rows
        ]
        faulty = [pair for pair in paired if pair[0].gold_faulty]
        clean = [pair for pair in paired if not pair[0].gold_faulty]
        faulty_differences = [
            int(treatment.accepted) - int(baseline.accepted)
            for treatment, baseline in faulty
        ]
        faulty_case_ids = [treatment.case_id for treatment, _ in faulty]
        clean_differences = [
            int(treatment.accepted) - int(baseline.accepted)
            for treatment, baseline in clean
        ]
        improvement = sum(
            baseline.accepted and not treatment.accepted
            for treatment, baseline in faulty
        )
        regression = sum(
            treatment.accepted and not baseline.accepted
            for treatment, baseline in faulty
        )
        ci_low, ci_high = _paired_bootstrap_ci(
            faulty_differences,
            cluster_ids=faulty_case_ids,
        )
        comparisons.append(
            TracePairedComparison(
                comparison_id=comparison_id,
                treatment_id=treatment_id,
                baseline_id=baseline_id,
                false_acceptance_difference=(
                    sum(faulty_differences) / len(faulty_differences)
                    if faulty_differences
                    else 0
                ),
                clean_acceptance_difference=(
                    sum(clean_differences) / len(clean_differences)
                    if clean_differences
                    else 0
                ),
                mean_latency_difference=(
                    sum(
                        treatment.latency_seconds - baseline.latency_seconds
                        for treatment, baseline in paired
                    )
                    / len(paired)
                    if paired
                    else 0
                ),
                improvement_pairs=improvement,
                regression_pairs=regression,
                mcnemar_exact_p_value=_mcnemar_exact_p(improvement, regression),
                bootstrap_ci_low=ci_low,
                bootstrap_ci_high=ci_high,
            )
        )
    return comparisons


def _mcnemar_exact_p(improvement: int, regression: int) -> float:
    total = improvement + regression
    if total == 0:
        return 1.0
    tail = sum(
        math.comb(total, value) for value in range(min(improvement, regression) + 1)
    ) / (2**total)
    return min(1.0, 2 * tail)


def _paired_bootstrap_ci(
    values: list[int],
    *,
    cluster_ids: list[str] | None = None,
) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    generator = random.Random(1729)
    clusters: dict[str, list[int]] = defaultdict(list)
    if cluster_ids is None:
        cluster_ids = [str(index) for index in range(len(values))]
    if len(cluster_ids) != len(values):
        raise ValueError("Bootstrap cluster IDs must align with paired differences")
    for cluster_id, value in zip(cluster_ids, values):
        clusters[cluster_id].append(value)
    cluster_values = list(clusters.values())
    draws = []
    for _ in range(2000):
        sampled_clusters = [
            cluster_values[generator.randrange(len(cluster_values))]
            for _ in cluster_values
        ]
        sample = [value for cluster in sampled_clusters for value in cluster]
        draws.append(sum(sample) / len(sample))
    draws.sort()
    return draws[int(0.025 * (len(draws) - 1))], draws[
        int(0.975 * (len(draws) - 1))
    ]
