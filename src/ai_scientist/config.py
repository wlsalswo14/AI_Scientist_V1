from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_repair_attempts(name: str) -> int | None:
    value = os.getenv(name, "unlimited").strip().lower()
    if value in {"unlimited", "infinite", "inf", "none"}:
        return None
    return int(value)


def _env_hypothesis_rounds(name: str) -> int | None:
    value = os.getenv(name, "unlimited").strip().lower()
    if value in {"unlimited", "infinite", "inf", "none"}:
        return None
    return int(value)


def repair_attempts(max_attempts: int | None) -> Iterator[int]:
    """Yield the initial attempt plus every allowed repair attempt."""

    attempt = 0
    while max_attempts is None or attempt <= max_attempts:
        yield attempt
        attempt += 1


def repair_budget_exhausted(attempt: int, max_attempts: int | None) -> bool:
    """Return whether a failed attempt has consumed a finite repair budget."""

    return max_attempts is not None and attempt >= max_attempts


def hypothesis_rounds(
    max_rounds: int | None, *, start: int = 1
) -> Iterator[int]:
    """Yield research-planning rounds until a finite limit or deadline stops them."""

    round_number = start
    while max_rounds is None or round_number <= max_rounds:
        yield round_number
        round_number += 1


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration. Agent prompts and research artifacts live elsewhere."""

    provider: str = "codex"
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "max"
    max_output_tokens: int = 12_000
    store_responses: bool = False
    runs_dir: Path = Path("runs")
    max_hypothesis_rounds: int | None = None
    max_experiment_rounds: int = 2
    max_review_rounds: int = 2
    max_global_backtracks: int = 2
    allow_code_execution: bool = False
    pipeline_smoke_test: bool = False
    dual_director_enabled: bool = True
    research_depth: str = "thesis"
    research_profile: str = "general"
    trace_reviewer_decisions_path: Path | None = None
    trace_prepare_only: bool = False
    experiment_timeout_seconds: int = 300
    model_timeout_seconds: int = 900
    model_max_retries: int = 3
    max_component_repair_attempts: int | None = None
    min_hypotheses: int = 3
    max_hypotheses: int = 5
    target_promoted_hypotheses: int = 3
    rubric_version: str = "1.0"
    minimum_gate_score: int = 3
    score_drop_absolute: float = 0.75
    score_drop_relative: float = 0.20
    critical_dimension_drop: float = 1.0
    max_stagnant_rounds: int = 2
    max_wall_clock_seconds: int = 14_400
    minimum_experiment_window_seconds: int = 900
    paper_reserve_seconds: int = 1_800
    finalization_reserve_seconds: int = 300
    watchdog_enabled: bool = True
    watchdog_check_interval_seconds: float = 5.0
    watchdog_stale_seconds: int = 3_600
    watchdog_max_restarts: int = 3

    @classmethod
    def from_env(cls, *, base_dir: Path | None = None) -> "Settings":
        runs = Path(os.getenv("AISCI_RUNS_DIR", "runs"))
        if base_dir is not None and not runs.is_absolute():
            runs = base_dir / runs
        trace_decisions_raw = os.getenv("AISCI_TRACE_REVIEW_DECISIONS")
        trace_decisions = Path(trace_decisions_raw) if trace_decisions_raw else None
        if (
            trace_decisions is not None
            and base_dir is not None
            and not trace_decisions.is_absolute()
        ):
            trace_decisions = base_dir / trace_decisions
        return cls(
            provider=os.getenv("AISCI_PROVIDER", "codex"),
            model=os.getenv("AISCI_MODEL", "gpt-5.6-sol"),
            reasoning_effort=os.getenv("AISCI_REASONING_EFFORT", "max"),
            max_output_tokens=int(os.getenv("AISCI_MAX_OUTPUT_TOKENS", "12000")),
            store_responses=_env_bool("AISCI_STORE_RESPONSES", False),
            runs_dir=runs,
            pipeline_smoke_test=_env_bool("AISCI_PIPELINE_SMOKE_TEST", False),
            dual_director_enabled=_env_bool("AISCI_DUAL_DIRECTOR_ENABLED", True),
            research_depth=os.getenv("AISCI_RESEARCH_DEPTH", "thesis").lower(),
            research_profile=os.getenv(
                "AISCI_RESEARCH_PROFILE", "general"
            ).lower().replace("-", "_"),
            trace_reviewer_decisions_path=trace_decisions,
            trace_prepare_only=_env_bool("AISCI_TRACE_PREPARE_ONLY", False),
            max_hypothesis_rounds=_env_hypothesis_rounds(
                "AISCI_MAX_HYPOTHESIS_ROUNDS"
            ),
            max_experiment_rounds=int(os.getenv("AISCI_MAX_EXPERIMENT_ROUNDS", "2")),
            max_review_rounds=int(os.getenv("AISCI_MAX_REVIEW_ROUNDS", "2")),
            max_global_backtracks=int(os.getenv("AISCI_MAX_GLOBAL_BACKTRACKS", "2")),
            allow_code_execution=_env_bool("AISCI_ALLOW_CODE_EXECUTION", False),
            experiment_timeout_seconds=int(
                os.getenv("AISCI_EXPERIMENT_TIMEOUT_SECONDS", "300")
            ),
            model_timeout_seconds=int(os.getenv("AISCI_MODEL_TIMEOUT_SECONDS", "900")),
            model_max_retries=int(os.getenv("AISCI_MODEL_MAX_RETRIES", "3")),
            max_component_repair_attempts=_env_repair_attempts(
                "AISCI_MAX_COMPONENT_REPAIR_ATTEMPTS"
            ),
            target_promoted_hypotheses=int(
                os.getenv("AISCI_TARGET_PROMOTED_CLAIMS", "3")
            ),
            minimum_gate_score=int(os.getenv("AISCI_MINIMUM_GATE_SCORE", "3")),
            max_stagnant_rounds=int(
                os.getenv("AISCI_MAX_STAGNANT_ROUNDS", "2")
            ),
            max_wall_clock_seconds=int(
                os.getenv("AISCI_MAX_WALL_CLOCK_SECONDS", "14400")
            ),
            minimum_experiment_window_seconds=int(
                os.getenv("AISCI_MINIMUM_EXPERIMENT_WINDOW_SECONDS", "900")
            ),
            paper_reserve_seconds=int(
                os.getenv("AISCI_PAPER_RESERVE_SECONDS", "1800")
            ),
            finalization_reserve_seconds=int(
                os.getenv("AISCI_FINALIZATION_RESERVE_SECONDS", "300")
            ),
            watchdog_enabled=_env_bool("AISCI_WATCHDOG_ENABLED", True),
            watchdog_check_interval_seconds=float(
                os.getenv("AISCI_WATCHDOG_CHECK_INTERVAL_SECONDS", "5")
            ),
            watchdog_stale_seconds=int(
                os.getenv("AISCI_WATCHDOG_STALE_SECONDS", "3600")
            ),
            watchdog_max_restarts=int(
                os.getenv("AISCI_WATCHDOG_MAX_RESTARTS", "3")
            ),
        )

    def validate(self) -> None:
        if self.provider not in {"codex", "openai"}:
            raise ValueError("provider must be 'codex' or 'openai'")
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.model_max_retries < 1:
            raise ValueError("model_max_retries must be at least 1")
        if self.max_hypothesis_rounds is not None and self.max_hypothesis_rounds < 1:
            raise ValueError("max_hypothesis_rounds must be at least 1")
        if (
            self.max_component_repair_attempts is not None
            and self.max_component_repair_attempts < 0
        ):
            raise ValueError("max_component_repair_attempts cannot be negative")
        if self.target_promoted_hypotheses < 1:
            raise ValueError("target_promoted_hypotheses must be at least 1")
        if self.research_depth == "publication" and self.target_promoted_hypotheses < 3:
            raise ValueError(
                "publication depth requires at least 3 promoted claims"
            )
        if self.research_depth not in {
            "quick",
            "competition",
            "thesis",
            "publication",
        }:
            raise ValueError(
                "research_depth must be quick, competition, thesis, or publication"
            )
        if self.research_profile not in {"general", "trace_audit"}:
            raise ValueError(
                "research_profile must be general or trace_audit"
            )
        if self.research_profile == "trace_audit" and not self.dual_director_enabled:
            raise ValueError("trace_audit requires the Dual-Director workflow")
        if self.trace_prepare_only and self.research_profile != "trace_audit":
            raise ValueError("trace_prepare_only requires the trace_audit profile")
        if (
            self.research_profile == "trace_audit"
            and self.allow_code_execution
            and not self.pipeline_smoke_test
            and not self.trace_prepare_only
            and self.trace_reviewer_decisions_path is None
        ):
            raise ValueError(
                "A substantive trace_audit run requires a frozen reviewer-decision "
                "batch via AISCI_TRACE_REVIEW_DECISIONS or --trace-review-decisions"
            )
        if (
            self.trace_reviewer_decisions_path is not None
            and not self.trace_reviewer_decisions_path.is_file()
        ):
            raise ValueError("trace_reviewer_decisions_path must be an existing file")
        if self.max_stagnant_rounds < 1:
            raise ValueError("max_stagnant_rounds must be at least 1")
        if not 0 <= self.minimum_gate_score <= 5:
            raise ValueError("minimum_gate_score must be between 0 and 5")
        allowed_efforts = {"none", "low", "medium", "high", "xhigh", "max"}
        if self.reasoning_effort not in allowed_efforts:
            raise ValueError(
                f"Unsupported reasoning effort: {self.reasoning_effort}. "
                f"Choose one of {sorted(allowed_efforts)}"
            )
        if self.min_hypotheses < 2 or self.max_hypotheses < self.min_hypotheses:
            raise ValueError("Invalid hypothesis candidate bounds")
        if self.max_wall_clock_seconds < 0:
            raise ValueError("max_wall_clock_seconds cannot be negative")
        if self.experiment_timeout_seconds < 0:
            raise ValueError("experiment_timeout_seconds cannot be negative")
        for name, value in {
            "minimum_experiment_window_seconds": (
                self.minimum_experiment_window_seconds
            ),
            "paper_reserve_seconds": self.paper_reserve_seconds,
            "finalization_reserve_seconds": self.finalization_reserve_seconds,
            "watchdog_stale_seconds": self.watchdog_stale_seconds,
            "watchdog_max_restarts": self.watchdog_max_restarts,
        }.items():
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.watchdog_check_interval_seconds <= 0:
            raise ValueError("watchdog_check_interval_seconds must be positive")
        if self.watchdog_enabled and self.watchdog_stale_seconds <= max(
            self.model_timeout_seconds * self.model_max_retries + 30,
            self.experiment_timeout_seconds,
        ):
            raise ValueError(
                "watchdog_stale_seconds must exceed the complete model retry "
                "window and experiment timeout"
            )
        reserved = (
            self.minimum_experiment_window_seconds
            + self.paper_reserve_seconds
            + self.finalization_reserve_seconds
        )
        if self.max_wall_clock_seconds and reserved >= self.max_wall_clock_seconds:
            raise ValueError(
                "The experiment, paper, and finalization reserves must fit inside "
                "max_wall_clock_seconds"
            )
