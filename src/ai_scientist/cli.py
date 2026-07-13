from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .orchestrator import ResearchOrchestrator
from .runtime import RuntimeStateStore, create_run_id
from .watchdog import RuntimeWatchdog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-scientist")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser(
        "run",
        help="Run the objective-and-question AI Scientist",
    )
    question_group = run.add_mutually_exclusive_group(required=True)
    question_group.add_argument("--question", help="The core research question")
    question_group.add_argument(
        "--random-ai-ml-topic",
        action="store_true",
        help="Ask the Director to select a random executable AI/ML research topic",
    )
    run.add_argument(
        "--objective",
        help=(
            "Research objective; when omitted, the core question is used as the "
            "objective"
        ),
    )
    run.add_argument(
        "--constraint",
        action="append",
        default=[],
        help=(
            "Concrete research or resource constraint supplied to every planning "
            "agent; repeat the flag for multiple constraints"
        ),
    )
    run.add_argument(
        "--research-depth",
        choices=["quick", "competition", "thesis", "publication"],
        help="Required research depth for claim expansion and promotion",
    )
    run.add_argument(
        "--research-profile",
        choices=["general", "trace-audit"],
        help=(
            "Research adapter; trace-audit specializes the pipeline for "
            "claim-result-code provenance and false-acceptance evaluation"
        ),
    )
    run.add_argument(
        "--trace-review-decisions",
        type=Path,
        help=(
            "Frozen blinded reviewer-decision JSON exported by the external/VESSL "
            "TRACE_AUDIT reviewer run"
        ),
    )
    run.add_argument(
        "--prepare-trace-review",
        action="store_true",
        help=(
            "Run through the frozen TRACE_AUDIT benchmark/corruption preparation, "
            "emit the external reviewer job spec, and pause the run for resumption"
        ),
    )
    run.add_argument(
        "--run-id",
        help="Explicit run ID used to resume a prepared non-terminal run",
    )
    run.add_argument(
        "--legacy-single-director",
        action="store_true",
        help="Use the legacy single-Director planning workflow",
    )
    run.add_argument(
        "--provider",
        choices=["codex", "openai"],
        help="Execution provider; codex uses the signed-in Codex CLI without an API key",
    )
    run.add_argument(
        "--model",
        help="Override the model for every agent in this run",
    )
    run.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
        help="Override reasoning effort for every agent in this run",
    )
    run.add_argument(
        "--execute-code",
        action="store_true",
        help="Allow generated Python experiments to run locally",
    )
    run.add_argument(
        "--pipeline-smoke-test",
        action="store_true",
        help=(
            "Allow a transparent CPU surrogate to exercise Experiment, Writer, "
            "Reviewer, and rendering when full research resources are unavailable"
        ),
    )
    run.add_argument("--runs-dir", type=Path, help="Override the artifact directory")
    run.add_argument(
        "--max-wall-seconds",
        type=int,
        help="Total autonomous wall-clock budget; 0 disables the deadline",
    )
    run.add_argument(
        "--experiment-timeout-seconds",
        type=int,
        help="Per-experiment execution limit; 0 disables this limit",
    )
    run.add_argument(
        "--paper-reserve-seconds",
        type=int,
        help="Time protected from earlier stages for Writer and Reviewer",
    )
    run.add_argument(
        "--finalization-reserve-seconds",
        type=int,
        help="Time protected for rendering and the final manifest",
    )
    run.add_argument(
        "--no-watchdog",
        action="store_true",
        help="Run in the foreground without the external Runtime Watchdog",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_argv)
    project_root = Path.cwd()
    load_dotenv(project_root / ".env", override=False)
    settings = Settings.from_env(base_dir=project_root)
    if args.runs_dir:
        settings = replace(settings, runs_dir=args.runs_dir)
    if args.provider:
        settings = replace(settings, provider=args.provider)
    if args.model:
        settings = replace(settings, model=args.model)
    if args.reasoning_effort:
        settings = replace(settings, reasoning_effort=args.reasoning_effort)
    if args.execute_code:
        settings = replace(settings, allow_code_execution=True)
    if args.pipeline_smoke_test:
        settings = replace(settings, pipeline_smoke_test=True)
    if args.research_depth:
        settings = replace(settings, research_depth=args.research_depth)
    if args.research_profile:
        settings = replace(
            settings,
            research_profile=args.research_profile.replace("-", "_"),
        )
    if args.trace_review_decisions:
        settings = replace(
            settings,
            trace_reviewer_decisions_path=args.trace_review_decisions,
        )
    if args.prepare_trace_review:
        settings = replace(settings, trace_prepare_only=True)
    if args.legacy_single_director:
        settings = replace(settings, dual_director_enabled=False)
    if args.max_wall_seconds is not None:
        settings = replace(settings, max_wall_clock_seconds=args.max_wall_seconds)
    if args.experiment_timeout_seconds is not None:
        settings = replace(
            settings,
            experiment_timeout_seconds=args.experiment_timeout_seconds,
        )
    if args.paper_reserve_seconds is not None:
        settings = replace(
            settings,
            paper_reserve_seconds=args.paper_reserve_seconds,
        )
    if args.finalization_reserve_seconds is not None:
        settings = replace(
            settings,
            finalization_reserve_seconds=args.finalization_reserve_seconds,
        )
    settings.validate()
    if settings.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for execution")
    question = args.question or (
        "Autonomously select a random, underexplored AI/ML research problem that can "
        "be tested end-to-end with public or programmatically generated data and a "
        "small local computational budget. Prefer a mechanistic question with at "
        "least three genuinely competing explanations and discriminating predictions."
    )
    objective = (args.objective or question).strip()
    if args.random_ai_ml_topic:
        print("AI/ML random-topic mode: the Director will select the concrete topic.")
    watchdog_child = os.getenv("AISCI_WATCHDOG_CHILD") == "1"
    run_id = args.run_id or os.getenv("AISCI_RUN_ID")
    if settings.watchdog_enabled and not args.no_watchdog and not watchdog_child:
        run_id = run_id or create_run_id()
        state_store = RuntimeStateStore(settings.runs_dir / run_id, run_id)
        state_store.initialize(question, settings, objective=objective)
        command = [sys.executable, "-m", "ai_scientist", *raw_argv]
        watchdog = RuntimeWatchdog(settings, state_store)
        result = watchdog.run(
            command,
            cwd=project_root,
            env={
                "AISCI_WATCHDOG_CHILD": "1",
                "AISCI_RUN_ID": run_id,
            },
        )
        manifest_path = settings.runs_dir / run_id / "manifest.json"
        if manifest_path.exists():
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(json.dumps(manifest_payload, ensure_ascii=False, indent=2))
            if manifest_payload.get("status") == "SUCCESS" or manifest_payload.get(
                "final_stage"
            ) == "TRACE_REVIEW_READY":
                return 0
            return 3 if manifest_payload.get("status") == "SYSTEM_FAILURE" else 2
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "SYSTEM_FAILURE",
                    "reason": result.reason,
                    "watchdog_log": str(result.log_path),
                    "restarts": result.restarts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return result.exit_code

    orchestrator = ResearchOrchestrator(settings, run_id=run_id)
    manifest = asyncio.run(
        orchestrator.run(
            question,
            objective=objective,
            constraints=args.constraint,
        )
    )
    print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return (
        0
        if manifest.status.value == "SUCCESS"
        or manifest.final_stage == "TRACE_REVIEW_READY"
        else 2
    )
