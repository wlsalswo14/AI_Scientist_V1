from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import ArtifactStore
from .config import Settings
from .rendering import render_audit_report
from .runtime import (
    RuntimeLifecycle,
    RuntimePhase,
    RuntimeStateStore,
    read_state_file,
    utc_now,
)
from .schemas import FinalManifest, ResearchDepth, ResearchProfile, RunStatus


@dataclass(frozen=True, slots=True)
class WatchdogResult:
    exit_code: int
    restarts: int
    reason: str
    log_path: Path


class RuntimeWatchdog:
    """Non-LLM process monitor for restart, heartbeat, and wall-clock limits."""

    def __init__(
        self,
        settings: Settings,
        state_store: RuntimeStateStore,
    ) -> None:
        self.settings = settings
        self.state_store = state_store
        self.log_path = state_store.run_dir / "watchdog.log"

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> WatchdogResult:
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        restarts = 0
        failure_restarts = 0
        finalization_restart_used = False
        phase_budget_interruptions: set[RuntimePhase] = set()
        while True:
            self._log(
                f"starting child attempt={restarts + 1} command={command[0]}"
            )
            with self.log_path.open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    list(command),
                    cwd=cwd,
                    env=child_env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                restart_reason: str | None = None
                budget_transition = False
                while True:
                    state = read_state_file(self.state_store.path)
                    if state is not None and state.terminal:
                        exit_code = self._wait_or_terminate(process)
                        reason = state.terminal_reason or state.lifecycle.value
                        self._log(f"terminal state observed: {reason}")
                        return WatchdogResult(
                            exit_code=exit_code,
                            restarts=restarts,
                            reason=reason,
                            log_path=self.log_path,
                        )

                    return_code = process.poll()
                    if return_code is not None:
                        state = read_state_file(self.state_store.path)
                        if state is not None and state.terminal:
                            return WatchdogResult(
                                exit_code=return_code,
                                restarts=restarts,
                                reason=state.terminal_reason or "terminal",
                                log_path=self.log_path,
                            )
                        restart_reason = (
                            f"child exited before terminal state (exit={return_code})"
                        )
                        break

                    if state is not None and state.deadline_at:
                        deadline = datetime.fromisoformat(state.deadline_at)
                        remaining = (deadline - utc_now()).total_seconds()
                        reserve = self._reserve_for_phase(state.phase)
                        if (
                            reserve > 0
                            and remaining <= reserve
                            and state.phase not in phase_budget_interruptions
                        ):
                            self._terminate(process)
                            phase_budget_interruptions.add(state.phase)
                            budget_transition = True
                            restart_reason = (
                                f"phase reserve reached for {state.phase.value}; "
                                f"preserving {reserve}s for downstream work"
                            )
                            break
                        if utc_now() >= deadline:
                            self._terminate(process)
                            if not finalization_restart_used:
                                finalization_restart_used = True
                                self.state_store.request_finalization(
                                    "Wall-clock deadline reached"
                                )
                                restart_reason = "deadline finalization restart"
                                break
                            return self._fail_terminal(
                                restarts,
                                "Finalization could not complete before the deadline",
                            )

                    if state is not None and self._heartbeat_is_stale(state.last_heartbeat):
                        restart_reason = (
                            f"heartbeat stale for more than "
                            f"{self.settings.watchdog_stale_seconds}s"
                        )
                        self._terminate(process)
                        break

                    time.sleep(self.settings.watchdog_check_interval_seconds)

            if restart_reason is None:
                restart_reason = "unknown child termination"
            if budget_transition:
                restarts += 1
                self.state_store.increment_restart(restart_reason)
                self._log(f"restarting child at phase boundary: {restart_reason}")
                continue
            if failure_restarts >= self.settings.watchdog_max_restarts:
                return self._fail_terminal(restarts, restart_reason)
            restarts += 1
            failure_restarts += 1
            self.state_store.increment_restart(restart_reason)
            self._log(f"restarting child: {restart_reason}")

    def _heartbeat_is_stale(self, value: str) -> bool:
        try:
            heartbeat = datetime.fromisoformat(value)
        except ValueError:
            return True
        return (
            utc_now() - heartbeat
        ).total_seconds() > self.settings.watchdog_stale_seconds

    def _reserve_for_phase(self, phase: RuntimePhase) -> int:
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

    def _fail_terminal(self, restarts: int, reason: str) -> WatchdogResult:
        self._log(f"watchdog exhausted: {reason}")
        state = self.state_store.load()
        if state is not None and not state.terminal:
            state = self.state_store.mark_terminal(
                RuntimeLifecycle.FAILED,
                reason=f"Runtime Watchdog: {reason}",
            )
        failure_path = self.state_store.run_dir / "watchdog_failure.json"
        failure_path.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "restarts": restarts,
                    "time": utc_now().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if state is not None:
            artifact_store = ArtifactStore(
                self.state_store.run_dir.parent,
                self.state_store.run_id,
            )
            audit_report = render_audit_report(
                artifact_store.paper_dir,
                question=state.question,
                final_stage="SYSTEM_FAILURE",
                status=RunStatus.SYSTEM_FAILURE.value,
                details=[f"Runtime Watchdog: {reason}"],
            )
            manifest = FinalManifest(
                run_id=state.run_id,
                question=state.question,
                research_objective=state.research_objective,
                research_depth=ResearchDepth(
                    (state.research_depth or self.settings.research_depth).upper()
                ),
                research_profile=ResearchProfile(
                    (state.research_profile or self.settings.research_profile).upper()
                ),
                status=RunStatus.SYSTEM_FAILURE,
                model=self.settings.model,
                reasoning_effort=self.settings.reasoning_effort,
                pipeline_smoke_test=self.settings.pipeline_smoke_test,
                final_stage="SYSTEM_FAILURE",
                audit_report=str(audit_report),
                artifact_ids=artifact_store.artifact_ids,
                valid_artifact_ids=artifact_store.valid_artifact_ids,
                stale_artifact_ids=[
                    artifact_id
                    for artifact_id in artifact_store.artifact_ids
                    if artifact_store.status_of(artifact_id) == "STALE"
                ],
                unresolved_issues=[f"Runtime Watchdog: {reason}"],
            )
            artifact_store.write_manifest(manifest)
        return WatchdogResult(
            exit_code=3,
            restarts=restarts,
            reason=reason,
            log_path=self.log_path,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _wait_or_terminate(self, process: subprocess.Popen[str]) -> int:
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            return process.returncode if process.returncode is not None else 3

    def _log(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
