from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ai_scientist.config import Settings
from ai_scientist.llm import ModelProvider
from ai_scientist.orchestrator import ResearchOrchestrator
from ai_scientist.program_validation import EVALUATOR_A_GATES, EVALUATOR_B_GATES
from ai_scientist.schemas import (
    ClaimDependency,
    ClaimDependencyRelation,
    ClaimDirectorOutput,
    ClaimErrorNote,
    ClaimEvaluation,
    ClaimEvaluatorReport,
    ContaminationStatus,
    CriterionScore,
    DirectorRole,
    EvaluationDecision,
    EvidenceLocation,
    EvidenceConcernCategory,
    EvidenceConcernResolution,
    EvidenceConcernSeverity,
    EvidenceCriticReport,
    EvidenceQuestionDraft,
    EvidenceResolutionStatus,
    EvidenceUnit,
    ExperimentContract,
    ExperimentorContamination,
    ExperimentorOutput,
    ExEvaluatorReport,
    GeneratedFile,
    HypothesisExperimentSpec,
    HypothesisResultJudgment,
    LinkedPaperClaim,
    NearestWorkComparison,
    PaperDraft,
    PaperReference,
    ProgramClaimType,
    ResearchClaimProposal,
    ResearchDepth,
    ResearchMode,
    ResearchModeAssessment,
    ResearchProfile,
    ResearchProgramComposition,
    ResearchProgramStage,
    ResearchResultStatus,
    ReviewReport,
    RunStatus,
    TargetGateScore,
    TraceBenchmarkPlan,
    TraceBenchmarkSource,
    TraceCorruptionPlan,
    TraceCorruptionRecipe,
    TraceFaultType,
    TraceStudyContract,
    TraceTension,
    VerificationStatus,
    WorkflowAction,
)
from ai_scientist.workflows.experiment import EX_EVALUATOR_CRITERIA
from ai_scientist.workflows.paper import REVIEW_CRITERIA


T = TypeVar("T", bound=BaseModel)
QUESTION = (
    "Does structured claim-result-code provenance reduce false acceptance "
    "in AI-scientist review?"
)
OBJECTIVE = "Test a bounded provenance gate and one mechanism-oriented extension."


def _evidence(evidence_id: str, title: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        title=title,
        authors=["A. Researcher"],
        year=2026,
        url=f"https://example.org/{evidence_id.lower()}",
        evidence_type="mock paper",
        location=EvidenceLocation(section="Results", paragraph=1),
        verbatim_excerpt="The mock study reports a bounded, source-located result.",
        context_summary="Synthetic evidence used only for pipeline plumbing tests.",
        verification_status=VerificationStatus.FULL_TEXT_VERIFIED,
    )


def _claim(
    claim_id: str,
    role: DirectorRole,
    claim_type: ProgramClaimType,
    evidence_id: str,
    *,
    dependency: ClaimDependency | None = None,
    tension_ids: list[str] | None = None,
) -> ResearchClaimProposal:
    is_anchor = role == DirectorRole.ANCHOR
    return ResearchClaimProposal(
        claim_id=claim_id,
        source_role=role,
        claim_type=claim_type,
        statement=(
            "A structured provenance gate reduces false acceptance by at least "
            "0.10 on the bounded mock task."
            if is_anchor
            else "Trace completeness mediates the provenance gate's rejection behavior."
        ),
        null_statement=(
            "The provenance gate changes false acceptance by less than 0.10."
            if is_anchor
            else "Trace completeness does not change rejection behavior."
        ),
        rationale="The claim is falsifiable and useful even under a null result.",
        mechanism=(
            ""
            if is_anchor
            else "Missing claim-result-code links expose unsupported acceptance decisions."
        ),
        dependencies=[dependency] if dependency else [],
        tension_ids=tension_ids or [],
        distinctive_prediction=(
            "False acceptance decreases by at least 0.10 under the gate."
            if is_anchor
            else "Malformed traces are rejected more often than complete traces."
        ),
        falsification_condition=(
            "Reject if the false-acceptance reduction is below 0.10."
            if is_anchor
            else "Reject if complete and malformed traces differ by less than 0.05."
        ),
        alternative_explanations=["Reviewer verbosity", "Mock-task artifacts"],
        positive_result_value="Supports the bounded claim.",
        negative_result_value="Rejects the bounded claim and identifies a failed gate.",
        null_result_value="Constrains the useful effect size.",
        minimum_experiment="Run a deterministic CPU-only synthetic review task.",
        required_data="A fixed local synthetic fixture.",
        required_resources=["one CPU core"],
        compute_estimate="Less than one minute on a local CPU.",
        uncertainties=["Synthetic-task external validity"],
        evidence_ids=[evidence_id],
        controlled_variables=["task", "seed", "review rubric"],
        manipulated_variables=["provenance condition"],
        measurement="false acceptance rate",
        decision_threshold=(
            "absolute reduction >= 0.10" if is_anchor else "rate difference >= 0.05"
        ),
    )


