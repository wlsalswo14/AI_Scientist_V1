from __future__ import annotations

import pytest

from ai_scientist.cli import build_parser
from ai_scientist.config import Settings
from ai_scientist.schemas import (
    ExecutionResult,
    ExperimentContract,
    ExperimentorOutput,
    GeneratedFile,
    HypothesisExperimentSpec,
    ResearchProfile,
    TraceAuditResultPayload,
    TraceFaultType,
    TraceReviewCondition,
    TraceReviewDecision,
    TraceReviewerDecisionBatch,
)
from ai_scientist.trace_audit import (
    TRACE_CONDITIONS,
    build_trace_study_contract,
    recomputed_trace_comparisons,
    recomputed_trace_metrics,
    reviewer_decision_batch_issues,
    trace_contract_fingerprint,
    trace_execution_issues,
    trace_study_contract_issues,
)
from ai_scientist.validation import validate_execution_bundle, validate_experiment_contract


def _trace_result() -> TraceAuditResultPayload:
    decisions = []
    for case_id, gold_faulty in (
        ("fault-1", True),
        ("fault-2", True),
        ("clean-1", False),
        ("clean-2", False),
    ):
        for condition in TRACE_CONDITIONS:
            if not gold_faulty:
                accepted = True
            elif condition == TraceReviewCondition.PAPER_ONLY:
                accepted = True
            elif condition in {
                TraceReviewCondition.RAW_ARTIFACTS,
                TraceReviewCondition.STRUCTURED_PROVENANCE,
            }:
                accepted = case_id == "fault-1"
            else:
                accepted = False
            decisions.append(
                TraceReviewDecision(
                    case_id=case_id,
                    condition_id=condition,
                    gold_faulty=gold_faulty,
                    gold_fault_type=(
                        TraceFaultType.RESULT_DIRECTION
                        if case_id == "fault-1"
                        else (
                            TraceFaultType.METRIC_CLAIM_MISMATCH
                            if case_id == "fault-2"
                            else None
                        )
                    ),
                    accepted=accepted,
                    reviewer_model="mock-rule-reviewer",
                    confidence=0.8,
                    detected_fault_types=(
                        [TraceFaultType.RESULT_DIRECTION]
                        if gold_faulty and not accepted
                        else []
                    ),
                    latency_seconds=0.1,
                    input_tokens=100,
                    output_tokens=20,
                )
            )
    seed = TraceAuditResultPayload(
        study_type="TRACE_AUDIT",
        study_mode="PIPELINE_SMOKE_TEST",
        scientific_claim_valid=False,
        analysis_target_id="A1",
        benchmark_case_count=4,
        corruption_manifest_hash="0123456789abcdef",
        leakage_check_passed=True,
        human_adjudication_minutes=0,
        decisions=decisions,
        condition_metrics=[
            {
                "condition_id": condition,
                "false_acceptance_rate": 0,
                "clean_acceptance_rate": 0,
                "faulty_cases": 0,
                "clean_cases": 0,
                "mean_latency_seconds": 0,
                "mean_input_tokens": 0,
                "mean_output_tokens": 0,
            }
            for condition in TRACE_CONDITIONS
        ],
        paired_comparisons=[
            {
                "comparison_id": comparison_id,
                "treatment_id": treatment,
                "baseline_id": baseline,
                "false_acceptance_difference": 0,
                "clean_acceptance_difference": 0,
                "mean_latency_difference": 0,
                "improvement_pairs": 0,
                "regression_pairs": 0,
                "mcnemar_exact_p_value": 1,
                "bootstrap_ci_low": 0,
                "bootstrap_ci_high": 0,
            }
            for comparison_id, treatment, baseline in (
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
        ],
    )
    return seed.model_copy(
        update={
            "condition_metrics": recomputed_trace_metrics(seed),
            "paired_comparisons": recomputed_trace_comparisons(seed),
        }
    )


def _experiment_contract(trace_contract) -> ExperimentContract:
    return ExperimentContract(
        contract_version="1.0",
        hypothesis_ids=["A1"],
        dataset_plan="Use fixed paired packages.",
        shared_protocol=["Blind manifests", "Run C0-C3"],
        metrics=["false acceptance", "clean acceptance", "review cost"],
        seeds=[7],
        statistical_plan="Exact McNemar plus paired bootstrap.",
        stopping_rule="Fixed case count.",
        hypothesis_specs=[
            HypothesisExperimentSpec(
                hypothesis_id="A1",
                unique_prediction="C3 has lower false acceptance than C0.",
                manipulation="Reviewer information condition.",
                controls=["package", "reviewer", "gold label"],
                measurement="false acceptance rate",
                expected_pattern="C3 < C0",
                rejection_condition="No paired reduction.",
            )
        ],
        trace_study_contract=trace_contract,
    )


def test_trace_audit_profile_requires_dual_directors() -> None:
    with pytest.raises(ValueError, match="Dual-Director"):
        Settings(
            research_profile="trace_audit",
            dual_director_enabled=False,
        ).validate()


def test_substantive_trace_audit_requires_external_reviewer_decisions() -> None:
    with pytest.raises(ValueError, match="reviewer-decision"):
        Settings(
            research_profile="trace_audit",
            allow_code_execution=True,
            pipeline_smoke_test=False,
        ).validate()


def test_trace_review_preparation_can_precede_external_decisions() -> None:
    Settings(
        research_profile="trace_audit",
        allow_code_execution=True,
        pipeline_smoke_test=False,
        trace_prepare_only=True,
    ).validate()


def test_cli_accepts_trace_audit_profile() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--question",
            "Does provenance reduce false acceptance?",
            "--research-profile",
            "trace-audit",
        ]
    )
    assert args.research_profile == "trace-audit"


