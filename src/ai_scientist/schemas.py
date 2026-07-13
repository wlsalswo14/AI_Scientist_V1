from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class VerificationStatus(StrEnum):
    FULL_TEXT_VERIFIED = "FULL_TEXT_VERIFIED"
    ABSTRACT_ONLY = "ABSTRACT_ONLY"
    SECONDARY_CITATION = "SECONDARY_CITATION"
    UNVERIFIED = "UNVERIFIED"


class SupportRelation(StrEnum):
    DIRECTLY_SUPPORTED = "DIRECTLY_SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    INFERRED = "INFERRED"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


class ResearchMode(StrEnum):
    DIRECT_TEST = "DIRECT_TEST"
    EXPLANATORY_RESEARCH = "EXPLANATORY_RESEARCH"
    BENCHMARK_AUDIT = "BENCHMARK_AUDIT"
    HYBRID_RESEARCH = "HYBRID_RESEARCH"


class ResearchReadiness(StrEnum):
    PROPOSED = "PROPOSED"
    TEST_READY = "TEST_READY"
    THEORY_READY = "THEORY_READY"
    AUDIT_READY = "AUDIT_READY"
    PROGRAM_READY = "PROGRAM_READY"


class ResearchTargetType(StrEnum):
    TEST_CLAIM = "TEST_CLAIM"
    MECHANISTIC_HYPOTHESIS = "MECHANISTIC_HYPOTHESIS"
    BENCHMARK_CLAIM = "BENCHMARK_CLAIM"
    BOUNDARY_CLAIM = "BOUNDARY_CLAIM"
    GENERALIZATION_CLAIM = "GENERALIZATION_CLAIM"
    THEORETICAL_CLAIM = "THEORETICAL_CLAIM"
    ENGINEERING_CLAIM = "ENGINEERING_CLAIM"


class ResearchDepth(StrEnum):
    QUICK = "QUICK"
    COMPETITION = "COMPETITION"
    THESIS = "THESIS"
    PUBLICATION = "PUBLICATION"


class ResearchProfile(StrEnum):
    GENERAL = "GENERAL"
    TRACE_AUDIT = "TRACE_AUDIT"


class DirectorRole(StrEnum):
    ANCHOR = "ANCHOR"
    EXPANSION = "EXPANSION"


class ProgramClaimType(StrEnum):
    EMPIRICAL = "EMPIRICAL"
    MECHANISTIC = "MECHANISTIC"
    BOUNDARY_CONDITION = "BOUNDARY_CONDITION"
    GENERALIZATION = "GENERALIZATION"
    THEORETICAL = "THEORETICAL"
    ENGINEERING = "ENGINEERING"
    BENCHMARK = "BENCHMARK"


class ClaimDependencyRelation(StrEnum):
    REQUIRES_SUPPORT = "REQUIRES_SUPPORT"
    REQUIRES_TEST = "REQUIRES_TEST"
    CAN_SURVIVE_NULL = "CAN_SURVIVE_NULL"
    CONTRADICTS = "CONTRADICTS"
    ALTERNATIVE_TO = "ALTERNATIVE_TO"
    GENERALIZES = "GENERALIZES"


class ClaimDependency(StrictModel):
    claim_id: str
    relation: ClaimDependencyRelation


class ResearchBrief(StrictModel):
    research_objective: str
    core_question: str
    research_depth: ResearchDepth
    research_profile: ResearchProfile = ResearchProfile.GENERAL
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    required_contributions: list[str] = Field(default_factory=list)


class ResearchResultStatus(StrEnum):
    BEST_SUPPORTED = "BEST_SUPPORTED"
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    FALSIFIED = "FALSIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"


class ResearchModeAssessment(StrictModel):
    original_question: str
    proposed_mode: ResearchMode
    classification_reason: str
    direct_testable_claim: str
    requires_competing_hypotheses: bool
    comparison_entities: list[str]
    primary_outcome: str
    claim_ceiling: str
    confidence: float = Field(ge=0, le=1)
    unresolved_ambiguities: list[str]
    research_depth: ResearchDepth = ResearchDepth.THESIS
    research_profile: ResearchProfile = ResearchProfile.GENERAL
    surface_mode: ResearchMode | None = None

    @model_validator(mode="after")
    def mode_is_internally_consistent(self) -> "ResearchModeAssessment":
        if self.proposed_mode == ResearchMode.EXPLANATORY_RESEARCH:
            if not self.requires_competing_hypotheses:
                raise ValueError("Explanatory research requires competing hypotheses")
        elif (
            self.proposed_mode
            in {ResearchMode.DIRECT_TEST, ResearchMode.BENCHMARK_AUDIT}
            and self.requires_competing_hypotheses
        ):
            raise ValueError(
                "DIRECT_TEST and BENCHMARK_AUDIT must not force competing hypotheses"
            )
        if self.proposed_mode in {
            ResearchMode.DIRECT_TEST,
            ResearchMode.HYBRID_RESEARCH,
        }:
            if not self.direct_testable_claim.strip():
                raise ValueError("DIRECT_TEST requires a direct testable claim")
            if not self.primary_outcome.strip():
                raise ValueError("DIRECT_TEST requires a primary outcome")
        return self


