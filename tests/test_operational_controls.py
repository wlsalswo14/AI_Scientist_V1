import os
import sys

import pytest
from pydantic import BaseModel, ConfigDict, computed_field

from ai_scientist.artifacts import ArtifactStore
from ai_scientist.cli import build_parser
from ai_scientist.config import Settings
from ai_scientist.progress import ProgressTracker, ProgressVector, compare_progress
from ai_scientist.runtime import (
    DeadlinePolicy,
    RuntimeLifecycle,
    RuntimePhase,
    RuntimeStateStore,
)
from ai_scientist.llm import _is_usage_limit_error
from ai_scientist.schemas import (
    ClaimUnit,
    EvidenceLocation,
    EvidenceUnit,
    ResearchCondition,
    ResearchContract,
    ResearchMode,
    ResearchPredictionCell,
    ResearchReadiness,
    ResearchTarget,
    ResearchTargetType,
    SupportRelation,
    VerificationStatus,
)
from ai_scientist.success_contract import (
    build_executable_success_contract,
    verify_success_contract,
)
from ai_scientist.watchdog import RuntimeWatchdog
from ai_scientist.workflows.experiment import ExperimentWorkflow


def test_cli_preserves_repeatable_research_constraints() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--question",
            "Does the intervention help?",
            "--constraint",
            "36 total packages",
            "--constraint",
            "CPU only",
        ]
    )

    assert args.constraint == ["36 total packages", "CPU only"]


class ComputedCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int

    @computed_field
    @property
    def doubled(self) -> int:
        return self.value * 2


def test_checkpoint_round_trip_excludes_computed_fields(tmp_path) -> None:
    store = ArtifactStore(tmp_path, "run-computed-checkpoint")
    store.checkpoint("computed", ComputedCheckpoint(value=3))

    restored = store.load_checkpoint("computed", ComputedCheckpoint)

    assert restored is not None
    assert restored.value == 3
    assert restored.doubled == 6


def test_resumed_run_rejects_a_different_research_objective(tmp_path) -> None:
    settings = Settings(watchdog_enabled=False)
    store = RuntimeStateStore(tmp_path / "run-objective", "run-objective")
    store.initialize(
        "Does B improve Y over A?",
        settings,
        objective="Explain the bounded effect.",
    )

    with pytest.raises(ValueError, match="original research objective"):
        store.initialize(
            "Does B improve Y over A?",
            settings,
            objective="A different objective.",
        )


def test_resumed_run_rejects_a_different_research_depth(tmp_path) -> None:
    store = RuntimeStateStore(tmp_path / "run-depth", "run-depth")
    store.initialize(
        "Does B improve Y over A?",
        Settings(watchdog_enabled=False, research_depth="thesis"),
        objective="Explain the bounded effect.",
    )

    with pytest.raises(ValueError, match="original research depth"):
        store.initialize(
            "Does B improve Y over A?",
            Settings(watchdog_enabled=False, research_depth="publication"),
            objective="Explain the bounded effect.",
        )


def test_targeted_upstream_experiment_repair_preserves_unaffected_targets() -> None:
    affected = ExperimentWorkflow._initial_affected_targets(
        selected={"A1", "A2", "X3"},
        resume_checkpoint=None,
        requested={"A2"},
    )
    generated = ExperimentWorkflow._targets_to_generate(
        selected=["A1", "A2", "X3"],
        affected=affected,
        current_output_ids={"A1", "A2", "X3"},
    )

    assert affected == {"A2"}
    assert generated == ["A2"]


def ready_contract() -> ResearchContract:
    evidence = EvidenceUnit(
        evidence_id="E1",
        title="Optimizer comparison",
        authors=["A. Author"],
        year=2025,
        url="https://example.org/paper",
        evidence_type="paper",
        location=EvidenceLocation(section="Results", table="Table 2"),
        verbatim_excerpt="Optimizer B reduced validation loss in the tested setting.",
        context_summary="A bounded optimizer comparison.",
        verification_status=VerificationStatus.FULL_TEXT_VERIFIED,
    )
    target = ResearchTarget(
        target_id="T1",
        target_type=ResearchTargetType.TEST_CLAIM,
        statement="Muon lowers validation loss relative to AdamW.",
        null_statement="Muon does not lower validation loss relative to AdamW.",
        rationale="Direct comparison",
        mechanism="",
        distinctive_prediction="The paired Muon-AdamW loss difference is negative.",
        falsification_condition="The interval does not clear the fixed threshold.",
        alternative_explanations=["Unequal tuning"],
        positive_result_value="Evidence for improvement in the tested scope.",
        negative_result_value="Evidence that B is worse in the tested scope.",
        null_result_value="A bounded null result constrains the effect size.",
        minimum_experiment="Matched paired runs.",
        required_data="Fixed train/validation split.",
        compute_estimate="Two optimizers over two seeds.",
        uncertainties=["Seed variance"],
        evidence_ids=["E1"],
    )
    return ResearchContract(
        contract_version="1.0",
        original_question="Does B improve validation loss over A?",
        research_mode=ResearchMode.DIRECT_TEST,
        readiness=ResearchReadiness.TEST_READY,
        selected_domain="small-model optimization",
        scope="AdamW versus Muon on a fixed TinyStories GPT workload",
        mode_rationale="The question contains a direct claim.",
        claim_ceiling="Only the tested model and budget.",
        evidence=[evidence],
        claims=[
            ClaimUnit(
                claim_id="C1",
                text="Prior work reports an optimizer difference.",
                claim_type="prior result",
                importance="CRITICAL",
                evidence_ids=["E1"],
                support_relation=SupportRelation.DIRECTLY_SUPPORTED,
                director_inference=False,
                inference_explanation="",
            )
        ],
        targets=[target],
        selected_target_ids=["T1"],
        prediction_matrix=[
            ResearchCondition(
                condition_id="COND1",
                description="Matched training",
                controlled_variables=["model", "data", "seed"],
                manipulated_variables=["optimizer"],
                measurement="paired validation-loss difference",
                decision_threshold="fixed minimum meaningful effect of 0.010",
                predictions=[
                    ResearchPredictionCell(
                        target_id="T1",
                        direction="B lower",
                        expected_pattern="B-A is below the threshold",
                        rejection_condition="interval fails the threshold",
                    )
                ],
            )
        ],
        search_limitations=[],
    )


