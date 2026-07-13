from __future__ import annotations

from pathlib import Path

from .agents import (
    AnchorDirectorAgent,
    ComposerAgent,
    ContractComposerAgent,
    ContractEvaluatorAAgent,
    ContractEvaluatorBAgent,
    DirectorAgent,
    DirectStudyDirectorAgent,
    ExpansionDirectorAgent,
    EvaluatorAAgent,
    EvaluatorBAgent,
    EvidenceCriticAgent,
    EvidenceGlobalAuditAgent,
    EvidenceResolverAgent,
    ExEvaluatorAgent,
    ExperimentDesignerAgent,
    ExperimentorAgent,
    ProgramEvaluatorAAgent,
    ProgramEvaluatorBAgent,
    ReviewerAgent,
    ResearchModeDirectorAgent,
    ResearchProgramComposerAgent,
    TraceBenchmarkCuratorAgent,
    TraceFaultInjectorAgent,
    WriterAgent,
)
from .artifacts import ArtifactStore
from .config import Settings, repair_budget_exhausted
from .execution import LocalExperimentRunner
from .evidence_audit import EvidenceAuditPipeline
from .llm import (
    CodexCLIProvider,
    HeartbeatModelProvider,
    ModelProvider,
    OpenAIResponsesProvider,
    ProviderUsageLimitError,
)
from .rendering import (
    render_audit_report,
    render_paper,
    render_submission_artifacts,
    render_unaccepted_draft,
)
from .provenance import build_provenance_graph, provenance_graph_issues
from .schemas import (
    ExperimentStageResult,
    FinalManifest,
    PaperStageResult,
    ResearchBrief,
    ResearchDepth,
    ResearchMode,
    ResearchModeAssessment,
    ResearchProfile,
    ResearchStageResult,
    RunStatus,
    TraceReviewerDecisionBatch,
    WorkflowAction,
)
from .runtime import (
    DeadlinePolicy,
    RuntimeLifecycle,
    RuntimePhase,
    RuntimeStateStore,
    create_run_id,
)
from .success_contract import (
    build_executable_success_contract,
    verify_success_contract,
)
from .trace_audit import (
    build_claim_ledger,
    claim_ledger_issues,
    reviewer_decision_batch_issues,
)
from .workflows import (
    DirectResearchWorkflow,
    DualDirectorResearchWorkflow,
    ExperimentWorkflow,
    HypothesisWorkflow,
    PaperWorkflow,
    ResearchModeWorkflow,
    TraceAuditPreparationWorkflow,
    research_stage_from_hypotheses,
)
from .workflows.errors import ResearchModeReclassificationError