class EvidenceLocation(StrictModel):
    section: str = ""
    page: int | None = None
    paragraph: int | None = None
    sentence: int | None = None
    figure: str | None = None
    table: str | None = None
    equation: str | None = None


class EvidenceUnit(StrictModel):
    evidence_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str
    evidence_type: str
    location: EvidenceLocation
    verbatim_excerpt: str
    context_summary: str
    verification_status: VerificationStatus


class NearestWorkComparison(StrictModel):
    evidence_id: str
    answered_aspect: str
    unresolved_difference: str


class TraceTension(StrictModel):
    tension_id: str
    source_role: DirectorRole
    statement: str
    agreements: list[str] = Field(min_length=1)
    conflicts: list[str] = Field(min_length=1)
    unexplained_phenomenon: str
    alternative_explanations: list[str] = Field(min_length=1)
    importance: str
    why_now: str
    falsifiable_probe: str
    nearest_work: list[NearestWorkComparison] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ResearchClaimProposal(StrictModel):
    claim_id: str
    source_role: DirectorRole
    claim_type: ProgramClaimType
    statement: str
    null_statement: str
    rationale: str
    mechanism: str
    dependencies: list[ClaimDependency] = Field(default_factory=list)
    tension_ids: list[str] = Field(default_factory=list)
    distinctive_prediction: str
    falsification_condition: str
    alternative_explanations: list[str]
    positive_result_value: str
    negative_result_value: str
    null_result_value: str
    minimum_experiment: str
    required_data: str
    required_resources: list[str]
    compute_estimate: str
    uncertainties: list[str]
    evidence_ids: list[str]
    controlled_variables: list[str]
    manipulated_variables: list[str]
    measurement: str
    decision_threshold: str