def test_cli_accepts_preparation_and_resume_run_id() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--question",
            "Does provenance reduce false acceptance?",
            "--research-profile",
            "trace-audit",
            "--prepare-trace-review",
            "--run-id",
            "run-fixed",
        ]
    )
    assert args.prepare_trace_review is True
    assert args.run_id == "run-fixed"


def test_frozen_trace_contract_has_exact_c0_c3_ablation() -> None:
    contract = build_trace_study_contract(
        ["A1", "X1"],
        pipeline_smoke_test=False,
    )

    assert contract.profile == ResearchProfile.TRACE_AUDIT
    assert [item.condition_id for item in contract.conditions] == list(
        TRACE_CONDITIONS
    )
    assert contract.benchmark_min_cases == 30
    assert not trace_study_contract_issues(
        contract,
        expected_claim_ids={"A1", "X1"},
    )


def test_substantive_execution_accepts_two_reviewer_families_per_case() -> None:
    seed = _trace_result()
    duplicated = [
        *seed.decisions,
        *[
            item.model_copy(update={"reviewer_model": "second-reviewer"})
            for item in seed.decisions
        ],
    ]
    result = seed.model_copy(
        update={
            "study_mode": "MAIN_STUDY",
            "scientific_claim_valid": True,
            "decisions": duplicated,
        }
    )
    result = result.model_copy(
        update={
            "condition_metrics": recomputed_trace_metrics(result),
            "paired_comparisons": recomputed_trace_comparisons(result),
        }
    )
    contract = build_trace_study_contract(["A1"], pipeline_smoke_test=True).model_copy(
        update={"benchmark_min_cases": 4, "minimum_reviewer_families": 2}
    )

    assert not trace_execution_issues(
        result.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=False,
    )


def test_experiment_designer_cannot_change_frozen_trace_contract() -> None:
    frozen = build_trace_study_contract(
        ["A1"],
        pipeline_smoke_test=True,
    )
    changed = frozen.model_copy(update={"benchmark_min_cases": 2})

    validation = validate_experiment_contract(
        _experiment_contract(changed),
        selected_target_ids={"A1"},
        expected_trace_study_contract=frozen,
    )

    assert not validation.valid
    assert any(
        issue.code == "TRACE_CONTRACT_DRIFT" for issue in validation.issues
    )