def test_executable_success_contract_checks_source_traceability() -> None:
    contract = ready_contract()
    executable = build_executable_success_contract(contract)
    report = verify_success_contract(executable, contract)
    assert report.passed

    placeholder_target = contract.targets[0].model_copy(
        update={"statement": "Optimizer B lowers loss relative to optimizer A."}
    )
    placeholder_contract = contract.model_copy(
        update={
            "scope": "optimizer A/B comparison",
            "targets": [placeholder_target],
        }
    )
    placeholder_report = verify_success_contract(executable, placeholder_contract)
    assert not placeholder_report.passed
    assert any(
        "placeholders" in item for item in placeholder_report.failure_messages
    )

    mapped_contract = contract.model_copy(
        update={
            "scope": (
                "Assumption-based operationalization: optimizer A = AdamW, "
                "optimizer B = Lion. Compare AdamW and Lion on a fixed workload."
            )
        }
    )
    mapped_report = verify_success_contract(executable, mapped_contract)
    assert mapped_report.passed

    unlocated = contract.evidence[0].model_copy(
        update={
            "location": EvidenceLocation(),
            "verification_status": VerificationStatus.UNVERIFIED,
        }
    )
    invalid = contract.model_copy(update={"evidence": [unlocated]})
    failed = verify_success_contract(executable, invalid)
    assert not failed.passed
    assert any("source location" in item for item in failed.failure_messages)
    assert any("UNVERIFIED" in item for item in failed.failure_messages)
    evidence_result = next(
        item for item in failed.results if item.criterion_id == "SC-EVIDENCE"
    )
    assert evidence_result.affected_target_ids == ["T1"]


def test_progress_tracker_detects_repeated_zero_progress(tmp_path) -> None:
    store = ArtifactStore(tmp_path, "run-progress")
    tracker = ProgressTracker(
        store,
        "paper",
        lower_is_better={"fatal_issues"},
        max_stagnant_rounds=2,
    )
    vector = ProgressVector(
        stage="paper",
        metrics={"supported_claims": 2.0, "fatal_issues": 1.0},
    )
    first = tracker.record(vector, round_number=1)
    second = tracker.record(vector, round_number=2)
    third = tracker.record(vector, round_number=3)

    assert first.delta.forward_progress
    assert second.delta.stagnant
    assert tracker.should_stop(third)

    improved = compare_progress(
        ProgressVector(
            stage="paper",
            metrics={"supported_claims": 2.0, "fatal_issues": 0.0},
        ),
        vector,
        lower_is_better={"fatal_issues"},
    )
    assert improved.forward_progress


def test_stale_status_cascades_without_rewriting_artifacts(tmp_path) -> None:
    store = ArtifactStore(tmp_path, "run-stale")
    evidence_id = store.save("evidence", {"value": 1})
    result_id = store.save(
        "result",
        {"value": 2},
        dependencies=[evidence_id],
    )
    claim_id = store.save(
        "paper-claim",
        {"value": 3},
        dependencies=[result_id],
    )

    stale = store.invalidate(
        [result_id],
        reason="independent calculation failed",
        cascade=True,
    )

    assert evidence_id not in stale
    assert store.status_of(evidence_id) == "VALID"
    assert store.status_of(result_id) == "STALE"
    assert store.status_of(claim_id) == "STALE"
    assert store.artifact_envelope(result_id)["payload"] == {"value": 2}


def test_deadline_policy_reserves_downstream_time(tmp_path) -> None:
    settings = Settings(
        max_wall_clock_seconds=100,
        minimum_experiment_window_seconds=20,
        paper_reserve_seconds=30,
        finalization_reserve_seconds=10,
    )
    state = RuntimeStateStore(tmp_path / "run-deadline", "run-deadline")
    state.initialize("question", settings)
    policy = DeadlinePolicy(state, settings)

    assert policy.can_start(RuntimePhase.RESEARCH_PLANNING).allowed
    state.request_finalization("competition deadline")
    assert not policy.can_start(RuntimePhase.EXPERIMENT).allowed
    assert policy.can_start(RuntimePhase.FINALIZATION).allowed