def _director(
    role: DirectorRole,
    *,
    trace_profile: bool = False,
) -> ClaimDirectorOutput:
    tension_id = "A-T1" if role == DirectorRole.ANCHOR else "X-T1"
    if role == DirectorRole.ANCHOR:
        evidence = _evidence("E1", "Mock Provenance Baseline")
        claims = [
            _claim(
                "A1",
                role,
                ProgramClaimType.EMPIRICAL,
                "E1",
                tension_ids=[tension_id] if trace_profile else [],
            )
        ]
    else:
        evidence = _evidence("E2", "Mock Trace Completeness Study")
        claims = [
            _claim(
                "X1",
                role,
                ProgramClaimType.MECHANISTIC,
                "E2",
                dependency=ClaimDependency(
                    claim_id="A1",
                    relation=ClaimDependencyRelation.REQUIRES_TEST,
                ),
                tension_ids=[tension_id] if trace_profile else [],
            )
        ]
    trace_tensions = (
        [
            TraceTension(
                tension_id=tension_id,
                source_role=role,
                statement=(
                    "More review context may not prevent false acceptance when "
                    "claim-result-code relationships remain unchecked."
                ),
                agreements=["Artifact access can expose evidence unavailable in a paper."],
                conflicts=["Raw artifact access does not guarantee cross-artifact checking."],
                unexplained_phenomenon=(
                    "Structurally inconsistent research packages can still be accepted."
                ),
                alternative_explanations=["Reviewer capability", "Context overload"],
                importance="False acceptance undermines autonomous research reliability.",
                why_now="Long-horizon AI-scientist systems now emit auditable artifacts.",
                falsifiable_probe="Compare blinded C0-C3 decisions on paired packages.",
                nearest_work=[
                    NearestWorkComparison(
                        evidence_id=evidence.evidence_id,
                        answered_aspect="The mock source studies artifact-aware review.",
                        unresolved_difference=(
                            "It does not isolate structured provenance from deterministic gates."
                        ),
                    )
                ],
                evidence_ids=[evidence.evidence_id],
                confidence=0.8,
            )
        ]
        if trace_profile
        else []
    )
    return ClaimDirectorOutput(
        artifact_version="1.0",
        director_role=role,
        research_objective=OBJECTIVE,
        core_question=QUESTION,
        scope="A local CPU-only synthetic audit of provenance-gated review.",
        assumptions=["The synthetic fixture is deterministic."],
        evidence=[evidence],
        trace_tensions=trace_tensions,
        selected_trace_tension_ids=[tension_id] if trace_profile else [],
        claims=claims,
        search_limitations=["Mock evidence is not scientific evidence."],
    )


def _claim_evaluator(role: str, gates: set[str]) -> ClaimEvaluatorReport:
    rows = (("A1", "E1"), ("X1", "E2"))
    return ClaimEvaluatorReport(
        evaluator_role=role,
        rubric_version="1.0",
        artifact_version="1.0",
        discovered_evidence=[],
        claim_evaluations=[
            ClaimEvaluation(
                claim_id=claim_id,
                gates=[
                    TargetGateScore(
                        gate=gate,
                        score=4,
                        passed=True,
                        evidence_ids=[evidence_id],
                        reason="The mock claim passes this plumbing-test gate.",
                        counterargument="Synthetic evidence cannot establish external validity.",
                    )
                    for gate in sorted(gates)
                ],
                fatal_issues=[],
                recommended_decision=EvaluationDecision.PROMOTE,
            )
            for claim_id, evidence_id in rows
        ],
        error_notebook=[],
        overall_decision=EvaluationDecision.PROMOTE,
        rationale="Both mock claims pass the structural gates.",
    )


def _methods_evaluator_revising_x1() -> ClaimEvaluatorReport:
    value = _claim_evaluator("methods", EVALUATOR_B_GATES)
    failed_gate = sorted(EVALUATOR_B_GATES)[0]
    evaluations = []
    for evaluation in value.claim_evaluations:
        if evaluation.claim_id != "X1":
            evaluations.append(evaluation)
            continue
        gates = [
            gate.model_copy(
                update={"score": 2, "passed": False}
            )
            if gate.gate == failed_gate
            else gate
            for gate in evaluation.gates
        ]
        evaluations.append(
            evaluation.model_copy(
                update={
                    "gates": gates,
                    "recommended_decision": EvaluationDecision.REVISE,
                }
            )
        )
    return value.model_copy(
        update={
            "claim_evaluations": evaluations,
            "error_notebook": [
                ClaimErrorNote(
                    claim_id="X1",
                    source_role=DirectorRole.EXPANSION,
                    failed_gates=[failed_gate],
                    counterexample="The first mock prediction is not discriminating enough.",
                    failure_cause="The methods gate needs a sharper boundary condition.",
                    forbidden_revision="Do not change the locked A1 claim.",
                    required_revision="Return a repaired X1 while preserving A1.",
                    preserve_claim_ids=["A1"],
                )
            ],
            "overall_decision": EvaluationDecision.REVISE,
            "rationale": "A1 passes, while X1 needs one claim-scoped revision.",
        }
    )