def test_trace_result_metrics_are_recomputed_from_case_decisions() -> None:
    contract = build_trace_study_contract(
        ["A1"],
        pipeline_smoke_test=True,
    )
    result = _trace_result()
    assert not trace_execution_issues(
        result.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=True,
    )

    bad_metrics = list(result.condition_metrics)
    bad_metrics[0] = bad_metrics[0].model_copy(
        update={"false_acceptance_rate": 0.25}
    )
    corrupted = result.model_copy(update={"condition_metrics": bad_metrics})
    issues = trace_execution_issues(
        corrupted.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=True,
    )

    assert any("false acceptance is not reproducible" in item for item in issues)

    bad_cost = list(result.condition_metrics)
    bad_cost[3] = bad_cost[3].model_copy(
        update={"mean_latency_seconds": 99.0}
    )
    cost_corrupted = result.model_copy(update={"condition_metrics": bad_cost})
    cost_issues = trace_execution_issues(
        cost_corrupted.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=True,
    )
    assert any("latency cost is not reproducible" in item for item in cost_issues)

    bad_comparisons = list(result.paired_comparisons)
    bad_comparisons[0] = bad_comparisons[0].model_copy(
        update={"mcnemar_exact_p_value": 0.01}
    )
    comparison_corrupted = result.model_copy(
        update={"paired_comparisons": bad_comparisons}
    )
    comparison_issues = trace_execution_issues(
        comparison_corrupted.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=True,
    )
    assert any("mcnemar_exact_p_value" in item for item in comparison_issues)


def test_smoke_result_cannot_claim_scientific_validity() -> None:
    contract = build_trace_study_contract(
        ["A1"],
        pipeline_smoke_test=True,
    )
    result = _trace_result().model_copy(update={"scientific_claim_valid": True})

    issues = trace_execution_issues(
        result.model_dump(mode="json"),
        contract,
        expected_target_id="A1",
        pipeline_smoke_test=True,
    )

    assert any("smoke test cannot mark" in item for item in issues)


def test_external_reviewer_batch_is_bound_to_frozen_contract() -> None:
    contract = build_trace_study_contract(
        ["A1"],
        pipeline_smoke_test=True,
    )
    result = _trace_result()
    batch = TraceReviewerDecisionBatch(
        batch_version="1.0",
        trace_contract_fingerprint=trace_contract_fingerprint(contract),
        reviewer_models=["mock-rule-reviewer"],
        corruption_manifest_hash=result.corruption_manifest_hash,
        leakage_check_passed=True,
        decisions=result.decisions,
    )
    assert not reviewer_decision_batch_issues(batch, contract)

    drifted = batch.model_copy(update={"trace_contract_fingerprint": "wrong"})
    issues = reviewer_decision_batch_issues(drifted, contract)
    assert any("does not match" in item for item in issues)


def test_execution_bundle_cannot_change_frozen_external_decisions() -> None:
    contract = build_trace_study_contract(["A1"], pipeline_smoke_test=True)
    seed = _trace_result()
    batch = TraceReviewerDecisionBatch(
        batch_version="1.0",
        trace_contract_fingerprint=trace_contract_fingerprint(contract),
        reviewer_models=["mock-rule-reviewer"],
        corruption_manifest_hash=seed.corruption_manifest_hash,
        leakage_check_passed=True,
        decisions=seed.decisions,
    )
    changed_rows = list(seed.decisions)
    changed_rows[0] = changed_rows[0].model_copy(
        update={"accepted": not changed_rows[0].accepted}
    )
    changed = seed.model_copy(update={"decisions": changed_rows})
    changed = changed.model_copy(
        update={
            "condition_metrics": recomputed_trace_metrics(changed),
            "paired_comparisons": recomputed_trace_comparisons(changed),
        }
    )
    output = ExperimentorOutput(
        hypothesis_id="A1",
        experiment_id="EXP-A1",
        files=[GeneratedFile(path="experiment.py", content="print('done')\n")],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )
    execution = ExecutionResult(
        hypothesis_id="A1",
        experiment_id="EXP-A1",
        exit_code=0,
        stdout="",
        stderr="",
        output_files={"result.json": changed.model_dump_json()},
        result_ids=["EXP-A1:result.json"],
        code_hash="abc123",
        workspace="workspace",
    )

    report = validate_execution_bundle(
        selected_target_ids={"A1"},
        experimentor_outputs=[output],
        executions=[execution],
        expected_trace_study_contract=contract,
        expected_trace_reviewer_decisions=batch,
        pipeline_smoke_test=True,
    )

    assert any(issue.code == "TRACE_EXTERNAL_DECISION_DRIFT" for issue in report.issues)
