from __future__ import annotations

from ..agents import TraceBenchmarkCuratorAgent, TraceFaultInjectorAgent
from ..artifacts import ArtifactStore
from ..config import Settings, repair_attempts, repair_budget_exhausted
from ..schemas import (
    ResearchContract,
    ResearchProfile,
    TraceBenchmarkPlan,
    TraceCorruptionPlan,
    TracePreparationStageResult,
    TraceReviewerDecisionBatch,
)
from ..trace_audit import (
    benchmark_plan_issues,
    corruption_plan_issues,
    trace_contract_fingerprint,
)


class TraceAuditPreparationWorkflow:
    """Curate the paired benchmark and fault recipes before any reviewer run."""

    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        curator: TraceBenchmarkCuratorAgent,
        injector: TraceFaultInjectorAgent,
    ) -> None:
        self.settings = settings
        self.store = store
        self.curator = curator
        self.injector = injector

    async def run(
        self,
        contract: ResearchContract,
    ) -> TracePreparationStageResult:
        if contract.research_profile != ResearchProfile.TRACE_AUDIT:
            raise RuntimeError("Trace preparation requires TRACE_AUDIT profile")
        if contract.trace_study_contract is None:
            raise RuntimeError("Trace preparation requires a frozen study contract")
        trace_contract = contract.trace_study_contract
        fingerprint = trace_contract_fingerprint(trace_contract)
        checkpoint = self.store.load_checkpoint(
            "trace-preparation",
            TracePreparationStageResult,
        )
        if checkpoint is not None and (
            checkpoint.trace_contract_fingerprint == fingerprint
            and not benchmark_plan_issues(
                checkpoint.benchmark_plan,
                trace_contract,
                pipeline_smoke_test=self.settings.pipeline_smoke_test,
            )
            and not corruption_plan_issues(
                checkpoint.corruption_plan,
                trace_contract,
            )
        ):
            return checkpoint

        base_payload = {
            "research_contract": contract.model_dump(mode="json"),
            "trace_study_contract": trace_contract.model_dump(mode="json"),
            "trace_contract_fingerprint": fingerprint,
            "pipeline_smoke_test": self.settings.pipeline_smoke_test,
        }
        benchmark, benchmark_id = await self._curate(
            base_payload,
            trace_contract,
        )
        corruption, corruption_id = await self._inject(
            {
                **base_payload,
                "benchmark_plan": benchmark.model_dump(mode="json"),
            },
            trace_contract,
            dependencies=[benchmark_id],
        )
        job_spec_id = self.store.save(
            "trace-review-job-spec",
            {
                "trace_contract_fingerprint": fingerprint,
                "minimum_reviewer_families": (
                    trace_contract.minimum_reviewer_families
                ),
                "minimum_unique_cases": trace_contract.benchmark_min_cases,
                "conditions": [
                    item.condition_id.value for item in trace_contract.conditions
                ],
                "corruption_manifest_hidden": True,
                "output_schema": TraceReviewerDecisionBatch.model_json_schema(),
                "output_filename": "reviewer-decisions.json",
            },
            dependencies=[benchmark_id, corruption_id],
            metadata={"external_runner": "VESSL_OR_EQUIVALENT"},
        )
        result = TracePreparationStageResult(
            trace_contract_fingerprint=fingerprint,
            benchmark_plan=benchmark,
            corruption_plan=corruption,
        )
        self.store.checkpoint("trace-preparation", result)
        self.store.event(
            "trace_preparation.ready",
            {
                "benchmark_artifact_id": benchmark_id,
                "corruption_artifact_id": corruption_id,
                "review_job_spec_artifact_id": job_spec_id,
                "planned_case_count": benchmark.planned_case_count,
                "recipe_count": len(corruption.recipes),
            },
        )
        return result

    async def _curate(
        self,
        payload: dict,
        trace_contract,
    ) -> tuple[TraceBenchmarkPlan, str]:
        previous_id: str | None = None
        output = await self.curator.run(
            payload,
            session_label="trace-benchmark-curator",
        )
        for attempt in repair_attempts(
            self.settings.max_component_repair_attempts
        ):
            artifact_id = self.store.save(
                "trace-benchmark-plan",
                output,
                dependencies=(
                    self.store.find_artifact_ids(kind="trace-study-contract")[-1:]
                    + ([previous_id] if previous_id else [])
                ),
                metadata={"repair_attempt": attempt, "frozen": True},
            )
            issues = benchmark_plan_issues(
                output,
                trace_contract,
                pipeline_smoke_test=self.settings.pipeline_smoke_test,
            )
            self.store.save(
                "trace-benchmark-plan-validation",
                {"issues": issues},
                dependencies=[artifact_id],
                metadata={"repair_attempt": attempt},
            )
            if not issues:
                return output, artifact_id
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                break
            previous_id = artifact_id
            output = await self.curator.run(
                {
                    **payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": issues,
                        "instruction": "Repair only the benchmark plan; the trace contract is frozen.",
                    },
                },
                session_label=f"trace-benchmark-curator-repair-{attempt + 1}",
            )
        raise RuntimeError("Trace Benchmark Curator failed its repair budget")

    async def _inject(
        self,
        payload: dict,
        trace_contract,
        *,
        dependencies: list[str],
    ) -> tuple[TraceCorruptionPlan, str]:
        previous_id: str | None = None
        output = await self.injector.run(
            payload,
            session_label="trace-fault-injector",
        )
        for attempt in repair_attempts(
            self.settings.max_component_repair_attempts
        ):
            artifact_id = self.store.save(
                "trace-corruption-plan",
                output,
                dependencies=dependencies + ([previous_id] if previous_id else []),
                metadata={"repair_attempt": attempt, "frozen": True},
            )
            issues = corruption_plan_issues(output, trace_contract)
            self.store.save(
                "trace-corruption-plan-validation",
                {"issues": issues},
                dependencies=[artifact_id],
                metadata={"repair_attempt": attempt},
            )
            if not issues:
                return output, artifact_id
            if repair_budget_exhausted(
                attempt, self.settings.max_component_repair_attempts
            ):
                break
            previous_id = artifact_id
            output = await self.injector.run(
                {
                    **payload,
                    "repair_only": {
                        "prior_output": output.model_dump(mode="json"),
                        "validation_issues": issues,
                        "instruction": "Repair only corruption recipes; benchmark and trace contracts are frozen.",
                    },
                },
                session_label=f"trace-fault-injector-repair-{attempt + 1}",
            )
        raise RuntimeError("Trace Fault Injector failed its repair budget")