def _composition(*, trace_profile: bool = False) -> ResearchProgramComposition:
    return ResearchProgramComposition(
        action=WorkflowAction.PROMOTE,
        integrated_claim_ids=["A1", "X1"],
        deferred_claim_ids=[],
        selected_trace_tension_ids=(
            ["A-T1", "X-T1"] if trace_profile else []
        ),
        stages=[
            ResearchProgramStage(
                stage_number=1,
                name="Establish the bounded effect",
                claim_ids=["A1"],
                purpose="Test false-acceptance reduction.",
                entry_condition="The mock program is approved.",
                completion_gate="A1 has a traceable result.",
            ),
            ResearchProgramStage(
                stage_number=2,
                name="Probe the trace mechanism",
                claim_ids=["X1"],
                purpose="Test the trace-completeness extension.",
                entry_condition="A1 has been executed.",
                completion_gate="X1 has a traceable result.",
            ),
        ],
        scope="A local CPU-only synthetic audit of provenance-gated review.",
        mode_rationale="A direct anchor is paired with one mechanism-oriented extension.",
        claim_ceiling="Only the two bounded synthetic claims may be reported.",
        failure_notebook=[],
        rationale="The two promoted claims form a dependency-ordered program.",
    )


def _criterion(name: str, trace_id: str) -> CriterionScore:
    return CriterionScore(
        criterion=name,
        score=4,
        evidence_ids=[trace_id],
        reason="The referenced mock artifact satisfies this structural criterion.",
        counterargument="A mock pass does not establish scientific quality.",
        confidence=0.95,
        missing_information=[],
    )


