from __future__ import annotations

import json

from ai_scientist.schemas import TraceReviewCondition
from ai_scientist.trace_audit import build_trace_study_contract
from tools.run_trace_external_review import (
    SEMANTIC_BOUNDARY_FAULTS,
    build_cases,
    review_session_index,
    visible_materials,
)


def test_submission_benchmark_is_frozen_balanced_and_blinded() -> None:
    contract = build_trace_study_contract(["A1", "X1"], pipeline_smoke_test=False)
    cases = build_cases(contract)

    assert len(cases) == 30
    assert sum(case["gold_faulty"] for case in cases) == 15
    assert sum(not case["gold_faulty"] for case in cases) == 15
    assert len({case["pair_id"] for case in cases}) == 15
    assert {case["fault_type"] for case in cases if case["fault_type"]} == {
        item.value for item in contract.fault_types
    }
    for case in cases:
        for condition in TraceReviewCondition:
            visible = json.dumps(
                visible_materials(case, condition),
                ensure_ascii=False,
            )
            assert "gold_faulty" not in visible
            assert "condition_id" not in visible
            assert condition.value not in visible


def test_trace_gate_has_a_frozen_semantic_boundary() -> None:
    contract = build_trace_study_contract(["A1"], pipeline_smoke_test=False)
    for case in build_cases(contract):
        failures = [
            row for row in case["gate_report"]["checks"] if row["status"] == "FAIL"
        ]
        semantic = case["fault_type"] in SEMANTIC_BOUNDARY_FAULTS
        expected_failures = int(case["gold_faulty"] and not semantic)
        assert len(failures) == expected_failures
        assert case["gate_report"]["overall_integrity"] == (
            "FAIL" if expected_failures else "PASS"
        )


def test_frozen_36_case_study_has_four_semantic_faults() -> None:
    contract = build_trace_study_contract(["A1", "X1"], pipeline_smoke_test=False)
    cases = build_cases(contract, 36)

    faulty = [case for case in cases if case["gold_faulty"]]
    assert len(faulty) == 18
    assert sum(case["fault_type"] in SEMANTIC_BOUNDARY_FAULTS for case in faulty) == 4
    assert sum(case["gate_report"]["overall_integrity"] == "FAIL" for case in faulty) == 14


def test_eight_review_sessions_cross_conditions_with_balanced_shards() -> None:
    contract = build_trace_study_contract(["A1"], pipeline_smoke_test=False)
    cases = build_cases(contract, 36)
    groups: dict[int, list[tuple[dict, int]]] = {index: [] for index in range(8)}
    for case in cases:
        for condition_index in range(4):
            index = review_session_index(
                pair_id=case["pair_id"],
                pair_variant=case["pair_variant"],
                condition_index=condition_index,
            )
            groups[index].append((case, condition_index))

    for index, rows in groups.items():
        assert len(rows) == 18
        assert {condition for _, condition in rows} == {index // 2}
        assert len({case["pair_id"] for case, _ in rows}) == 18
        assert sum(case["gold_faulty"] for case, _ in rows) == 9