class ClaimDirectorOutput(StrictModel):
    artifact_version: str
    director_role: DirectorRole
    research_objective: str
    core_question: str
    scope: str
    assumptions: list[str]
    evidence: list[EvidenceUnit]
    trace_tensions: list[TraceTension] = Field(default_factory=list)
    selected_trace_tension_ids: list[str] = Field(default_factory=list)
    claims: list[ResearchClaimProposal] = Field(min_length=1, max_length=6)
    search_limitations: list[str]

    @model_validator(mode="after")
    def claims_are_locally_consistent(self) -> "ClaimDirectorOutput":
        claim_ids = [item.claim_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Director claim IDs must be unique")
        evidence_ids = {item.evidence_id for item in self.evidence}
        tension_ids = {item.tension_id for item in self.trace_tensions}
        if len(tension_ids) != len(self.trace_tensions):
            raise ValueError("Director tension IDs must be unique")
        if not set(self.selected_trace_tension_ids).issubset(tension_ids):
            raise ValueError("selected_trace_tension_ids contains an unknown tension")
        for tension in self.trace_tensions:
            if tension.source_role != self.director_role:
                raise ValueError(
                    f"Tension {tension.tension_id} source role does not match its Director"
                )
            if not set(tension.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in tension {tension.tension_id}")
            if any(
                item.evidence_id not in evidence_ids for item in tension.nearest_work
            ):
                raise ValueError(
                    f"Unknown nearest-work evidence in tension {tension.tension_id}"
                )
        for claim in self.claims:
            if claim.source_role != self.director_role:
                raise ValueError(
                    f"Claim {claim.claim_id} source role does not match its Director"
                )
            if not set(claim.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in claim {claim.claim_id}")
            if not set(claim.tension_ids).issubset(tension_ids):
                raise ValueError(f"Unknown tension in claim {claim.claim_id}")
        return self


class ClaimUnit(StrictModel):
    claim_id: str
    text: str
    claim_type: str
    importance: str
    evidence_ids: list[str]
    support_relation: SupportRelation
    director_inference: bool
    inference_explanation: str = ""
    contradicting_evidence_ids: list[str] = Field(default_factory=list)


class DomainCandidate(StrictModel):
    domain: str
    problem: str
    research_value: str
    executable_experiment: str
    public_data: str
    feasibility_score: float = Field(ge=0, le=5)


class ResearchTension(StrictModel):
    tension_id: str
    statement: str
    agreements: list[str]
    conflicts: list[str]
    unexplained_phenomenon: str
    alternative_explanations: list[str]
    importance: str
    why_now: str
    evidence_ids: list[str]
    confidence: float = Field(ge=0, le=1)


class Hypothesis(StrictModel):
    hypothesis_id: str
    statement: str
    tension_id: str
    mechanism: str
    nearest_work_difference: str
    knowledge_change: str
    decision_change: str
    distinctive_prediction: str
    falsification_condition: str
    alternative_explanations: list[str]
    positive_result_value: str
    negative_result_value: str
    null_result_value: str
    minimum_experiment: str
    required_data: str
    compute_estimate: str
    uncertainties: list[str]
    evidence_ids: list[str]


class PredictionCell(StrictModel):
    hypothesis_id: str
    direction: str
    expected_pattern: str
    rejection_condition: str


class DiscriminatingCondition(StrictModel):
    condition_id: str
    description: str
    controlled_variables: list[str]
    manipulated_variables: list[str]
    measurement: str
    decision_threshold: str
    predictions: list[PredictionCell]


class DirectorOutput(StrictModel):
    artifact_version: str
    original_question: str
    selected_domain: str
    scope: str
    domain_candidates: list[DomainCandidate] = Field(min_length=1, max_length=3)
    evidence: list[EvidenceUnit]
    claims: list[ClaimUnit]
    tensions: list[ResearchTension] = Field(min_length=1, max_length=3)
    selected_tension_id: str
    hypotheses: list[Hypothesis] = Field(min_length=3, max_length=5)
    prediction_matrix: list[DiscriminatingCondition] = Field(min_length=1)
    search_limitations: list[str]

    @model_validator(mode="after")
    def references_are_locally_consistent(self) -> "DirectorOutput":
        evidence_ids = {item.evidence_id for item in self.evidence}
        tension_ids = {item.tension_id for item in self.tensions}
        hypothesis_ids = {item.hypothesis_id for item in self.hypotheses}
        if self.selected_tension_id not in tension_ids:
            raise ValueError("selected_tension_id does not exist")
        for claim in self.claims:
            if not set(claim.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in claim {claim.claim_id}")
        for hypothesis in self.hypotheses:
            if hypothesis.tension_id not in tension_ids:
                raise ValueError(f"Unknown tension in {hypothesis.hypothesis_id}")
            if not set(hypothesis.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in {hypothesis.hypothesis_id}")
        for condition in self.prediction_matrix:
            ids = {prediction.hypothesis_id for prediction in condition.predictions}
            if not ids.issubset(hypothesis_ids):
                raise ValueError(f"Unknown hypothesis in {condition.condition_id}")
        return self


class ResearchTarget(StrictModel):
    target_id: str
    target_type: ResearchTargetType
    statement: str
    null_statement: str
    rationale: str
    mechanism: str
    tension_ids: list[str] = Field(default_factory=list)
    distinctive_prediction: str
    falsification_condition: str
    alternative_explanations: list[str]
    positive_result_value: str
    negative_result_value: str
    null_result_value: str
    minimum_experiment: str
    required_data: str
    compute_estimate: str
    uncertainties: list[str]
    evidence_ids: list[str]


class ResearchPredictionCell(StrictModel):
    target_id: str
    direction: str
    expected_pattern: str
    rejection_condition: str


class ResearchCondition(StrictModel):
    condition_id: str
    description: str
    controlled_variables: list[str]
    manipulated_variables: list[str]
    measurement: str
    decision_threshold: str
    predictions: list[ResearchPredictionCell]


class TraceReviewCondition(StrEnum):
    PAPER_ONLY = "C0_PAPER_ONLY"
    RAW_ARTIFACTS = "C1_RAW_ARTIFACTS"
    STRUCTURED_PROVENANCE = "C2_STRUCTURED_PROVENANCE"
    TRACE_GATE = "C3_TRACE_GATE"


class TraceFaultType(StrEnum):
    RESULT_DIRECTION = "RESULT_DIRECTION"
    METRIC_CLAIM_MISMATCH = "METRIC_CLAIM_MISMATCH"
    STALE_ARTIFACT = "STALE_ARTIFACT"
    EXECUTION_HASH_MISMATCH = "EXECUTION_HASH_MISMATCH"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    NEGATIVE_RESULT_OMISSION = "NEGATIVE_RESULT_OMISSION"
    CLAIM_CEILING = "CLAIM_CEILING"
    CODE_INVARIANT = "CODE_INVARIANT"
    UNSUPPORTED_MECHANISM = "UNSUPPORTED_MECHANISM"
    CITATION_CLAIM_MISMATCH = "CITATION_CLAIM_MISMATCH"


class TraceConditionSpec(StrictModel):
    condition_id: TraceReviewCondition
    reviewer_inputs: list[str] = Field(min_length=1)
    structured_provenance: bool
    deterministic_gate: bool


class TraceGateRule(StrictModel):
    rule_id: str
    fault_type: TraceFaultType
    description: str
    required_artifacts: list[str] = Field(min_length=1)
    deterministic: bool = True


class TraceStudyContract(StrictModel):
    contract_version: str
    profile: ResearchProfile
    primary_metric: str
    clean_case_metric: str
    conditions: list[TraceConditionSpec] = Field(min_length=4, max_length=4)
    fault_types: list[TraceFaultType] = Field(min_length=4)
    gate_rules: list[TraceGateRule] = Field(min_length=4)
    paired_design: bool
    blinded_review: bool
    minimum_reviewer_families: int = Field(ge=1)
    reviewer_separation_policy: str
    benchmark_min_cases: int = Field(ge=1)
    gold_label_policy: str
    leakage_controls: list[str] = Field(min_length=1)
    primary_comparison: str
    secondary_comparisons: list[str] = Field(min_length=1)
    statistical_plan: str
    cost_metrics: list[str] = Field(min_length=1)
    stopping_rule: str
    claim_ids: list[str] = Field(min_length=1)


class TraceReviewDecision(StrictModel):
    case_id: str
    condition_id: TraceReviewCondition
    gold_faulty: bool
    gold_fault_type: TraceFaultType | None = None
    accepted: bool
    reviewer_model: str
    confidence: float = Field(ge=0, le=1)
    detected_fault_types: list[TraceFaultType] = Field(default_factory=list)
    latency_seconds: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class TraceConditionMetrics(StrictModel):
    condition_id: TraceReviewCondition
    false_acceptance_rate: float = Field(ge=0, le=1)
    clean_acceptance_rate: float = Field(ge=0, le=1)
    faulty_cases: int = Field(ge=0)
    clean_cases: int = Field(ge=0)
    mean_latency_seconds: float = Field(ge=0)
    mean_input_tokens: float = Field(ge=0)
    mean_output_tokens: float = Field(ge=0)


class TraceFaultConditionMetrics(StrictModel):
    fault_type: TraceFaultType
    condition_id: TraceReviewCondition
    false_acceptance_rate: float = Field(ge=0, le=1)
    decisions: int = Field(ge=1)


class TracePairedComparison(StrictModel):
    comparison_id: str
    treatment_id: TraceReviewCondition
    baseline_id: TraceReviewCondition
    false_acceptance_difference: float = Field(ge=-1, le=1)
    clean_acceptance_difference: float = Field(ge=-1, le=1)
    mean_latency_difference: float
    improvement_pairs: int = Field(ge=0)
    regression_pairs: int = Field(ge=0)
    mcnemar_exact_p_value: float = Field(ge=0, le=1)
    bootstrap_ci_low: float = Field(ge=-1, le=1)
    bootstrap_ci_high: float = Field(ge=-1, le=1)


class TraceAuditResultPayload(StrictModel):
    study_type: str
    study_mode: str
    scientific_claim_valid: bool
    analysis_target_id: str
    benchmark_case_count: int = Field(ge=1)
    corruption_manifest_hash: str
    leakage_check_passed: bool
    human_adjudication_minutes: float = Field(ge=0)
    decisions: list[TraceReviewDecision] = Field(min_length=1)
    condition_metrics: list[TraceConditionMetrics] = Field(min_length=4, max_length=4)
    fault_type_metrics: list[TraceFaultConditionMetrics] = Field(default_factory=list)
    paired_comparisons: list[TracePairedComparison] = Field(min_length=4, max_length=4)
    notes: list[str] = Field(default_factory=list)


class TraceReviewerDecisionBatch(StrictModel):
    batch_version: str
    trace_contract_fingerprint: str
    reviewer_models: list[str] = Field(min_length=1)
    corruption_manifest_hash: str
    leakage_check_passed: bool
    measurement_notes: list[str] = Field(default_factory=list)
    decisions: list[TraceReviewDecision] = Field(min_length=1)


class TraceBenchmarkSource(StrictModel):
    source_id: str
    source_kind: str
    location: str
    inclusion_reason: str
    identity_risk: str


class TraceBenchmarkPlan(StrictModel):
    plan_version: str
    trace_contract_fingerprint: str
    planned_case_count: int = Field(ge=1)
    sources: list[TraceBenchmarkSource] = Field(min_length=1)
    inclusion_criteria: list[str] = Field(min_length=1)
    exclusion_criteria: list[str] = Field(min_length=1)
    clean_case_definition: str
    paired_variant_policy: str
    pilot_policy: str
    main_split_frozen: bool
    gold_label_policy: str
    adjudication_policy: str


class TraceCorruptionRecipe(StrictModel):
    recipe_id: str
    fault_type: TraceFaultType
    precondition: str
    transformation: str
    expected_gold_label: str
    replay_check: str
    hidden_fields: list[str] = Field(min_length=1)


class TraceCorruptionPlan(StrictModel):
    plan_version: str
    trace_contract_fingerprint: str
    recipes: list[TraceCorruptionRecipe] = Field(min_length=4)
    manifest_fields: list[str] = Field(min_length=1)
    hidden_from_reviewer: bool
    deterministic_replay: bool
    leakage_test: str


class TracePreparationStageResult(StrictModel):
    trace_contract_fingerprint: str
    benchmark_plan: TraceBenchmarkPlan
    corruption_plan: TraceCorruptionPlan


class ResearchContract(StrictModel):
    contract_version: str
    original_question: str
    research_mode: ResearchMode
    research_profile: ResearchProfile = ResearchProfile.GENERAL
    readiness: ResearchReadiness
    selected_domain: str
    scope: str
    mode_rationale: str
    claim_ceiling: str
    evidence: list[EvidenceUnit]
    trace_tensions: list[TraceTension] = Field(default_factory=list)
    selected_trace_tension_ids: list[str] = Field(default_factory=list)
    claims: list[ClaimUnit]
    targets: list[ResearchTarget] = Field(min_length=1, max_length=5)
    selected_target_ids: list[str]
    prediction_matrix: list[ResearchCondition] = Field(min_length=1)
    trace_study_contract: TraceStudyContract | None = None
    search_limitations: list[str]

    @model_validator(mode="after")
    def contract_is_locally_consistent(self) -> "ResearchContract":
        evidence_ids = {item.evidence_id for item in self.evidence}
        tension_ids = {item.tension_id for item in self.trace_tensions}
        if not set(self.selected_trace_tension_ids).issubset(tension_ids):
            raise ValueError("Research Contract selects an unknown trace tension")
        target_ids = [item.target_id for item in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Research target IDs must be unique")
        if not set(self.selected_target_ids).issubset(target_ids):
            raise ValueError("selected_target_ids contains an unknown target")
        if self.readiness != ResearchReadiness.PROPOSED and not self.selected_target_ids:
            raise ValueError("A ready contract requires at least one selected target")
        for claim in self.claims:
            if not set(claim.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in claim {claim.claim_id}")
        for target in self.targets:
            if not set(target.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"Unknown evidence in target {target.target_id}")
            if not set(target.tension_ids).issubset(tension_ids):
                raise ValueError(f"Unknown tension in target {target.target_id}")
        for condition in self.prediction_matrix:
            prediction_ids = {item.target_id for item in condition.predictions}
            if not prediction_ids.issubset(target_ids):
                raise ValueError(
                    f"Unknown research target in condition {condition.condition_id}"
                )
        expected_type = {
            ResearchMode.DIRECT_TEST: ResearchTargetType.TEST_CLAIM,
            ResearchMode.EXPLANATORY_RESEARCH: (
                ResearchTargetType.MECHANISTIC_HYPOTHESIS
            ),
            ResearchMode.BENCHMARK_AUDIT: ResearchTargetType.BENCHMARK_CLAIM,
            ResearchMode.HYBRID_RESEARCH: None,
        }[self.research_mode]
        if expected_type is not None and any(
            item.target_type != expected_type for item in self.targets
        ):
            raise ValueError("Research target type does not match research mode")
        if self.research_mode == ResearchMode.EXPLANATORY_RESEARCH:
            if len(self.targets) < 3:
                raise ValueError("EXPLANATORY_RESEARCH requires 3-5 targets")
        elif self.research_mode != ResearchMode.HYBRID_RESEARCH and len(self.targets) > 3:
            raise ValueError("Direct tests and benchmark audits allow 1-3 targets")
        expected_readiness = {
            ResearchMode.DIRECT_TEST: ResearchReadiness.TEST_READY,
            ResearchMode.EXPLANATORY_RESEARCH: ResearchReadiness.THEORY_READY,
            ResearchMode.BENCHMARK_AUDIT: ResearchReadiness.AUDIT_READY,
            ResearchMode.HYBRID_RESEARCH: ResearchReadiness.PROGRAM_READY,
        }[self.research_mode]
        if self.readiness not in {ResearchReadiness.PROPOSED, expected_readiness}:
            raise ValueError("Readiness does not match research mode")
        if self.research_profile == ResearchProfile.TRACE_AUDIT:
            if not self.selected_trace_tension_ids:
                raise ValueError("TRACE_AUDIT requires a selected trace tension")
            if self.trace_study_contract is None:
                raise ValueError("TRACE_AUDIT requires a frozen Trace Study Contract")
            if self.trace_study_contract.profile != ResearchProfile.TRACE_AUDIT:
                raise ValueError("Trace Study Contract profile mismatch")
        return self


class CriterionScore(StrictModel):
    criterion: str
    score: int = Field(ge=0, le=5)
    evidence_ids: list[str]
    reason: str
    counterargument: str
    confidence: float = Field(ge=0, le=1)
    missing_information: list[str]
    fatal_issue: bool = False


class TargetGateScore(StrictModel):
    gate: str
    score: int = Field(ge=0, le=5)
    passed: bool
    evidence_ids: list[str]
    reason: str
    counterargument: str
    fatal_issue: bool = False


class EvaluationDecision(StrEnum):
    PROMOTE = "PROMOTE"
    REVISE = "REVISE"
    REJECT = "REJECT"


class ClaimErrorNote(StrictModel):
    claim_id: str
    source_role: DirectorRole
    failed_gates: list[str]
    counterexample: str
    failure_cause: str
    forbidden_revision: str
    required_revision: str
    preserve_claim_ids: list[str] = Field(default_factory=list)


class ClaimEvaluation(StrictModel):
    claim_id: str
    gates: list[TargetGateScore] = Field(min_length=1)
    fatal_issues: list[str]
    recommended_decision: EvaluationDecision


class ClaimEvaluatorReport(StrictModel):
    evaluator_role: str
    rubric_version: str
    artifact_version: str
    discovered_evidence: list[EvidenceUnit]
    claim_evaluations: list[ClaimEvaluation] = Field(min_length=1)
    error_notebook: list[ClaimErrorNote]
    overall_decision: EvaluationDecision
    rationale: str


class TargetEvaluation(StrictModel):
    target_id: str
    gates: list[TargetGateScore] = Field(min_length=1)
    fatal_issues: list[str]
    recommended_decision: EvaluationDecision


class EvaluatorReport(StrictModel):
    evaluator_role: str
    rubric_version: str
    artifact_version: str
    discovered_evidence: list[EvidenceUnit]
    criteria: list[CriterionScore] = Field(min_length=1)
    target_evaluations: list[TargetEvaluation] = Field(min_length=1)
    fatal_issues: list[str]
    concrete_counterexamples: list[str]
    recommended_decision: EvaluationDecision

    @computed_field
    @property
    def average_score(self) -> float:
        return sum(item.score for item in self.criteria) / len(self.criteria)


class WorkflowAction(StrEnum):
    PROMOTE = "PROMOTE"
    REVISE = "REVISE"
    REPLACE = "REPLACE"
    RESEARCH_AGAIN = "RESEARCH_AGAIN"
    RESELECT_TENSION = "RESELECT_TENSION"
    RECLASSIFY_MODE = "RECLASSIFY_MODE"
    PASS = "PASS"
    RERUN = "RERUN"
    REPAIR = "REPAIR"
    ADD_CONTROL = "ADD_CONTROL"
    RETURN_TO_ANALYSIS = "RETURN_TO_ANALYSIS"
    RETURN_TO_EXPERIMENT = "RETURN_TO_EXPERIMENT"
    RETURN_TO_HYPOTHESIS = "RETURN_TO_HYPOTHESIS"
    RETURN_TO_WRITER = "RETURN_TO_WRITER"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class ContaminationStatus(StrEnum):
    CLEAN = "CLEAN"
    VALID_DOWNGRADE = "VALID_DOWNGRADE"
    REGRESSION = "REGRESSION"
    CONTAMINATED = "CONTAMINATED"


class FailureNote(StrictModel):
    target_id: str
    original_claim: str
    counterexample: str
    conflict: str
    failure_cause: str
    forbidden_reasoning: str
    required_revision: str
    changed_prediction_required: str


class ComposerReport(StrictModel):
    action: WorkflowAction
    promoted_hypothesis_ids: list[str]
    agreed: list[str]
    unique_but_critical: list[str]
    disagreements: list[str]
    evidence_failures: list[str]
    failure_notebook: list[FailureNote]
    contamination_status: ContaminationStatus
    score_delta: float | None = None
    rationale: str


class ContractComposerReport(StrictModel):
    action: WorkflowAction
    promoted_target_ids: list[str]
    agreed: list[str]
    unique_but_critical: list[str]
    disagreements: list[str]
    evidence_failures: list[str]
    failure_notebook: list[FailureNote]
    contamination_status: ContaminationStatus
    score_delta: float | None = None
    rationale: str


class ResearchProgramStage(StrictModel):
    stage_number: int = Field(ge=1)
    name: str
    claim_ids: list[str] = Field(min_length=1)
    purpose: str
    entry_condition: str
    completion_gate: str


class ResearchProgramComposition(StrictModel):
    action: WorkflowAction
    integrated_claim_ids: list[str]
    deferred_claim_ids: list[str]
    selected_trace_tension_ids: list[str] = Field(default_factory=list)
    stages: list[ResearchProgramStage]
    scope: str
    mode_rationale: str
    claim_ceiling: str
    failure_notebook: list[ClaimErrorNote]
    rationale: str


class HypothesisStageResult(StrictModel):
    director_output: DirectorOutput
    evaluator_a: EvaluatorReport
    evaluator_b: EvaluatorReport
    composer: ComposerReport
    round_number: int


class ResearchStageResult(StrictModel):
    mode_assessment: ResearchModeAssessment
    contract: ResearchContract
    source_stage: str
    round_number: int
    research_brief: ResearchBrief | None = None
    anchor_output: ClaimDirectorOutput | None = None
    expansion_output: ClaimDirectorOutput | None = None
    claim_evaluator_a: ClaimEvaluatorReport | None = None
    claim_evaluator_b: ClaimEvaluatorReport | None = None
    program_composition: ResearchProgramComposition | None = None


class HypothesisExperimentSpec(StrictModel):
    hypothesis_id: str
    unique_prediction: str
    manipulation: str
    controls: list[str]
    measurement: str
    expected_pattern: str
    rejection_condition: str


class ExperimentContract(StrictModel):
    contract_version: str
    hypothesis_ids: list[str]
    dataset_plan: str
    shared_protocol: list[str]
    metrics: list[str]
    seeds: list[int]
    statistical_plan: str
    stopping_rule: str
    hypothesis_specs: list[HypothesisExperimentSpec]
    trace_study_contract: TraceStudyContract | None = None


class GeneratedFile(StrictModel):
    path: str
    content: str


class ExperimentorOutput(StrictModel):
    hypothesis_id: str
    experiment_id: str
    files: list[GeneratedFile] = Field(min_length=1)
    entrypoint: str
    expected_result_file: str = "result.json"
    protocol_notes: list[str]


class ExecutionResult(StrictModel):
    hypothesis_id: str
    experiment_id: str
    exit_code: int
    timed_out: bool = False
    stdout: str
    stderr: str
    output_files: dict[str, str]
    result_ids: list[str]
    code_hash: str
    workspace: str


class HypothesisResultJudgment(StrictModel):
    hypothesis_id: str
    status: ResearchResultStatus
    rationale: str
    result_ids: list[str]


class ExperimentorContamination(StrictModel):
    hypothesis_id: str
    status: ContaminationStatus


class EvidenceConcernCategory(StrEnum):
    CONSTRUCT_VALIDITY = "CONSTRUCT_VALIDITY"
    TARGET_EVIDENCE_ALIGNMENT = "TARGET_EVIDENCE_ALIGNMENT"
    DATA_PROVENANCE = "DATA_PROVENANCE"
    EVALUATION_INDEPENDENCE = "EVALUATION_INDEPENDENCE"
    METHOD_BENCHMARK_CIRCULARITY = "METHOD_BENCHMARK_CIRCULARITY"
    GOLD_LABEL_CREDIBILITY = "GOLD_LABEL_CREDIBILITY"
    BASELINE_AND_ATTRIBUTION = "BASELINE_AND_ATTRIBUTION"
    STATISTICAL_VALIDITY = "STATISTICAL_VALIDITY"
    EXTERNAL_VALIDITY = "EXTERNAL_VALIDITY"
    CLAIM_SCOPE = "CLAIM_SCOPE"


class EvidenceConcernSeverity(StrEnum):
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    FATAL = "FATAL"


class EvidenceResolutionStatus(StrEnum):
    SOLVED_BY_EXPERIMENT = "SOLVED_BY_EXPERIMENT"
    SOLVED_BY_EXTERNAL_EVIDENCE = "SOLVED_BY_EXTERNAL_EVIDENCE"
    PROMOTED = "PROMOTED"
    INVALID = "INVALID"
    DUPLICATE = "DUPLICATE"


class EvidenceAuditUnit(StrictModel):
    unit_id: str
    unit_type: str
    target_ids: list[str]
    content: dict[str, Any]


class EvidenceAuditManifest(StrictModel):
    manifest_version: str
    units: list[EvidenceAuditUnit] = Field(min_length=1)


class EvidenceQuestionDraft(StrictModel):
    category: EvidenceConcernCategory
    target_ids: list[str] = Field(min_length=1)
    question: str
    evidence_obligation: list[str] = Field(min_length=1)
    why_material: str
    proposed_severity: EvidenceConcernSeverity


class EvidenceCriticReport(StrictModel):
    critic_lens: str
    questions: list[EvidenceQuestionDraft] = Field(min_length=1, max_length=4)


class EvidenceConcern(StrictModel):
    concern_id: str
    critic_lens: str
    category: EvidenceConcernCategory
    target_ids: list[str] = Field(min_length=1)
    question: str
    evidence_obligation: list[str] = Field(min_length=1)
    why_material: str
    proposed_severity: EvidenceConcernSeverity


class EvidenceConcernResolution(StrictModel):
    concern_id: str
    status: EvidenceResolutionStatus
    severity: EvidenceConcernSeverity
    evidence_unit_ids: list[str]
    finding: str
    unresolved_gap: str
    recommended_action: WorkflowAction


class EvidenceConcernDiscard(StrictModel):
    concern_id: str
    reason: str
    canonical_id: str | None = None


class EvidenceGlobalAuditReport(StrictModel):
    kept_concern_ids: list[str]
    discarded: list[EvidenceConcernDiscard]
    rationale: str


class EvidenceAuditOutcome(StrictModel):
    manifest: EvidenceAuditManifest
    critic_reports: list[EvidenceCriticReport]
    concerns: list[EvidenceConcern]
    resolutions: list[EvidenceConcernResolution]
    global_audit: EvidenceGlobalAuditReport
    unresolved_major_ids: list[str]
    unresolved_fatal_ids: list[str]
    recommended_action: WorkflowAction
    paper_eligible: bool
    complete: bool


class ExEvaluatorReport(StrictModel):
    action: WorkflowAction
    rubric_version: str
    criteria: list[CriterionScore]
    judgments: list[HypothesisResultJudgment]
    best_supported_hypothesis_id: str | None = None
    affected_hypothesis_ids: list[str]
    failure_notebook: list[FailureNote]
    contamination_by_experimentor: list[ExperimentorContamination]
    rationale: str

    @computed_field
    @property
    def average_score(self) -> float:
        if not self.criteria:
            return 0.0
        return sum(item.score for item in self.criteria) / len(self.criteria)


class ExperimentStageResult(StrictModel):
    contract: ExperimentContract
    experimentor_outputs: list[ExperimentorOutput]
    executions: list[ExecutionResult]
    evaluation: ExEvaluatorReport
    trace_preparation: TracePreparationStageResult | None = None
    trace_reviewer_decisions: TraceReviewerDecisionBatch | None = None
    evidence_audit: EvidenceAuditOutcome | None = None
    round_number: int
    passed: bool = False
    failure_reasons: list[str] = Field(default_factory=list)


class ClaimLedgerEntry(StrictModel):
    claim_id: str
    target_id: str
    status: ResearchResultStatus
    allowed_claim: str
    effect_summary: str
    forbidden_generalizations: list[str] = Field(min_length=1)
    evidence_ids: list[str]
    result_ids: list[str] = Field(min_length=1)


class ClaimLedger(StrictModel):
    ledger_version: str
    research_profile: ResearchProfile
    claim_ceiling: str
    entries: list[ClaimLedgerEntry] = Field(min_length=1)


class ProvenanceNode(StrictModel):
    node_id: str
    node_type: str
    status: str
    content_hash: str


class ProvenanceEdge(StrictModel):
    source_id: str
    target_id: str
    relation: str


class ProvenanceGraph(StrictModel):
    graph_version: str
    run_id: str
    root_claim_ids: list[str] = Field(min_length=1)
    nodes: list[ProvenanceNode] = Field(min_length=1)
    edges: list[ProvenanceEdge] = Field(min_length=1)


class LinkedPaperClaim(StrictModel):
    claim_id: str
    claim: str
    evidence_ids: list[str]
    result_ids: list[str]


class PaperReference(StrictModel):
    evidence_id: str
    title: str
    authors: list[str]
    year: int | None = None
    url: str


class PaperDraft(StrictModel):
    research_mode: ResearchMode
    research_profile: ResearchProfile = ResearchProfile.GENERAL
    claim_ceiling: str
    title: str
    abstract: str
    markdown: str
    linked_claims: list[LinkedPaperClaim]
    references: list[PaperReference] = Field(default_factory=list)
    disclosed_negative_results: list[str]
    limitations: list[str]


class ReviewIssue(StrictModel):
    issue: str
    evidence: list[str]
    root_cause_stage: str
    severity: str
    required_fix: str


class ReviewReport(StrictModel):
    action: WorkflowAction
    rubric_version: str
    criteria: list[CriterionScore]
    fatal_issues: list[ReviewIssue]
    non_fatal_issues: list[ReviewIssue]
    acceptance_conditions: list[str]
    contamination_status: ContaminationStatus
    score_delta: float | None = None
    rationale: str

    @computed_field
    @property
    def average_score(self) -> float:
        if not self.criteria:
            return 0.0
        return sum(item.score for item in self.criteria) / len(self.criteria)


class PaperStageResult(StrictModel):
    draft: PaperDraft
    review: ReviewReport
    round_number: int
    accepted: bool = False
    failure_reasons: list[str] = Field(default_factory=list)


class SubmissionFormatAudit(StrictModel):
    anonymous: bool
    abstract_sentence_count: int
    abstract_valid: bool
    main_body_pages: int | None = None
    main_body_within_limit: bool
    references_excluded: bool
    icml_latex_source: bool
    official_pdf_compiled: bool
    pdf_backend: str
    self_review_present: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunStatus(StrEnum):
    SUCCESS = "SUCCESS"
    INCONCLUSIVE = "INCONCLUSIVE"
    NEGATIVE_RESULT = "NEGATIVE_RESULT"
    PARTIAL_COMPLETION = "PARTIAL_COMPLETION"
    FAILED_WITH_AUDIT = "FAILED_WITH_AUDIT"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class FinalManifest(StrictModel):
    run_id: str
    question: str
    research_objective: str = ""
    research_depth: ResearchDepth = ResearchDepth.THESIS
    research_profile: ResearchProfile = ResearchProfile.GENERAL
    status: RunStatus
    model: str
    reasoning_effort: str
    pipeline_smoke_test: bool = False
    research_mode: ResearchMode | None = None
    research_readiness: ResearchReadiness | None = None
    selected_target_ids: list[str] = Field(default_factory=list)
    final_stage: str
    paper_markdown: str | None = None
    paper_pdf: str | None = None
    paper_latex: str | None = None
    submission_metadata: str | None = None
    self_review: str | None = None
    format_audit: str | None = None
    unaccepted_draft: str | None = None
    audit_report: str | None = None
    artifact_ids: list[str]
    valid_artifact_ids: list[str] = Field(default_factory=list)
    stale_artifact_ids: list[str] = Field(default_factory=list)
    unresolved_issues: list[str]
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