def _trace_experiment_code(target_id: str) -> str:
    return (
        "import json\n"
        "import math\n"
        "import random\n\n"
        "cases = [\n"
        "    {'case_id': 'fault-1', 'gold_faulty': True},\n"
        "    {'case_id': 'fault-2', 'gold_faulty': True},\n"
        "    {'case_id': 'clean-1', 'gold_faulty': False},\n"
        "    {'case_id': 'clean-2', 'gold_faulty': False},\n"
        "]\n"
        "conditions = [\n"
        "    'C0_PAPER_ONLY', 'C1_RAW_ARTIFACTS',\n"
        "    'C2_STRUCTURED_PROVENANCE', 'C3_TRACE_GATE',\n"
        "]\n"
        "decisions = []\n"
        "for case_index, case in enumerate(cases):\n"
        "    for condition_index, condition in enumerate(conditions):\n"
        "        if not case['gold_faulty']:\n"
        "            accepted = True\n"
        "        elif condition == 'C0_PAPER_ONLY':\n"
        "            accepted = True\n"
        "        elif condition in {'C1_RAW_ARTIFACTS', 'C2_STRUCTURED_PROVENANCE'}:\n"
        "            accepted = case['case_id'] == 'fault-1'\n"
        "        else:\n"
        "            accepted = False\n"
        "        decisions.append({\n"
        "            'case_id': case['case_id'],\n"
        "            'condition_id': condition,\n"
        "            'gold_faulty': case['gold_faulty'],\n"
        "            'accepted': accepted,\n"
        "            'reviewer_model': 'mock-rule-reviewer',\n"
        "            'confidence': 0.8,\n"
        "            'detected_fault_types': (\n"
        "                ['RESULT_DIRECTION'] if case['gold_faulty'] and not accepted else []\n"
        "            ),\n"
        "            'latency_seconds': 0.01 * (condition_index + 1),\n"
        "            'input_tokens': 100 + 10 * condition_index,\n"
        "            'output_tokens': 20 + case_index,\n"
        "        })\n"
        "metrics = []\n"
        "for condition in conditions:\n"
        "    rows = [row for row in decisions if row['condition_id'] == condition]\n"
        "    faulty = [row for row in rows if row['gold_faulty']]\n"
        "    clean = [row for row in rows if not row['gold_faulty']]\n"
        "    metrics.append({\n"
        "        'condition_id': condition,\n"
        "        'false_acceptance_rate': sum(row['accepted'] for row in faulty) / len(faulty),\n"
        "        'clean_acceptance_rate': sum(row['accepted'] for row in clean) / len(clean),\n"
        "        'faulty_cases': len(faulty),\n"
        "        'clean_cases': len(clean),\n"
        "        'mean_latency_seconds': sum(row['latency_seconds'] for row in rows) / len(rows),\n"
        "        'mean_input_tokens': sum(row['input_tokens'] for row in rows) / len(rows),\n"
        "        'mean_output_tokens': sum(row['output_tokens'] for row in rows) / len(rows),\n"
        "    })\n"
        "comparison_specs = [\n"
        "    ('C3_vs_C0', 'C3_TRACE_GATE', 'C0_PAPER_ONLY'),\n"
        "    ('C1_vs_C0', 'C1_RAW_ARTIFACTS', 'C0_PAPER_ONLY'),\n"
        "    ('C2_vs_C1', 'C2_STRUCTURED_PROVENANCE', 'C1_RAW_ARTIFACTS'),\n"
        "    ('C3_vs_C2', 'C3_TRACE_GATE', 'C2_STRUCTURED_PROVENANCE'),\n"
        "]\n"
        "row_index = {(row['reviewer_model'], row['case_id'], row['condition_id']): row for row in decisions}\n"
        "pair_keys = sorted({(row['reviewer_model'], row['case_id']) for row in decisions})\n"
        "comparisons = []\n"
        "for comparison_id, treatment_id, baseline_id in comparison_specs:\n"
        "    paired = [(row_index[(reviewer, case_id, treatment_id)], row_index[(reviewer, case_id, baseline_id)]) for reviewer, case_id in pair_keys]\n"
        "    faulty = [pair for pair in paired if pair[0]['gold_faulty']]\n"
        "    clean = [pair for pair in paired if not pair[0]['gold_faulty']]\n"
        "    faulty_diffs = [int(treatment['accepted']) - int(baseline['accepted']) for treatment, baseline in faulty]\n"
        "    clean_diffs = [int(treatment['accepted']) - int(baseline['accepted']) for treatment, baseline in clean]\n"
        "    improvement = sum(baseline['accepted'] and not treatment['accepted'] for treatment, baseline in faulty)\n"
        "    regression = sum(treatment['accepted'] and not baseline['accepted'] for treatment, baseline in faulty)\n"
        "    total = improvement + regression\n"
        "    if total:\n"
        "        tail = sum(math.comb(total, value) for value in range(min(improvement, regression) + 1)) / (2 ** total)\n"
        "        p_value = min(1.0, 2 * tail)\n"
        "    else:\n"
        "        p_value = 1.0\n"
        "    generator = random.Random(1729)\n"
        "    draws = []\n"
        "    for _ in range(2000):\n"
        "        sample = [faulty_diffs[generator.randrange(len(faulty_diffs))] for _ in faulty_diffs]\n"
        "        draws.append(sum(sample) / len(sample))\n"
        "    draws.sort()\n"
        "    comparisons.append({\n"
        "        'comparison_id': comparison_id,\n"
        "        'treatment_id': treatment_id,\n"
        "        'baseline_id': baseline_id,\n"
        "        'false_acceptance_difference': sum(faulty_diffs) / len(faulty_diffs),\n"
        "        'clean_acceptance_difference': sum(clean_diffs) / len(clean_diffs),\n"
        "        'mean_latency_difference': sum(treatment['latency_seconds'] - baseline['latency_seconds'] for treatment, baseline in paired) / len(paired),\n"
        "        'improvement_pairs': improvement,\n"
        "        'regression_pairs': regression,\n"
        "        'mcnemar_exact_p_value': p_value,\n"
        "        'bootstrap_ci_low': draws[int(0.025 * (len(draws) - 1))],\n"
        "        'bootstrap_ci_high': draws[int(0.975 * (len(draws) - 1))],\n"
        "    })\n"
        "result = {\n"
        "    'study_type': 'TRACE_AUDIT',\n"
        "    'study_mode': 'PIPELINE_SMOKE_TEST',\n"
        "    'scientific_claim_valid': False,\n"
        f"    'analysis_target_id': {target_id!r},\n"
        "    'benchmark_case_count': len(cases),\n"
        "    'corruption_manifest_hash': '0123456789abcdef0123456789abcdef',\n"
        "    'leakage_check_passed': True,\n"
        "    'human_adjudication_minutes': 0.0,\n"
        "    'decisions': decisions,\n"
        "    'condition_metrics': metrics,\n"
        "    'paired_comparisons': comparisons,\n"
        "    'notes': ['CPU-only deterministic trace-audit plumbing test.'],\n"
        "}\n"
        "with open('result.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(result, handle, sort_keys=True)\n"
    )