class ResearchOrchestrator:
    """Thin stage router. Scientific judgments remain in specialist agents."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: ModelProvider | None = None,
        run_id: str | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.run_id = run_id or self._new_run_id()
        self.store = ArtifactStore(settings.runs_dir, self.run_id)
        self.runtime = RuntimeStateStore(self.store.run_dir, self.run_id)
        self.deadline_policy = DeadlinePolicy(self.runtime, settings)
        if provider is not None:
            base_provider = provider
        elif settings.provider == "codex":
            base_provider = CodexCLIProvider(settings)
        else:
            base_provider = OpenAIResponsesProvider(settings)
        self.provider = HeartbeatModelProvider(
            base_provider,
            self._model_heartbeat,
        )
        self.hypothesis_workflow = HypothesisWorkflow(
            settings,
            self.store,
            DirectorAgent(self.provider),
            EvaluatorAAgent(self.provider),
            EvaluatorBAgent(self.provider),
            ComposerAgent(self.provider),
            self.deadline_policy,
        )
        self.research_mode_workflow = ResearchModeWorkflow(
            self.store,
            ResearchModeDirectorAgent(self.provider),
        )
        self.direct_research_workflow = DirectResearchWorkflow(
            settings,
            self.store,
            DirectStudyDirectorAgent(self.provider),
            ContractEvaluatorAAgent(self.provider),
            ContractEvaluatorBAgent(self.provider),
            ContractComposerAgent(self.provider),
            self.deadline_policy,
        )
        self.dual_director_workflow = DualDirectorResearchWorkflow(
            settings,
            self.store,
            AnchorDirectorAgent(self.provider),
            ExpansionDirectorAgent(self.provider),
            ProgramEvaluatorAAgent(self.provider),
            ProgramEvaluatorBAgent(self.provider),
            ResearchProgramComposerAgent(self.provider),
            self.deadline_policy,
        )
        runner = LocalExperimentRunner(
            self.store.experiments_dir,
            timeout_seconds=settings.experiment_timeout_seconds,
            enabled=settings.allow_code_execution,
        )
        self.experiment_workflow = ExperimentWorkflow(
            settings,
            self.store,
            ExperimentDesignerAgent(self.provider),
            ExperimentorAgent(self.provider),
            ExEvaluatorAgent(self.provider),
            runner,
            self.deadline_policy,
            EvidenceAuditPipeline(
                EvidenceCriticAgent(self.provider),
                EvidenceResolverAgent(self.provider),
                EvidenceGlobalAuditAgent(self.provider),
                max_repair_attempts=settings.max_component_repair_attempts,
            ),
        )
        self.trace_preparation_workflow = TraceAuditPreparationWorkflow(
            settings,
            self.store,
            TraceBenchmarkCuratorAgent(self.provider),
            TraceFaultInjectorAgent(self.provider),
        )
        self.paper_workflow = PaperWorkflow(
            settings,
            self.store,
            WriterAgent(self.provider),
            ReviewerAgent(self.provider),
            self.deadline_policy,
        )

    async def run(
        self,
        question: str,
        *,
        objective: str | None = None,
        constraints: list[str] | None = None,
    ) -> FinalManifest:
        question = question.strip()
        if not question:
            raise ValueError("The single research question cannot be empty")
        objective = (objective or question).strip()
        if not objective:
            raise ValueError("The research objective cannot be empty")
        research_depth = ResearchDepth(self.settings.research_depth.upper())
        research_profile = ResearchProfile(self.settings.research_profile.upper())
        brief = ResearchBrief(
            research_objective=objective,
            core_question=question,
            research_depth=research_depth,
            research_profile=research_profile,
            constraints=[item.strip() for item in (constraints or []) if item.strip()],
            assumptions=[],
            required_contributions=self._required_contributions(
                research_depth,
                research_profile,
            ),
        )
        runtime_state = self.runtime.initialize(
            question,
            self.settings,
            objective=objective,
        )
        existing_manifest = self.store.run_dir / "manifest.json"
        if runtime_state.terminal and existing_manifest.exists():
            return FinalManifest.model_validate_json(
                existing_manifest.read_text(encoding="utf-8")
            )
        self.store.event(
            "run.started",
            {
                "question": question,
                "research_objective": objective,
                "research_depth": research_depth,
                "research_profile": research_profile,
                "model": self.settings.model,
                "reasoning_effort": self.settings.reasoning_effort,
                "resumed": runtime_state.restart_count > 0,
            },
        )
        research_feedback = list(
            runtime_state.pending_feedback.get("research", [])
        )
        mode_feedback = list(runtime_state.pending_feedback.get("mode", []))
        mode_reclassification_attempts = 0
        experiment_feedback = list(
            runtime_state.pending_feedback.get("experiment", [])
        )
        unresolved: list[str] = []
        paper_markdown: Path | None = None
        paper_pdf: Path | None = None
        paper_latex: Path | None = None
        submission_metadata: Path | None = None
        self_review: Path | None = None
        format_audit: Path | None = None
        unaccepted_draft: Path | None = None
        audit_report: Path | None = None
        final_stage = "RESEARCH_MODE"
        status = RunStatus.FAILED_WITH_AUDIT
        hypothesis_stage = None
        mode_assessment = self._resumable_mode(question)
        if mode_assessment is not None:
            mode_assessment = self._legacy_compatible_assessment(mode_assessment)
        research_stage = self._resumable_research(question, objective)
        experiment_stage = self._resumable_experiment(research_stage)
        trace_preparation = (
            experiment_stage.trace_preparation
            if experiment_stage is not None
            else None
        )
        trace_reviewer_decisions = (
            experiment_stage.trace_reviewer_decisions
            if experiment_stage is not None
            else None
        )
        paper_stage = self._resumable_paper(experiment_stage)
        claim_ledger = None
        success_contract_passed = False
        deadline_halted = False
        seed_contract: ResearchStageResult | None = None
        locked_research_target_ids: set[str] | None = None
        seed_experiment_stage: ExperimentStageResult | None = None
        initial_experiment_target_ids: set[str] | None = None
        if any(
            item is not None
            for item in (mode_assessment, research_stage, experiment_stage, paper_stage)
        ):
            self.store.event(
                "run.resumed",
                {
                    "mode": mode_assessment is not None,
                    "research": research_stage is not None,
                    "experiment": experiment_stage is not None,
                    "paper": paper_stage is not None,
                },
            )
        try:
            if mode_assessment is None:
                decision = self.deadline_policy.can_start(RuntimePhase.RESEARCH_MODE)
                if not decision.allowed:
                    deadline_halted = True
                    status = RunStatus.PARTIAL_COMPLETION
                    final_stage = "DEADLINE_FINALIZED"
                    unresolved.append(decision.reason)
                else:
                    self.runtime.enter_phase(
                        RuntimePhase.RESEARCH_MODE,
                        "classify_research_mode",
                    )
                    mode_assessment = await self.research_mode_workflow.run(
                        question,
                        research_objective=objective,
                        research_depth=research_depth,
                        research_profile=research_profile,
                    )
                    mode_assessment = self._legacy_compatible_assessment(
                        mode_assessment
                    )
                    self.runtime.complete_phase(RuntimePhase.RESEARCH_MODE)

            for backtrack in range(self.settings.max_global_backtracks + 1):
                if deadline_halted or mode_assessment is None:
                    break
                if research_stage is None:
                    final_stage = "RESEARCH_PLANNING"
                    decision = self.deadline_policy.can_start(
                        RuntimePhase.RESEARCH_PLANNING
                    )
                    if not decision.allowed:
                        deadline_halted = True
                        status = RunStatus.PARTIAL_COMPLETION
                        final_stage = "DEADLINE_FINALIZED"
                        unresolved.append(decision.reason)
                        break
                    self.runtime.enter_phase(
                        RuntimePhase.RESEARCH_PLANNING,
                        "create_and_verify_research_contract",
                    )
                    while research_stage is None:
                        try:
                            if self.settings.dual_director_enabled:
                                research_stage = await self.dual_director_workflow.run(
                                    brief,
                                    mode_assessment,
                                    upstream_feedback=research_feedback,
                                )
                            elif (
                                mode_assessment.proposed_mode
                                == ResearchMode.EXPLANATORY_RESEARCH
                            ):
                                hypothesis_stage = await self.hypothesis_workflow.run(
                                    question,
                                    upstream_feedback=research_feedback,
                                    mode_assessment=mode_assessment,
                                    initial_locked_hypothesis_ids=(
                                        locked_research_target_ids
                                    ),
                                )
                                research_stage = research_stage_from_hypotheses(
                                    mode_assessment,
                                    hypothesis_stage,
                                    max_selected_targets=(
                                        self.settings.target_promoted_hypotheses
                                    ),
                                )
                                contract_id = self.store.save(
                                    "research-contract-final",
                                    research_stage.contract,
                                    dependencies=self.store.find_artifact_ids(
                                        kind="composer-report"
                                    )[-1:],
                                    metadata={
                                        "mode": mode_assessment.proposed_mode,
                                        "source_stage": "HYPOTHESIS",
                                    },
                                )
                                self.store.checkpoint("research", research_stage)
                                self.store.event(
                                    "research_contract.ready",
                                    {
                                        "artifact_id": contract_id,
                                        "mode": mode_assessment.proposed_mode,
                                        "readiness": (
                                            research_stage.contract.readiness
                                        ),
                                        "selected_target_ids": (
                                            research_stage.contract.selected_target_ids
                                        ),
                                    },
                                )
                            else:
                                research_stage = (
                                    await self.direct_research_workflow.run(
                                        mode_assessment,
                                        upstream_feedback=research_feedback,
                                        seed_contract=(
                                            seed_contract.contract
                                            if seed_contract is not None
                                            else None
                                        ),
                                        initial_locked_target_ids=(
                                            locked_research_target_ids
                                        ),
                                    )
                                )
                        except ResearchModeReclassificationError as exc:
                            if repair_budget_exhausted(
                                mode_reclassification_attempts,
                                self.settings.max_component_repair_attempts,
                            ):
                                raise
                            mode_reclassification_attempts += 1
                            mode_feedback.append(str(exc))
                            self.runtime.add_feedback("mode", str(exc))
                            prior_assessment = mode_assessment
                            mode_assessment = await self.research_mode_workflow.run(
                                question,
                                research_objective=objective,
                                research_depth=research_depth,
                                research_profile=research_profile,
                                prior_assessment=prior_assessment,
                                reclassification_feedback=mode_feedback,
                            )
                            mode_assessment = self._legacy_compatible_assessment(
                                mode_assessment
                            )
                            self.runtime.clear_feedback("mode")
                            hypothesis_stage = None
                            research_stage = None
                            seed_contract = None
                            locked_research_target_ids = None
                    self.runtime.complete_phase(RuntimePhase.RESEARCH_PLANNING)

                executable_contract = build_executable_success_contract(
                    research_stage.contract
                )
                contract_artifact = self.store.latest_envelope(
                    "research-contract-final"
                )
                contract_dependencies = (
                    [contract_artifact["artifact_id"]]
                    if contract_artifact is not None
                    else []
                )
                executable_id = self.store.save(
                    "executable-success-contract",
                    executable_contract,
                    dependencies=contract_dependencies,
                )
                success_report = verify_success_contract(
                    executable_contract,
                    research_stage.contract,
                )
                self.store.save(
                    "success-contract-verification",
                    success_report,
                    dependencies=[executable_id],
                )
                success_contract_passed = success_report.passed
                if not success_report.passed:
                    failures = success_report.failure_messages
                    self.store.event(
                        "research_contract.executable_gate_failed",
                        {"issues": failures, "backtrack": backtrack},
                    )
                    if contract_dependencies:
                        self.store.invalidate(
                            contract_dependencies,
                            reason="Executable Research Contract failed",
                            cascade=True,
                        )
                    research_feedback.extend(failures)
                    for failure in failures:
                        self.runtime.add_feedback("research", failure)
                    affected_targets = {
                        target_id
                        for result in success_report.results
                        for target_id in result.affected_target_ids
                        if not result.passed
                    }
                    seed_contract = research_stage
                    locked_research_target_ids = set(
                        research_stage.contract.selected_target_ids
                    ) - affected_targets
                    research_stage = None
                    hypothesis_stage = None
                    experiment_stage = None
                    paper_stage = None
                    if backtrack < self.settings.max_global_backtracks:
                        continue
                    unresolved.extend(failures)
                    status = RunStatus.FAILED_WITH_AUDIT
                    final_stage = "RESEARCH_CONTRACT_FAILED"
                    break
                self.runtime.clear_feedback("research")
                seed_contract = None
                locked_research_target_ids = None

                final_stage = "EXPERIMENT"
                if experiment_stage is None:
                    decision = self.deadline_policy.can_start(RuntimePhase.EXPERIMENT)
                    if not decision.allowed:
                        deadline_halted = True
                        status = RunStatus.PARTIAL_COMPLETION
                        final_stage = "DEADLINE_FINALIZED"
                        unresolved.append(decision.reason)
                        break
                    self.runtime.enter_phase(
                        RuntimePhase.EXPERIMENT,
                        "execute_selected_research_targets",
                    )
                    if research_profile == ResearchProfile.TRACE_AUDIT:
                        trace_preparation = (
                            await self.trace_preparation_workflow.run(
                                research_stage.contract
                            )
                        )
                        if self.settings.trace_prepare_only:
                            manifest = FinalManifest(
                                run_id=self.run_id,
                                question=question,
                                research_objective=objective,
                                research_depth=research_depth,
                                research_profile=research_profile,
                                status=RunStatus.PARTIAL_COMPLETION,
                                model=self.settings.model,
                                reasoning_effort=self.settings.reasoning_effort,
                                pipeline_smoke_test=self.settings.pipeline_smoke_test,
                                research_mode=research_stage.contract.research_mode,
                                research_readiness=research_stage.contract.readiness,
                                selected_target_ids=(
                                    research_stage.contract.selected_target_ids
                                ),
                                final_stage="TRACE_REVIEW_READY",
                                artifact_ids=self.store.artifact_ids,
                                valid_artifact_ids=self.store.valid_artifact_ids,
                                stale_artifact_ids=[
                                    artifact_id
                                    for artifact_id in self.store.artifact_ids
                                    if self.store.status_of(artifact_id) == "STALE"
                                ],
                                unresolved_issues=[
                                    "Prepared and frozen; awaiting the external blinded "
                                    "reviewer-decision batch before experiment execution."
                                ],
                            )
                            self.store.write_manifest(manifest)
                            self.store.event(
                                "trace_review.paused",
                                {
                                    "status": manifest.status,
                                    "final_stage": manifest.final_stage,
                                    "resume_run_id": self.run_id,
                                },
                            )
                            self.runtime.heartbeat(
                                action="await_external_trace_review"
                            )
                            return manifest
                        if not self.settings.pipeline_smoke_test:
                            decisions_path = (
                                self.settings.trace_reviewer_decisions_path
                            )
                            if decisions_path is None:
                                raise RuntimeError(
                                    "TRACE_AUDIT main study requires a frozen external "
                                    "reviewer-decision batch"
                                )
                            trace_reviewer_decisions = (
                                TraceReviewerDecisionBatch.model_validate_json(
                                    decisions_path.read_text(encoding="utf-8")
                                )
                            )
                            decision_issues = reviewer_decision_batch_issues(
                                trace_reviewer_decisions,
                                research_stage.contract.trace_study_contract,
                            )
                            decision_id = self.store.save(
                                "trace-reviewer-decision-batch",
                                trace_reviewer_decisions,
                                dependencies=(
                                    self.store.find_artifact_ids(
                                        kind="trace-corruption-plan"
                                    )[-1:]
                                    + self.store.find_artifact_ids(
                                        kind="trace-external-review-audit"
                                    )[-1:]
                                ),
                                metadata={"frozen": True, "external": True},
                            )
                            self.store.save(
                                "trace-reviewer-decision-batch-validation",
                                {"issues": decision_issues},
                                dependencies=[decision_id],
                            )
                            if decision_issues:
                                raise RuntimeError(
                                    "Invalid external TRACE_AUDIT reviewer decisions: "
                                    + "; ".join(decision_issues)
                                )
                    experiment_stage = await self.experiment_workflow.run(
                        research_stage,
                        upstream_feedback=experiment_feedback,
                        seed_checkpoint=seed_experiment_stage,
                        initial_affected_target_ids=initial_experiment_target_ids,
                        trace_preparation=trace_preparation,
                        trace_reviewer_decisions=trace_reviewer_decisions,
                    )
                    seed_experiment_stage = None
                    initial_experiment_target_ids = None
                    self.runtime.complete_phase(RuntimePhase.EXPERIMENT)
                if (
                    experiment_stage.evaluation.action
                    == WorkflowAction.RETURN_TO_HYPOTHESIS
                    and backtrack < self.settings.max_global_backtracks
                ):
                    research_feedback.append(experiment_stage.evaluation.rationale)
                    self.runtime.add_feedback(
                        "research",
                        experiment_stage.evaluation.rationale,
                    )
                    affected_targets = set(
                        experiment_stage.evaluation.affected_hypothesis_ids
                    )
                    if not affected_targets:
                        affected_targets = set(
                            research_stage.contract.selected_target_ids
                        )
                    seed_contract = research_stage
                    locked_research_target_ids = set(
                        research_stage.contract.selected_target_ids
                    ) - affected_targets
                    self._invalidate_latest(
                        "research-contract-final",
                        "Ex-Evaluator returned the study to research planning",
                    )
                    research_stage = None
                    hypothesis_stage = None
                    experiment_stage = None
                    seed_experiment_stage = None
                    initial_experiment_target_ids = None
                    continue
                if not experiment_stage.passed:
                    if any(
                        reason.startswith("DEADLINE:")
                        for reason in experiment_stage.failure_reasons
                    ):
                        deadline_halted = True
                        status = RunStatus.PARTIAL_COMPLETION
                    else:
                        status = RunStatus.FAILED_WITH_AUDIT
                    unresolved.extend(experiment_stage.failure_reasons)
                    final_stage = (
                        "DEADLINE_FINALIZED"
                        if deadline_halted
                        else "EXPERIMENT_FAILED"
                    )
                    break
                self.runtime.clear_feedback("experiment")
                experiment_feedback.clear()
                claim_ledger = build_claim_ledger(
                    research_stage.contract,
                    experiment_stage,
                )
                ledger_issues = claim_ledger_issues(
                    claim_ledger,
                    research_stage.contract,
                )
                ledger_dependencies = self.store.find_artifact_ids(
                    kind="ex-evaluator-report"
                )[-1:] + self.store.find_artifact_ids(
                    kind="research-contract-final"
                )[-1:]
                ledger_id = self.store.save(
                    "claim-ledger",
                    claim_ledger,
                    dependencies=ledger_dependencies,
                )
                self.store.save(
                    "claim-ledger-validation",
                    {"issues": ledger_issues},
                    dependencies=[ledger_id],
                )
                self.store.checkpoint("claim-ledger", claim_ledger)
                if ledger_issues:
                    status = RunStatus.FAILED_WITH_AUDIT
                    final_stage = "CLAIM_LEDGER_FAILED"
                    unresolved.extend(ledger_issues)
                    break
                provenance_graph = build_provenance_graph(
                    self.store,
                    claim_ledger,
                    experiment_stage,
                )
                provenance_issues = provenance_graph_issues(
                    provenance_graph,
                    claim_ledger,
                )
                provenance_id = self.store.save(
                    "provenance-graph",
                    provenance_graph,
                    dependencies=[ledger_id, *ledger_dependencies],
                )
                self.store.save(
                    "provenance-graph-validation",
                    {"issues": provenance_issues},
                    dependencies=[provenance_id],
                )
                self.store.checkpoint("provenance-graph", provenance_graph)
                if provenance_issues:
                    status = RunStatus.FAILED_WITH_AUDIT
                    final_stage = "PROVENANCE_GRAPH_FAILED"
                    unresolved.extend(provenance_issues)
                    break
                final_stage = "PAPER"
                if paper_stage is None:
                    decision = self.deadline_policy.can_start(RuntimePhase.PAPER)
                    if not decision.allowed:
                        deadline_halted = True
                        status = RunStatus.PARTIAL_COMPLETION
                        final_stage = "DEADLINE_FINALIZED"
                        unresolved.append(decision.reason)
                        break
                    self.runtime.enter_phase(
                        RuntimePhase.PAPER,
                        "write_and_review_paper",
                    )
                    paper_stage = await self.paper_workflow.run(
                        research_stage,
                        experiment_stage,
                        claim_ledger,
                        provenance_graph,
                    )
                    self.runtime.complete_phase(RuntimePhase.PAPER)
                review_action = paper_stage.review.action
                if paper_stage.accepted:
                    status = RunStatus.SUCCESS
                    break
                if any(
                    reason.startswith("DEADLINE:")
                    for reason in paper_stage.failure_reasons
                ):
                    deadline_halted = True
                    status = RunStatus.PARTIAL_COMPLETION
                    unresolved.extend(paper_stage.failure_reasons)
                    final_stage = "DEADLINE_FINALIZED"
                    break
                if (
                    review_action == WorkflowAction.RETURN_TO_HYPOTHESIS
                    and backtrack < self.settings.max_global_backtracks
                ):
                    research_feedback.append(paper_stage.review.rationale)
                    self.runtime.add_feedback(
                        "research",
                        paper_stage.review.rationale,
                    )
                    affected_targets = self._review_affected_targets(
                        paper_stage,
                        research_stage,
                    )
                    seed_contract = research_stage
                    locked_research_target_ids = set(
                        research_stage.contract.selected_target_ids
                    ) - affected_targets
                    self._invalidate_latest(
                        "research-contract-final",
                        "Reviewer returned the study to hypothesis planning",
                    )
                    research_stage = None
                    hypothesis_stage = None
                    experiment_stage = None
                    paper_stage = None
                    seed_experiment_stage = None
                    initial_experiment_target_ids = None
                    continue
                if review_action in {
                    WorkflowAction.RETURN_TO_ANALYSIS,
                    WorkflowAction.RETURN_TO_EXPERIMENT,
                } and backtrack < self.settings.max_global_backtracks:
                    experiment_feedback.append(paper_stage.review.rationale)
                    self.runtime.add_feedback(
                        "experiment",
                        paper_stage.review.rationale,
                    )
                    if review_action == WorkflowAction.RETURN_TO_EXPERIMENT:
                        seed_experiment_stage = experiment_stage
                        initial_experiment_target_ids = self._review_affected_targets(
                            paper_stage,
                            research_stage,
                        )
                    else:
                        seed_experiment_stage = None
                        initial_experiment_target_ids = None
                        self._invalidate_latest(
                            "experiment-contract",
                            "Reviewer returned the study to analysis",
                        )
                    experiment_stage = None
                    paper_stage = None
                    continue
                if review_action == WorkflowAction.REJECT:
                    status = RunStatus.FAILED_WITH_AUDIT
                else:
                    status = RunStatus.INCONCLUSIVE
                unresolved.extend(issue.issue for issue in paper_stage.review.fatal_issues)
                unresolved.extend(paper_stage.review.acceptance_conditions)
                unresolved.extend(paper_stage.failure_reasons)
                break
        except Exception as exc:
            self.store.event(
                "run.failed",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            self.runtime.record_error(f"{type(exc).__name__}: {exc}")
            current_state = self.runtime.load()
            deadline_decision = (
                self.deadline_policy.can_start(current_state.phase)
                if current_state is not None
                else None
            )
            if isinstance(exc, ProviderUsageLimitError):
                unresolved.append(str(exc))
                status = RunStatus.SYSTEM_FAILURE
                final_stage = "PROVIDER_UNAVAILABLE"
            elif deadline_decision is not None and not deadline_decision.allowed:
                deadline_halted = True
                unresolved.append(deadline_decision.reason)
                status = RunStatus.PARTIAL_COMPLETION
                final_stage = "DEADLINE_FINALIZED"
            else:
                unresolved.append(f"{type(exc).__name__}: {exc}")
                status = RunStatus.FAILED_WITH_AUDIT

        success_invariants_hold = (
            research_stage is not None
            and research_stage.contract.readiness.value != "PROPOSED"
            and success_contract_passed
            and experiment_stage is not None
            and experiment_stage.passed
            and paper_stage is not None
            and paper_stage.accepted
        )
        if status == RunStatus.SUCCESS and not success_invariants_hold:
            status = RunStatus.FAILED_WITH_AUDIT
            unresolved.append(
                "SUCCESS invariant failed: ready contract, passed experiment, and "
                "accepted paper are all required"
            )

        self.runtime.enter_phase(
            RuntimePhase.FINALIZATION,
            "render_and_verify_final_artifacts",
        )
        if (
            status == RunStatus.SUCCESS
            and paper_stage is not None
            and paper_stage.accepted
        ):
            paper_markdown, paper_pdf = render_paper(
                paper_stage.draft, self.store.paper_dir
            )
            if research_profile == ResearchProfile.TRACE_AUDIT:
                (
                    paper_latex,
                    submission_metadata,
                    self_review,
                    submission_audit,
                ) = render_submission_artifacts(
                    paper_stage.draft,
                    paper_stage.review,
                    self.store.paper_dir,
                    require_official_pdf=(
                        not self.settings.pipeline_smoke_test
                    ),
                )
                format_audit = self.store.paper_dir / "format_audit.json"
                audit_id = self.store.save(
                    "submission-format-audit",
                    submission_audit,
                    dependencies=self.store.find_artifact_ids(
                        kind="review-report"
                    )[-1:],
                )
                self.store.event(
                    "submission.format_audited",
                    {
                        "artifact_id": audit_id,
                        "main_body_pages": submission_audit.main_body_pages,
                        "issues": submission_audit.issues,
                    },
                )
                if submission_audit.issues:
                    status = RunStatus.FAILED_WITH_AUDIT
                    final_stage = "SUBMISSION_FORMAT_FAILED"
                    unresolved.extend(submission_audit.issues)
                    paper_markdown = None
                    paper_pdf = None
                else:
                    final_stage = "FINAL"
            else:
                final_stage = "FINAL"
        elif paper_stage is not None:
            unaccepted_draft = render_unaccepted_draft(
                paper_stage.draft,
                paper_stage.review,
                self.store.paper_dir,
            )
            final_stage = "PAPER_UNACCEPTED"
            unresolved.extend(paper_stage.failure_reasons)
        elif experiment_stage is not None and not experiment_stage.passed:
            final_stage = "EXPERIMENT_FAILED"
            unresolved.extend(experiment_stage.failure_reasons)
        elif experiment_stage is not None:
            unresolved.append("The experiment stage completed without a paper draft.")
        elif research_stage is not None:
            unresolved.append("Research planning completed without experiments.")

        if deadline_halted and status != RunStatus.SUCCESS:
            final_stage = "DEADLINE_FINALIZED"

        if status != RunStatus.SUCCESS:
            audit_report = render_audit_report(
                self.store.paper_dir,
                question=question,
                final_stage=final_stage,
                status=status.value,
                details=unresolved,
            )

        manifest = FinalManifest(
            run_id=self.run_id,
            question=question,
            research_objective=objective,
            research_depth=research_depth,
            research_profile=research_profile,
            status=status,
            model=self.settings.model,
            reasoning_effort=self.settings.reasoning_effort,
            pipeline_smoke_test=self.settings.pipeline_smoke_test,
            research_mode=(
                research_stage.contract.research_mode
                if research_stage
                else (mode_assessment.proposed_mode if mode_assessment else None)
            ),
            research_readiness=(
                research_stage.contract.readiness if research_stage else None
            ),
            selected_target_ids=(
                research_stage.contract.selected_target_ids if research_stage else []
            ),
            final_stage=final_stage,
            paper_markdown=str(paper_markdown) if paper_markdown else None,
            paper_pdf=str(paper_pdf) if paper_pdf else None,
            paper_latex=str(paper_latex) if paper_latex else None,
            submission_metadata=(
                str(submission_metadata) if submission_metadata else None
            ),
            self_review=str(self_review) if self_review else None,
            format_audit=str(format_audit) if format_audit else None,
            unaccepted_draft=(
                str(unaccepted_draft) if unaccepted_draft else None
            ),
            audit_report=str(audit_report) if audit_report else None,
            artifact_ids=self.store.artifact_ids,
            valid_artifact_ids=self.store.valid_artifact_ids,
            stale_artifact_ids=[
                artifact_id
                for artifact_id in self.store.artifact_ids
                if self.store.status_of(artifact_id) == "STALE"
            ],
            unresolved_issues=list(dict.fromkeys(unresolved)),
        )
        self.store.write_manifest(manifest)
        self.store.event(
            "run.finished",
            {"status": manifest.status, "final_stage": manifest.final_stage},
        )
        lifecycle = (
            RuntimeLifecycle.SUCCESS
            if manifest.status == RunStatus.SUCCESS
            else (
                RuntimeLifecycle.PARTIAL
                if manifest.status
                in {
                    RunStatus.INCONCLUSIVE,
                    RunStatus.NEGATIVE_RESULT,
                    RunStatus.PARTIAL_COMPLETION,
                }
                else RuntimeLifecycle.FAILED
            )
        )
        self.runtime.mark_terminal(
            lifecycle,
            reason=f"{manifest.status.value}: {manifest.final_stage}",
        )
        return manifest

    def _model_heartbeat(self, session_label: str, status: str) -> None:
        self.runtime.model_heartbeat(session_label, status)

    def _resumable_mode(self, question: str) -> ResearchModeAssessment | None:
        value = self.store.load_checkpoint("research-mode", ResearchModeAssessment)
        expected_profile = ResearchProfile(self.settings.research_profile.upper())
        if (
            value is not None
            and value.original_question == question
            and value.research_profile == expected_profile
        ):
            return value
        return None

    def _legacy_compatible_assessment(
        self,
        value: ResearchModeAssessment,
    ) -> ResearchModeAssessment:
        if (
            self.settings.dual_director_enabled
            or value.proposed_mode != ResearchMode.HYBRID_RESEARCH
        ):
            return value
        surface_mode = value.surface_mode
        if surface_mode in {None, ResearchMode.HYBRID_RESEARCH}:
            surface_mode = ResearchMode.DIRECT_TEST
        normalized = ResearchModeAssessment.model_validate(
            {
                **value.model_dump(mode="json"),
                "proposed_mode": surface_mode,
                "surface_mode": surface_mode,
                "requires_competing_hypotheses": (
                    surface_mode == ResearchMode.EXPLANATORY_RESEARCH
                ),
            }
        )
        artifact_id = self.store.save(
            "research-mode-legacy-normalization",
            normalized,
            metadata={"source_mode": value.proposed_mode},
        )
        self.store.checkpoint("research-mode", normalized)
        self.store.event(
            "research_mode.legacy_normalized",
            {
                "artifact_id": artifact_id,
                "source_mode": value.proposed_mode,
                "mode": normalized.proposed_mode,
            },
        )
        return normalized

    def _resumable_research(
        self,
        question: str,
        objective: str,
    ) -> ResearchStageResult | None:
        if self.store.latest_envelope("research-contract-final") is None:
            return None
        value = self.store.load_checkpoint("research", ResearchStageResult)
        if (
            value is not None
            and value.contract.original_question == question
            and value.contract.readiness.value != "PROPOSED"
            and value.contract.research_profile
            == ResearchProfile(self.settings.research_profile.upper())
        ):
            if self.settings.dual_director_enabled and (
                value.research_brief is None
                or value.research_brief.research_objective != objective
            ):
                return None
            return value
        return None

    @staticmethod
    def _required_contributions(
        depth: ResearchDepth,
        profile: ResearchProfile = ResearchProfile.GENERAL,
    ) -> list[str]:
        contributions = {
            ResearchDepth.QUICK: ["EMPIRICAL_ANCHOR"],
            ResearchDepth.COMPETITION: [
                "EMPIRICAL_ANCHOR",
                "ROBUSTNESS_OR_BOUNDARY_EXTENSION",
            ],
            ResearchDepth.THESIS: [
                "EMPIRICAL_ANCHOR",
                "MECHANISM_BOUNDARY_OR_GENERALIZATION_EXTENSION",
            ],
            ResearchDepth.PUBLICATION: [
                "EMPIRICAL_ANCHOR",
                "NOVEL_EXTENSION",
                "INDEPENDENT_GENERALIZATION_OR_THEORY_TEST",
            ],
        }[depth]
        if profile == ResearchProfile.TRACE_AUDIT:
            contributions = [
                *contributions,
                "SOURCE_GROUNDED_RESEARCH_TENSION",
                "PAIRED_C0_C1_C2_C3_FALSE_ACCEPTANCE_STUDY",
                "DETERMINISTIC_CLAIM_RESULT_CODE_TRACE_GATE",
                "CLEAN_ACCEPTANCE_AND_COST_TRADEOFF",
            ]
        return contributions

    def _resumable_experiment(
        self,
        research_stage: ResearchStageResult | None,
    ) -> ExperimentStageResult | None:
        if research_stage is None:
            return None
        value = self.store.load_checkpoint("experiment", ExperimentStageResult)
        if value is None or not value.passed:
            return None
        expected = {
            (item.experiment_id, item.code_hash) for item in value.executions
        }
        available: set[tuple[str | None, str | None]] = set()
        for artifact_id in self.store.find_artifact_ids(kind="execution-result"):
            envelope = self.store.artifact_envelope(artifact_id)
            if envelope is None:
                continue
            payload = envelope.get("payload", {})
            available.add(
                (payload.get("experiment_id"), payload.get("code_hash"))
            )
        if not expected.issubset(available):
            return None
        return value

    def _resumable_paper(
        self,
        experiment_stage: ExperimentStageResult | None,
    ) -> PaperStageResult | None:
        if experiment_stage is None:
            return None
        value = self.store.load_checkpoint("paper", PaperStageResult)
        if value is None or not value.accepted:
            return None
        result_ids = {
            result_id
            for execution in experiment_stage.executions
            for result_id in execution.result_ids
        }
        if any(
            not set(claim.result_ids).issubset(result_ids)
            for claim in value.draft.linked_claims
        ):
            return None
        if self.store.latest_envelope("paper-draft") is None:
            return None
        return value

    def _invalidate_latest(self, kind: str, reason: str) -> None:
        envelope = self.store.latest_envelope(kind)
        if envelope is not None:
            self.store.invalidate(
                [envelope["artifact_id"]],
                reason=reason,
                cascade=True,
            )

    @staticmethod
    def _review_affected_targets(
        paper_stage: PaperStageResult,
        research_stage: ResearchStageResult,
    ) -> set[str]:
        selected = set(research_stage.contract.selected_target_ids)
        issue_text = "\n".join(
            [
                paper_stage.review.rationale,
                *paper_stage.review.acceptance_conditions,
                *[
                    " ".join(
                        [
                            issue.issue,
                            issue.required_fix,
                            *issue.evidence,
                        ]
                    )
                    for issue in (
                        paper_stage.review.fatal_issues
                        + paper_stage.review.non_fatal_issues
                    )
                ],
            ]
        )
        affected = {target_id for target_id in selected if target_id in issue_text}
        return affected or selected

    @staticmethod
    def _new_run_id() -> str:
        return create_run_id()