def test_runtime_state_persists_pending_feedback_across_restart(tmp_path) -> None:
    settings = Settings()
    state = RuntimeStateStore(tmp_path / "run-feedback", "run-feedback")
    state.initialize("question", settings)
    state.add_feedback("research", "repair the distinctive prediction")

    reopened = RuntimeStateStore(tmp_path / "run-feedback", "run-feedback")
    assert reopened.load().pending_feedback["research"] == [
        "repair the distinctive prediction"
    ]
    reopened.clear_feedback("research")
    assert "research" not in reopened.load().pending_feedback


def test_parallel_model_heartbeat_keeps_unfinished_session_visible(tmp_path) -> None:
    settings = Settings()
    state = RuntimeStateStore(tmp_path / "run-models", "run-models")
    state.initialize("question", settings)

    state.model_heartbeat("evaluator-a", "started")
    state.model_heartbeat("evaluator-b", "started")
    state.model_heartbeat("evaluator-b", "completed")

    current = state.load()
    assert current.active_model_sessions == ["evaluator-a"]
    assert current.next_action == "models:active:evaluator-a"
    assert current.last_model_event == "model:evaluator-b:completed"


def test_manual_resume_clears_sessions_from_an_inactive_process(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(watchdog_enabled=False)
    run_dir = tmp_path / "run-resume"
    state = RuntimeStateStore(run_dir, "run-resume")
    state.initialize("question", settings)
    stale = state.load().model_copy(
        update={
            "process_id": 987654,
            "active_model_sessions": ["stale-evaluator"],
        }
    )
    state.save(stale)
    monkeypatch.setattr("ai_scientist.runtime._process_is_alive", lambda _: False)

    resumed = RuntimeStateStore(run_dir, "run-resume").initialize(
        "question",
        settings,
    )

    assert resumed.restart_count == 1
    assert resumed.active_model_sessions == []
    assert resumed.last_model_event == "runtime:resumed"
    assert resumed.process_id == os.getpid()

    state.model_heartbeat("evaluator-a", "completed")
    assert state.load().active_model_sessions == []


def test_usage_limit_detection_is_specific() -> None:
    assert _is_usage_limit_error("ERROR: You've hit your usage limit.")
    assert not _is_usage_limit_error("HTTP 429: temporary rate limit")


def test_runtime_watchdog_restarts_a_real_failed_process(tmp_path) -> None:
    settings = Settings(
        max_wall_clock_seconds=60,
        minimum_experiment_window_seconds=0,
        paper_reserve_seconds=0,
        finalization_reserve_seconds=0,
        watchdog_check_interval_seconds=0.05,
        watchdog_stale_seconds=10,
        watchdog_max_restarts=2,
    )
    run_id = "run-watchdog"
    state = RuntimeStateStore(tmp_path / run_id, run_id)
    state.initialize("question", settings)
    watchdog = RuntimeWatchdog(settings, state)
    child = (
        "import os,sys; from pathlib import Path; "
        "from ai_scientist.runtime import RuntimeStateStore,RuntimeLifecycle; "
        "run_id=os.environ['TEST_RUN_ID']; "
        "store=RuntimeStateStore(Path(os.environ['TEST_RUN_DIR']),run_id); "
        "current=store.load(); "
        "sys.exit(7) if current.restart_count == 0 else "
        "(store.mark_terminal(RuntimeLifecycle.SUCCESS, reason='resumed') and sys.exit(0))"
    )
    result = watchdog.run(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        env={
            "TEST_RUN_ID": run_id,
            "TEST_RUN_DIR": str(tmp_path / run_id),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [os.environ.get("PYTHONPATH", ""), os.getcwd(), "src"])
            ),
        },
    )

    assert result.exit_code == 0
    assert result.restarts == 1
    assert state.load().lifecycle == RuntimeLifecycle.SUCCESS


def test_runtime_watchdog_writes_auditable_system_failure(tmp_path) -> None:
    settings = Settings(
        max_wall_clock_seconds=60,
        minimum_experiment_window_seconds=0,
        paper_reserve_seconds=0,
        finalization_reserve_seconds=0,
        watchdog_check_interval_seconds=0.05,
        watchdog_stale_seconds=10,
        watchdog_max_restarts=0,
    )
    run_id = "run-watchdog-failure"
    state = RuntimeStateStore(tmp_path / run_id, run_id)
    state.initialize("question", settings)
    result = RuntimeWatchdog(settings, state).run(
        [sys.executable, "-c", "raise SystemExit(9)"],
        cwd=tmp_path,
    )

    assert result.exit_code == 3
    assert (tmp_path / run_id / "manifest.json").exists()
    assert (tmp_path / run_id / "paper" / "audit_report.md").exists()