class ScriptedPipelineProvider(ModelProvider):
    """Schema-aware fake model; all non-model pipeline code remains real."""

    def __init__(
        self,
        profile: ResearchProfile = ResearchProfile.GENERAL,
    ) -> None:
        self.calls: list[str] = []
        self.profile = profile

    async def generate(
        self,
        schema: type[T],
        *,
        instructions: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        session_label: str,
    ) -> T:
        self.calls.append(session_label)
        if schema is ResearchModeAssessment:
            value: BaseModel = ResearchModeAssessment(
                original_question=QUESTION,
                proposed_mode=ResearchMode.HYBRID_RESEARCH,
                surface_mode=ResearchMode.DIRECT_TEST,
                classification_reason="The bounded test supports one explanatory extension.",
                direct_testable_claim="Structured provenance reduces false acceptance.",
                requires_competing_hypotheses=False,
                comparison_entities=["paper-only review", "provenance-gated review"],
                primary_outcome="false acceptance rate",
                claim_ceiling="Evidence-gated synthetic claims only.",
                confidence=0.95,
                unresolved_ambiguities=[],
                research_depth=ResearchDepth.COMPETITION,
                research_profile=self.profile,
            )
        elif schema is ClaimDirectorOutput:
            role = (
                DirectorRole.ANCHOR
                if session_label.startswith("anchor-")
                else DirectorRole.EXPANSION
            )
            value = _director(
                role,
                trace_profile=self.profile == ResearchProfile.TRACE_AUDIT,
            )
        elif schema is ClaimEvaluatorReport:
            is_a = "evaluator-a" in session_label
            required_a_gates = set(
                payload.get("required_evaluator_a_gates", EVALUATOR_A_GATES)
            )
            value = _claim_evaluator(
                "literature" if is_a else "methods",
                required_a_gates if is_a else EVALUATOR_B_GATES,
            )
        elif schema is ResearchProgramComposition:
            value = _composition(
                trace_profile=self.profile == ResearchProfile.TRACE_AUDIT
            )
        elif schema is TraceBenchmarkPlan:
            value = TraceBenchmarkPlan(
                plan_version="1.0",
                trace_contract_fingerprint=payload["trace_contract_fingerprint"],
                planned_case_count=payload["trace_study_contract"][
                    "benchmark_min_cases"
                ],
                sources=[
                    TraceBenchmarkSource(
                        source_id="SRC-MOCK-1",
                        source_kind="programmatically generated fixture",
                        location="embedded CPU-only test fixture",
                        inclusion_reason="Exercises paired clean and faulty packages.",
                        identity_risk="none; contains no author metadata",
                    )
                ],
                inclusion_criteria=["Package has paper, code, result, and manifest."],
                exclusion_criteria=["Package cannot be replayed deterministically."],
                clean_case_definition="All frozen cross-artifact invariants pass.",
                paired_variant_policy="Apply one bounded recipe to each clean package.",
                pilot_policy="Use pilot decisions only to verify plumbing.",
                main_split_frozen=True,
                gold_label_policy="Use hidden deterministic corruption manifests.",
                adjudication_policy="Replay every recipe and resolve only manifest conflicts.",
            )
        elif schema is TraceCorruptionPlan:
            value = TraceCorruptionPlan(
                plan_version="1.0",
                trace_contract_fingerprint=payload["trace_contract_fingerprint"],
                recipes=[
                    TraceCorruptionRecipe(
                        recipe_id=f"CR-{index:02d}",
                        fault_type=TraceFaultType(fault_type),
                        precondition="The clean package passes all frozen invariants.",
                        transformation=f"Inject exactly one {fault_type} defect.",
                        expected_gold_label="faulty",
                        replay_check="Reapply and verify the intended invariant fails once.",
                        hidden_fields=["gold_faulty", "fault_type", "condition_id"],
                    )
                    for index, fault_type in enumerate(
                        payload["trace_study_contract"]["fault_types"],
                        start=1,
                    )
                ],
                manifest_fields=[
                    "case_id",
                    "source_hash",
                    "corrupted_hash",
                    "fault_type",
                    "gold_faulty",
                ],
                hidden_from_reviewer=True,
                deterministic_replay=True,
                leakage_test="Assert hidden manifest fields are absent from reviewer inputs.",
            )
        elif schema is ExperimentContract:
            target_ids = payload["selected_target_ids"]
            value = ExperimentContract(
                contract_version="1.0",
                hypothesis_ids=target_ids,
                dataset_plan="Use the fixed local synthetic fixture.",
                shared_protocol=["Use one deterministic seed", "Write canonical JSON"],
                metrics=["mock support score"],
                seeds=[7],
                statistical_plan="Report the predeclared deterministic mock estimate.",
                stopping_rule="Stop after one execution per selected target.",
                hypothesis_specs=[
                    HypothesisExperimentSpec(
                        hypothesis_id=target_id,
                        unique_prediction=f"{target_id} produces its predeclared result.",
                        manipulation="Select the fixed synthetic condition.",
                        controls=["seed", "fixture", "metric"],
                        measurement="mock support score",
                        expected_pattern="The result is marked supported.",
                        rejection_condition="The result file is missing or unsupported.",
                    )
                    for target_id in target_ids
                ],
                trace_study_contract=(
                    TraceStudyContract.model_validate(payload["trace_study_contract"])
                    if payload.get("trace_study_contract")
                    else None
                ),
            )
        elif schema is ExperimentorOutput:
            target_id = payload["target_id"]
            experiment_id = f"EXP-{target_id}"
            if self.profile == ResearchProfile.TRACE_AUDIT:
                code = _trace_experiment_code(target_id)
            else:
                result = {
                    "target_id": target_id,
                    "primary_metric": "mock_support_score",
                    "higher_is_better": True,
                    "estimate": 0.20 if target_id == "A1" else 0.12,
                    "status": "supported",
                    "mock_only": True,
                }
                code = (
                    "import json\n\n"
                    f"result = {result!r}\n"
                    "with open('result.json', 'w', encoding='utf-8') as handle:\n"
                    "    json.dump(result, handle, sort_keys=True)\n"
                )
            value = ExperimentorOutput(
                hypothesis_id=target_id,
                experiment_id=experiment_id,
                files=[GeneratedFile(path="experiment.py", content=code)],
                entrypoint="experiment.py",
                expected_result_file="result.json",
                protocol_notes=["CPU-only deterministic pipeline smoke test."],
            )
        elif schema is EvidenceCriticReport:
            category_by_lens = {
                "construct": EvidenceConcernCategory.CONSTRUCT_VALIDITY,
                "data-population": EvidenceConcernCategory.DATA_PROVENANCE,
                "independence": EvidenceConcernCategory.EVALUATION_INDEPENDENCE,
                "circularity": EvidenceConcernCategory.METHOD_BENCHMARK_CIRCULARITY,
                "baseline-attribution": EvidenceConcernCategory.BASELINE_AND_ATTRIBUTION,
                "statistics": EvidenceConcernCategory.STATISTICAL_VALIDITY,
                "generalization": EvidenceConcernCategory.EXTERNAL_VALIDITY,
            }
            value = EvidenceCriticReport(
                critic_lens=payload["critic_lens"],
                questions=[
                    EvidenceQuestionDraft(
                        category=category_by_lens[payload["critic_lens"]],
                        target_ids=[payload["allowed_target_ids"][0]],
                        question="Does the supplied evidence meet this lens obligation?",
                        evidence_obligation=["The frozen contract and result must agree."],
                        why_material="A mismatch would invalidate the target evidence.",
                        proposed_severity=EvidenceConcernSeverity.MAJOR,
                    )
                ],
            )
        elif schema is EvidenceConcernResolution:
            value = EvidenceConcernResolution(
                concern_id=payload["concern"]["concern_id"],
                status=EvidenceResolutionStatus.SOLVED_BY_EXPERIMENT,
                severity=EvidenceConcernSeverity.MINOR,
                evidence_unit_ids=[payload["allowed_evidence_unit_ids"][0]],
                finding="The mock contract and result meet the stated obligation.",
                unresolved_gap="",
                recommended_action=WorkflowAction.PASS,
            )
        elif schema is ExEvaluatorReport:
            executions = payload["execution_results"]
            trace_id = executions[0]["result_ids"][0]
            value = ExEvaluatorReport(
                action=WorkflowAction.PASS,
                rubric_version="1.0",
                criteria=[
                    _criterion(name, trace_id)
                    for name in sorted(EX_EVALUATOR_CRITERIA)
                ],
                judgments=[
                    HypothesisResultJudgment(
                        hypothesis_id=item["hypothesis_id"],
                        status=(
                            ResearchResultStatus.INCONCLUSIVE
                            if self.profile == ResearchProfile.TRACE_AUDIT
                            else ResearchResultStatus.SUPPORTED
                        ),
                        rationale="The deterministic result exists and matches the fixture.",
                        result_ids=item["result_ids"],
                    )
                    for item in executions
                ],
                best_supported_hypothesis_id=(
                    None
                    if self.profile == ResearchProfile.TRACE_AUDIT
                    else executions[0]["hypothesis_id"]
                ),
                affected_hypothesis_ids=[],
                failure_notebook=[],
                contamination_by_experimentor=[
                    ExperimentorContamination(
                        hypothesis_id=item["hypothesis_id"],
                        status=ContaminationStatus.CLEAN,
                    )
                    for item in executions
                ],
                rationale="All selected mock executions passed structural checks.",
            )
        elif schema is PaperDraft:
            contract = payload["research_contract"]
            executions = payload["execution_results"]
            result_by_target = {
                item["hypothesis_id"]: item["result_ids"][0] for item in executions
            }
            target_by_id = {
                item["target_id"]: item for item in contract["targets"]
            }
            ledger_by_id = {
                item["claim_id"]: item for item in payload["claim_ledger"]["entries"]
            }
            linked_claims = [
                LinkedPaperClaim(
                    claim_id=target_id,
                    claim=(
                        ledger_by_id[target_id]["allowed_claim"]
                        if self.profile == ResearchProfile.TRACE_AUDIT
                        else (
                            f"The CPU-only mock execution for {target_id} completed "
                            "and returned the predeclared supported status."
                        )
                    ),
                    evidence_ids=target_by_id[target_id]["evidence_ids"],
                    result_ids=[result_by_target[target_id]],
                )
                for target_id in contract["selected_target_ids"]
            ]
            references = [
                PaperReference(
                    evidence_id=item["evidence_id"],
                    title=item["title"],
                    authors=item["authors"],
                    year=item["year"],
                    url=item["url"],
                )
                for item in contract["evidence"]
            ]
            verified_effects = "\n\n".join(
                item["effect_summary"]
                for item in payload["claim_ledger"]["entries"]
            )
            value = PaperDraft(
                research_mode=ResearchMode(contract["research_mode"]),
                research_profile=ResearchProfile(contract["research_profile"]),
                claim_ceiling=contract["claim_ceiling"],
                title="Mock Provenance-Gated Review Pipeline Study",
                abstract=(
                    "We test a complete AI-scientist pipeline using synthetic evidence. "
                    "Two dependency-ordered claims are executed locally on a CPU. "
                    "Both executions emit traceable results and pass the frozen gates. "
                    "These outcomes validate plumbing only and make no scientific claim."
                ),
                markdown=(
                    "# Mock Provenance-Gated Review Pipeline Study\n\n"
                    "## 1. Introduction\n\nThis document tests the tension between raw artifact access and cross-artifact validation.\n\n"
                    "## 2. Method\n\nTwo deterministic CPU-only fixtures executed blinded C0, C1, C2, and C3 conditions with hidden gold labels and leakage checks.\n\n"
                    "## 3. Results\n\nA1 and X1 emitted reproducible false-acceptance, clean-acceptance, and cost fields, but the smoke result is scientifically inconclusive.\n\n"
                    + verified_effects
                    + "\n\n"
                    "## 4. Limitations\n\nSynthetic outputs do not support real scientific conclusions."
                ),
                linked_claims=linked_claims,
                references=references,
                disclosed_negative_results=(
                    ["A1 and X1 are scientifically inconclusive in smoke mode."]
                    if self.profile == ResearchProfile.TRACE_AUDIT
                    else []
                ),
                limitations=[
                    "All evidence and results are synthetic.",
                    "This test checks continuity, not research merit.",
                ],
            )
        elif schema is ReviewReport:
            trace_id = payload["paper_claim_ids"][0]
            value = ReviewReport(
                action=WorkflowAction.ACCEPT,
                rubric_version="1.0",
                criteria=[
                    _criterion(name, trace_id) for name in sorted(REVIEW_CRITERIA)
                ],
                fatal_issues=[],
                non_fatal_issues=[],
                acceptance_conditions=[],
                contamination_status=ContaminationStatus.CLEAN,
                rationale="The mock draft is internally traceable and clearly caveated.",
            )
        else:
            raise AssertionError(
                f"Unexpected model schema {schema.__name__} in {session_label}"
            )
        return value  # type: ignore[return-value]


