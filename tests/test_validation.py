from __future__ import annotations

import hashlib
from itertools import islice
from pathlib import Path

import pytest

from ai_scientist.config import (
    Settings,
    hypothesis_rounds,
    repair_attempts,
    repair_budget_exhausted,
)
from ai_scientist.cli import build_parser
from ai_scientist.execution import LocalExperimentRunner, UnsafeExperimentError
from ai_scientist.schemas import ExperimentorOutput, GeneratedFile
from ai_scientist.validation import validate_experimentor


def test_default_model_profile_is_final_competition_profile() -> None:
    settings = Settings()
    assert settings.model == "gpt-5.6-sol"
    assert settings.reasoning_effort == "max"


def test_component_repair_budget_is_unlimited_by_default() -> None:
    settings = Settings()

    assert settings.max_component_repair_attempts is None
    assert list(islice(repair_attempts(None), 5)) == [0, 1, 2, 3, 4]
    assert not repair_budget_exhausted(10_000, None)


def test_component_repair_budget_still_accepts_finite_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISCI_MAX_COMPONENT_REPAIR_ATTEMPTS", "2")

    settings = Settings.from_env()

    assert settings.max_component_repair_attempts == 2
    assert list(repair_attempts(2)) == [0, 1, 2]
    assert repair_budget_exhausted(2, 2)


def test_component_repair_budget_accepts_unlimited_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISCI_MAX_COMPONENT_REPAIR_ATTEMPTS", "unlimited")

    assert Settings.from_env().max_component_repair_attempts is None


def test_hypothesis_rounds_are_unlimited_by_default() -> None:
    settings = Settings()

    assert settings.max_hypothesis_rounds is None
    assert list(islice(hypothesis_rounds(None), 5)) == [1, 2, 3, 4, 5]


def test_hypothesis_rounds_still_accept_finite_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISCI_MAX_HYPOTHESIS_ROUNDS", "3")

    settings = Settings.from_env()

    assert settings.max_hypothesis_rounds == 3
    assert list(hypothesis_rounds(3)) == [1, 2, 3]


def test_model_and_effort_can_be_overridden_per_run() -> None:
    Settings(model="gpt-5.6-terra", reasoning_effort="medium").validate()


def test_rejects_unknown_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="Unsupported reasoning effort"):
        Settings(reasoning_effort="extreme").validate()


def test_cli_accepts_objective_question_and_research_depth() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--objective",
            "Explain a bounded phenomenon",
            "--question",
            "Does B improve Y over A?",
            "--research-depth",
            "thesis",
        ]
    )

    assert args.objective == "Explain a bounded phenomenon"
    assert args.question == "Does B improve Y over A?"
    assert args.research_depth == "thesis"


def test_publication_depth_requires_three_promoted_claim_slots() -> None:
    with pytest.raises(ValueError, match="publication depth requires"):
        Settings(
            research_depth="publication",
            target_promoted_hypotheses=2,
        ).validate()


def test_runner_blocks_system_imports(tmp_path: Path) -> None:
    value = ExperimentorOutput(
        hypothesis_id="H1",
        experiment_id="EXP-H1",
        files=[GeneratedFile(path="experiment.py", content="import os\n")],
        entrypoint="experiment.py",
        protocol_notes=[],
    )
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)
    with pytest.raises(UnsafeExperimentError, match="Blocked import"):
        runner.run(value, round_number=1)


def test_runner_rejects_workspace_escape(tmp_path: Path) -> None:
    value = ExperimentorOutput(
        hypothesis_id="H1",
        experiment_id="EXP-H1",
        files=[GeneratedFile(path="../escape.py", content="print('x')\n")],
        entrypoint="../escape.py",
        protocol_notes=[],
    )
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)
    with pytest.raises(UnsafeExperimentError, match="escapes"):
        runner.run(value, round_number=1)


def test_runner_assigns_canonical_result_id(tmp_path: Path) -> None:
    value = ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[
            GeneratedFile(
                path="experiment.py",
                content=(
                    "from pathlib import Path\n"
                    "Path('result.json').write_text('{\"loss\": 1.0}', "
                    "encoding='utf-8')\n"
                ),
            )
        ],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)

    result = runner.run(value, round_number=1)

    assert result.exit_code == 0
    assert result.result_ids == ["EXP-T1:result.json"]


def test_runner_injects_read_only_data_without_reporting_it_as_output(
    tmp_path: Path,
) -> None:
    value = ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[
            GeneratedFile(
                path="experiment.py",
                content=(
                    "import json\n"
                    "from pathlib import Path\n"
                    "rows = json.loads(Path('reviewer-decisions.json').read_text(encoding='utf-8'))\n"
                    "Path('result.json').write_text(json.dumps({'count': len(rows['decisions'])}), encoding='utf-8')\n"
                ),
            )
        ],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)

    result = runner.run(
        value,
        round_number=1,
        input_files={"reviewer-decisions.json": '{"decisions": [1, 2]}'},
    )

    assert result.exit_code == 0
    assert "reviewer-decisions.json" not in result.output_files
    assert result.output_files["result.json"] == '{"count": 2}'


def test_runner_preserves_injected_input_bytes_across_platforms(
    tmp_path: Path,
) -> None:
    content = '{\n  "decisions": [1, 2]\n}\n'
    expected_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    value = ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[
            GeneratedFile(
                path="experiment.py",
                content=(
                    "import hashlib\n"
                    "from pathlib import Path\n"
                    "blob = Path('reviewer-decisions.json').read_bytes()\n"
                    "Path('result.json').write_text("
                    "hashlib.sha256(blob).hexdigest(), encoding='utf-8')\n"
                ),
            )
        ],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)

    result = runner.run(
        value,
        round_number=1,
        input_files={"reviewer-decisions.json": content},
    )

    assert result.exit_code == 0
    assert result.output_files["result.json"] == expected_sha256
    assert (
        tmp_path / "T1" / "round-1" / "reviewer-decisions.json"
    ).read_bytes() == content.encode("utf-8")


def test_runner_clears_stale_files_when_reusing_a_round(tmp_path: Path) -> None:
    runner = LocalExperimentRunner(tmp_path, timeout_seconds=5, enabled=True)
    workspace = tmp_path / "T1" / "round-1"
    workspace.mkdir(parents=True)
    (workspace / "stale.json").write_text('{"old": true}', encoding="utf-8")
    value = ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[
            GeneratedFile(
                path="experiment.py",
                content=(
                    "from pathlib import Path\n"
                    "Path('result.json').write_text('{\"loss\": 1.0}', "
                    "encoding='utf-8')\n"
                ),
            )
        ],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )

    result = runner.run(value, round_number=1)

    assert result.exit_code == 0
    assert "stale.json" not in result.output_files
    assert not (workspace / "stale.json").exists()


def test_experimentor_validation_rejects_invalid_python_syntax() -> None:
    value = ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[
            GeneratedFile(
                path="experiment.py",
                content="result = [1, 2)\n",
            )
        ],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )

    report = validate_experimentor(value, selected_target_ids={"T1"})

    assert not report.valid
    assert any(issue.code == "INVALID_PYTHON_SYNTAX" for issue in report.issues)
