from __future__ import annotations

import json
import os
import ctypes
import threading
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_run_id() -> str:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{uuid4().hex[:8]}"


def _process_is_alive(process_id: int) -> bool:
    """Check liveness without using signal 0, which terminates on Windows."""

    if process_id <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return False
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RuntimePhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    RESEARCH_MODE = "RESEARCH_MODE"
    RESEARCH_PLANNING = "RESEARCH_PLANNING"
    EXPERIMENT = "EXPERIMENT"
    PAPER = "PAPER"
    FINALIZATION = "FINALIZATION"
    TERMINAL = "TERMINAL"


class RuntimeLifecycle(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


TERMINAL_LIFECYCLES = {
    RuntimeLifecycle.SUCCESS,
    RuntimeLifecycle.PARTIAL,
    RuntimeLifecycle.FAILED,
}


class RunState(BaseModel):
    """Small current-state projection; scientific artifacts remain separate."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    question: str
    research_objective: str = ""
    research_depth: str = ""
    research_profile: str = ""
    lifecycle: RuntimeLifecycle = RuntimeLifecycle.PENDING
    phase: RuntimePhase = RuntimePhase.INITIALIZING
    next_action: str = "initialize"
    started_at: str
    updated_at: str
    last_heartbeat: str
    deadline_at: str | None = None
    process_id: int | None = None
    restart_count: int = 0
    force_finalize: bool = False
    stage_status: dict[str, str] = Field(default_factory=dict)
    progress: dict[str, dict[str, float]] = Field(default_factory=dict)
    pending_feedback: dict[str, list[str]] = Field(default_factory=dict)
    active_model_sessions: list[str] = Field(default_factory=list)
    last_model_event: str | None = None
    last_error: str | None = None
    terminal_reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.lifecycle in TERMINAL_LIFECYCLES


class RuntimeStateStore:
    """Atomic storage for the single run-state projection."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.path = run_dir / "run_state.json"
        self._lock = threading.RLock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def initialize(
        self,
        question: str,
        settings: Settings,
        *,
        objective: str = "",
    ) -> RunState:
        existing = self.load()
        if existing is not None:
            if existing.run_id != self.run_id:
                raise ValueError("run_state.json belongs to a different run")
            if existing.question != question:
                raise ValueError(
                    "A resumed run must use the original research question"
                )
            if (
                objective
                and existing.research_objective
                and existing.research_objective != objective
            ):
                raise ValueError(
                    "A resumed run must use the original research objective"
                )
            if objective and not existing.research_objective:
                existing = existing.model_copy(
                    update={"research_objective": objective}
                )
                self.save(existing)
            if (
                existing.research_depth
                and existing.research_depth != settings.research_depth
            ):
                raise ValueError(
                    "A resumed run must use the original research depth"
                )
            if not existing.research_depth:
                existing = existing.model_copy(
                    update={"research_depth": settings.research_depth}
                )
                self.save(existing)
            if (
                existing.research_profile
                and existing.research_profile != settings.research_profile
            ):
                raise ValueError(
                    "A resumed run must use the original research profile"
                )
            if not existing.research_profile:
                existing = existing.model_copy(
                    update={"research_profile": settings.research_profile}
                )
                self.save(existing)
            prior_process = existing.process_id
            if (
                not existing.terminal
                and prior_process is not None
                and prior_process != os.getpid()
            ):
                watchdog_child = os.getenv("AISCI_WATCHDOG_CHILD") == "1"
                if not watchdog_child and _process_is_alive(prior_process):
                    raise RuntimeError(
                        f"Run {self.run_id} is already active in process "
                        f"{prior_process}"
                    )
                now = utc_now().isoformat()
                existing = existing.model_copy(
                    update={
                        "restart_count": (
                            existing.restart_count
                            if watchdog_child
                            else existing.restart_count + 1
                        ),
                        "last_error": (
                            existing.last_error
                            if watchdog_child
                            else f"Recovered from inactive process {prior_process}"
                        ),
                        "active_model_sessions": [],
                        "last_model_event": "runtime:resumed",
                        "updated_at": now,
                        "last_heartbeat": now,
                        "process_id": os.getpid(),
                    }
                )
                self.save(existing)
            return existing
        now = utc_now()
        deadline = (
            now + timedelta(seconds=settings.max_wall_clock_seconds)
            if settings.max_wall_clock_seconds
            else None
        )
        state = RunState(
            run_id=self.run_id,
            question=question,
            research_objective=objective,
            research_depth=settings.research_depth,
            research_profile=settings.research_profile,
            started_at=now.isoformat(),
            updated_at=now.isoformat(),
            last_heartbeat=now.isoformat(),
            deadline_at=deadline.isoformat() if deadline else None,
            process_id=os.getpid(),
        )
        self.save(state)
        return state

    def load(self) -> RunState | None:
        if not self.path.exists():
            return None
        return RunState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: RunState) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def heartbeat(
        self,
        *,
        action: str | None = None,
        progress: dict[str, float] | None = None,
        stage: str | None = None,
    ) -> RunState | None:
        state = self.load()
        if state is None or state.terminal:
            return state
        now = utc_now().isoformat()
        update: dict[str, Any] = {
            "updated_at": now,
            "last_heartbeat": now,
            "process_id": os.getpid(),
        }
        if action is not None:
            update["next_action"] = action
        if progress is not None and stage is not None:
            all_progress = dict(state.progress)
            all_progress[stage] = progress
            update["progress"] = all_progress
        state = state.model_copy(update=update)
        self.save(state)
        return state

    def model_heartbeat(self, session_label: str, status: str) -> RunState | None:
        """Atomically project concurrent model activity without hiding siblings."""

        with self._lock:
            state = self.load()
            if state is None or state.terminal:
                return state
            active = set(state.active_model_sessions)
            if status == "started":
                active.add(session_label)
            else:
                active.discard(session_label)
            ordered = sorted(active)
            event = f"model:{session_label}:{status}"
            action = (
                "models:active:" + ",".join(ordered)
                if ordered
                else event
            )
            now = utc_now().isoformat()
            state = state.model_copy(
                update={
                    "active_model_sessions": ordered,
                    "last_model_event": event,
                    "next_action": action,
                    "updated_at": now,
                    "last_heartbeat": now,
                    "process_id": os.getpid(),
                }
            )
            self.save(state)
            return state

    def enter_phase(self, phase: RuntimePhase, action: str) -> RunState:
        state = self._required()
        now = utc_now().isoformat()
        stage_status = dict(state.stage_status)
        if (
            state.phase != phase
            and stage_status.get(state.phase.value) == "RUNNING"
        ):
            stage_status[state.phase.value] = "INTERRUPTED"
        stage_status[phase.value] = "RUNNING"
        lifecycle = (
            RuntimeLifecycle.FINALIZING
            if phase == RuntimePhase.FINALIZATION
            else RuntimeLifecycle.RUNNING
        )
        state = state.model_copy(
            update={
                "phase": phase,
                "lifecycle": lifecycle,
                "next_action": action,
                "updated_at": now,
                "last_heartbeat": now,
                "process_id": os.getpid(),
                "stage_status": stage_status,
            }
        )
        self.save(state)
        return state

    def complete_phase(self, phase: RuntimePhase) -> RunState:
        state = self._required()
        stage_status = dict(state.stage_status)
        stage_status[phase.value] = "COMPLETE"
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "stage_status": stage_status,
                "updated_at": now,
                "last_heartbeat": now,
            }
        )
        self.save(state)
        return state

    def request_finalization(self, reason: str) -> RunState:
        state = self._required()
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "force_finalize": True,
                "next_action": "finalize_valid_artifacts",
                "updated_at": now,
                "last_heartbeat": now,
                "terminal_reason": reason,
            }
        )
        self.save(state)
        return state

    def record_error(self, message: str) -> RunState:
        state = self._required()
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "last_error": message,
                "updated_at": now,
                "last_heartbeat": now,
            }
        )
        self.save(state)
        return state

    def add_feedback(self, channel: str, message: str) -> RunState:
        state = self._required()
        pending = {key: list(value) for key, value in state.pending_feedback.items()}
        values = pending.setdefault(channel, [])
        if message not in values:
            values.append(message)
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "pending_feedback": pending,
                "updated_at": now,
                "last_heartbeat": now,
            }
        )
        self.save(state)
        return state

    def clear_feedback(self, channel: str) -> RunState:
        state = self._required()
        pending = {key: list(value) for key, value in state.pending_feedback.items()}
        pending.pop(channel, None)
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "pending_feedback": pending,
                "updated_at": now,
                "last_heartbeat": now,
            }
        )
        self.save(state)
        return state

    def increment_restart(self, reason: str) -> RunState:
        state = self._required()
        now = utc_now().isoformat()
        state = state.model_copy(
            update={
                "restart_count": state.restart_count + 1,
                "last_error": reason,
                "updated_at": now,
                "last_heartbeat": now,
                "process_id": None,
            }
        )
        self.save(state)
        return state

    def mark_terminal(
        self,
        lifecycle: RuntimeLifecycle,
        *,
        reason: str,
    ) -> RunState:
        if lifecycle not in TERMINAL_LIFECYCLES:
            raise ValueError("mark_terminal requires a terminal lifecycle")
        state = self._required()
        now = utc_now().isoformat()
        stage_status = dict(state.stage_status)
        stage_status[RuntimePhase.FINALIZATION.value] = "COMPLETE"
        state = state.model_copy(
            update={
                "lifecycle": lifecycle,
                "phase": RuntimePhase.TERMINAL,
                "next_action": "none",
                "updated_at": now,
                "last_heartbeat": now,
                "process_id": None,
                "stage_status": stage_status,
                "terminal_reason": reason,
            }
        )
        self.save(state)
        return state

    def _required(self) -> RunState:
        state = self.load()
        if state is None:
            raise RuntimeError("Runtime state has not been initialized")
        return state


class DeadlineDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    remaining_seconds: float | None
    required_reserve_seconds: int
    reason: str


class DeadlinePolicy:
    """Deterministic stage admission using one persisted absolute deadline."""

    def __init__(self, state_store: RuntimeStateStore, settings: Settings) -> None:
        self.state_store = state_store
        self.settings = settings

    def remaining_seconds(self) -> float | None:
        state = self.state_store.load()
        if state is None or state.deadline_at is None:
            return None
        deadline = datetime.fromisoformat(state.deadline_at)
        return (deadline - utc_now()).total_seconds()

    def can_start(self, phase: RuntimePhase) -> DeadlineDecision:
        state = self.state_store.load()
        remaining = self.remaining_seconds()
        if phase in {RuntimePhase.FINALIZATION, RuntimePhase.TERMINAL}:
            return DeadlineDecision(
                allowed=True,
                remaining_seconds=remaining,
                required_reserve_seconds=0,
                reason="Finalization is always allowed",
            )
        if state is not None and state.force_finalize:
            return DeadlineDecision(
                allowed=False,
                remaining_seconds=remaining,
                required_reserve_seconds=0,
                reason="Runtime Watchdog requested finalization",
            )
        reserve = self._reserve_for(phase)
        allowed = remaining is None or remaining > reserve
        return DeadlineDecision(
            allowed=allowed,
            remaining_seconds=remaining,
            required_reserve_seconds=reserve,
            reason=(
                "Enough wall-clock budget remains"
                if allowed
                else (
                    f"Remaining wall-clock budget must be preserved for downstream "
                    f"work ({reserve}s required)"
                )
            ),
        )

    def _reserve_for(self, phase: RuntimePhase) -> int:
        if phase in {
            RuntimePhase.RESEARCH_MODE,
            RuntimePhase.RESEARCH_PLANNING,
        }:
            return (
                self.settings.minimum_experiment_window_seconds
                + self.settings.paper_reserve_seconds
                + self.settings.finalization_reserve_seconds
            )
        if phase == RuntimePhase.EXPERIMENT:
            return (
                self.settings.paper_reserve_seconds
                + self.settings.finalization_reserve_seconds
            )
        if phase == RuntimePhase.PAPER:
            return self.settings.finalization_reserve_seconds
        return 0


def read_state_file(path: Path) -> RunState | None:
    """Best-effort state reader for the external Watchdog."""

    try:
        return RunState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