class OneRevisionPipelineProvider(ScriptedPipelineProvider):
    """Force one valid Evaluator-B rejection before normal recovery."""

    def __init__(self) -> None:
        super().__init__()
        self.methods_evaluator_calls = 0

    async def generate(
        self,
        schema: type[T],
        *,
        instructions: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        session_label: str,
    ) -> T:
        if schema is ClaimEvaluatorReport and "evaluator-b" in session_label:
            self.methods_evaluator_calls += 1
            if self.methods_evaluator_calls == 1:
                self.calls.append(session_label)
                return _methods_evaluator_revising_x1()  # type: ignore[return-value]
        return await super().generate(
            schema,
            instructions=instructions,
            payload=payload,
            tools=tools,
            session_label=session_label,
        )


def _settings(
    runs_dir: Path,
    *,
    hypothesis_rounds: int = 1,
    research_profile: str = "general",
) -> Settings:
    return Settings(
        runs_dir=runs_dir,
        model="mock-model",
        reasoning_effort="low",
        allow_code_execution=True,
        pipeline_smoke_test=True,
        dual_director_enabled=True,
        research_depth="competition",
        research_profile=research_profile,
        target_promoted_hypotheses=2,
        max_hypothesis_rounds=hypothesis_rounds,
        max_experiment_rounds=1,
        max_review_rounds=1,
        max_global_backtracks=0,
        max_component_repair_attempts=0,
        experiment_timeout_seconds=30,
        max_wall_clock_seconds=0,
        minimum_experiment_window_seconds=0,
        paper_reserve_seconds=0,
        finalization_reserve_seconds=0,
        watchdog_enabled=False,
    )


def test_full_orchestrator_mock_reaches_final_paper_without_gpu(tmp_path) -> None:
    provider = ScriptedPipelineProvider()
    settings = _settings(tmp_path / "runs")
    orchestrator = ResearchOrchestrator(
        settings,
        provider=provider,
        run_id="mock-e2e",
    )

    manifest = asyncio.run(orchestrator.run(QUESTION, objective=OBJECTIVE))

    assert manifest.status == RunStatus.SUCCESS
    assert manifest.final_stage == "FINAL"
    assert manifest.selected_target_ids == ["A1", "X1"]
    assert manifest.pipeline_smoke_test
    assert manifest.audit_report is None
    assert manifest.paper_markdown is not None
    assert manifest.paper_pdf is not None
    assert Path(manifest.paper_markdown).is_file()
    assert Path(manifest.paper_pdf).is_file()
    assert (tmp_path / "runs/mock-e2e/paper/paper.md").is_file()
    assert (tmp_path / "runs/mock-e2e/paper/paper.pdf").stat().st_size > 0
    assert sorted(
        label for label in provider.calls if label.startswith("experimentor-")
    ) == [
        "experimentor-A1-isolated-round-1",
        "experimentor-X1-isolated-round-1",
    ]
    assert provider.calls[-2:] == [
        "writer-round-1",
        "reviewer-isolated-round-1",
    ]
    for result_path in (
        tmp_path / "runs/mock-e2e/experiments/A1/round-1/result.json",
        tmp_path / "runs/mock-e2e/experiments/X1/round-1/result.json",
    ):
        assert json.loads(result_path.read_text(encoding="utf-8"))["mock_only"]


def test_full_orchestrator_recovers_from_claim_scoped_evaluator_b_revision(
    tmp_path,
) -> None:
    provider = OneRevisionPipelineProvider()
    orchestrator = ResearchOrchestrator(
        _settings(tmp_path / "runs", hypothesis_rounds=2),
        provider=provider,
        run_id="mock-e2e-revision",
    )

    manifest = asyncio.run(orchestrator.run(QUESTION, objective=OBJECTIVE))

    assert manifest.status == RunStatus.SUCCESS
    assert manifest.final_stage == "FINAL"
    assert provider.methods_evaluator_calls == 2
    assert sum(label.startswith("anchor-director-") for label in provider.calls) == 1
    assert sum(label.startswith("expansion-director-") for label in provider.calls) == 2
    assert any(
        label == "program-evaluator-b-round-1" for label in provider.calls
    )
    assert any(
        label == "program-evaluator-b-round-2" for label in provider.calls
    )
    assert Path(manifest.paper_markdown or "").is_file()
    assert Path(manifest.paper_pdf or "").is_file()


def test_trace_audit_profile_reaches_claim_ledger_and_final_paper_on_cpu(
    tmp_path,
) -> None:
    provider = ScriptedPipelineProvider(ResearchProfile.TRACE_AUDIT)
    orchestrator = ResearchOrchestrator(
        _settings(tmp_path / "runs", research_profile="trace_audit"),
        provider=provider,
        run_id="mock-trace-audit",
    )

    manifest = asyncio.run(orchestrator.run(QUESTION, objective=OBJECTIVE))

    assert manifest.status == RunStatus.SUCCESS
    assert manifest.final_stage == "FINAL"
    assert manifest.research_profile == ResearchProfile.TRACE_AUDIT
    assert manifest.selected_target_ids == ["A1", "X1"]
    assert any(item.startswith("trace-study-contract:") for item in manifest.artifact_ids)
    assert any(item.startswith("trace-benchmark-plan:") for item in manifest.artifact_ids)
    assert any(item.startswith("trace-corruption-plan:") for item in manifest.artifact_ids)
    assert any(item.startswith("trace-review-job-spec:") for item in manifest.artifact_ids)
    assert any(item.startswith("claim-ledger:") for item in manifest.artifact_ids)
    assert any(item.startswith("provenance-graph:") for item in manifest.artifact_ids)
    assert manifest.stale_artifact_ids == []
    assert manifest.unresolved_issues == []
    assert Path(manifest.paper_latex or "").is_file()
    assert Path(manifest.submission_metadata or "").is_file()
    assert Path(manifest.self_review or "").is_file()
    assert Path(manifest.format_audit or "").is_file()
    latex = Path(manifest.paper_latex or "").read_text(encoding="utf-8")
    assert "\\usepackage{icml2026}" in latex
    assert "\\author{" not in latex
    format_payload = json.loads(
        Path(manifest.format_audit or "").read_text(encoding="utf-8")
    )
    assert format_payload["anonymous"]
    assert format_payload["abstract_valid"]
    assert format_payload["main_body_within_limit"]
    assert format_payload["issues"] == []
    assert format_payload["pdf_backend"] in {
        "icml2026-latex",
        "reportlab-fallback",
    }
    for target_id in ("A1", "X1"):
        payload = json.loads(
            (
                tmp_path
                / f"runs/mock-trace-audit/experiments/{target_id}/round-1/result.json"
            ).read_text(encoding="utf-8")
        )
        assert payload["study_type"] == "TRACE_AUDIT"
        assert not payload["scientific_claim_valid"]
        assert {
            item["condition_id"] for item in payload["condition_metrics"]
        } == {
            "C0_PAPER_ONLY",
            "C1_RAW_ARTIFACTS",
            "C2_STRUCTURED_PROVENANCE",
            "C3_TRACE_GATE",
        }
    assert Path(manifest.paper_markdown or "").is_file()
    assert Path(manifest.paper_pdf or "").is_file()
