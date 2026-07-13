from __future__ import annotations

import asyncio

from ai_scientist.program_validation import (
    EVALUATOR_A_GATES,
    EVALUATOR_B_GATES,
    build_research_contract_from_program,
    compute_claim_promotions,
    filter_promotions_by_dependencies,
    validate_claim_dependencies,
    validate_claim_director,
    validate_claim_evaluator,
    validate_program_composition,
)
from ai_scientist.artifacts import ArtifactStore
from ai_scientist.config import Settings
from ai_scientist.schemas import (
    ClaimDependency,
    ClaimDependencyRelation,
    ClaimDirectorOutput,
    ClaimErrorNote,
    ClaimEvaluation,
    ClaimEvaluatorReport,
    DirectorRole,
    EvaluationDecision,
    EvidenceLocation,
    EvidenceUnit,
    ProgramClaimType,
    ResearchBrief,
    ResearchClaimProposal,
    ResearchDepth,
    ResearchMode,
    ResearchModeAssessment,
    ResearchProgramComposition,
    ResearchProgramStage,
    ResearchReadiness,
    TargetGateScore,
    VerificationStatus,
    WorkflowAction,
)
from ai_scientist.validation import validate_research_contract
from ai_scientist.workflows.research_program import DualDirectorResearchWorkflow


class FakeAgent:
    def __init__(self, outputs) -> None:
        self.outputs = list(outputs)
        self.payloads = []

    async def run(self, payload, *, session_label):
        self.payloads.append((payload, session_label))
        if not self.outputs:
            raise AssertionError(f"Unexpected model call: {session_label}")
        return self.outputs.pop(0)


def evidence(evidence_id: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        title=f"Source {evidence_id}",
        authors=["A. Author"],
        year=2026,
        url=f"https://example.org/{evidence_id}",
        evidence_type="paper",
        location=EvidenceLocation(section="Results"),
        verbatim_excerpt="The measured effect varies under the tested condition.",
        context_summary="A source-located result.",
        verification_status=VerificationStatus.FULL_TEXT_VERIFIED,
    )


def claim(
    claim_id: str,
    role: DirectorRole,
    claim_type: ProgramClaimType,
    evidence_id: str,
    *,
    dependencies: list[ClaimDependency] | None = None,
) -> ResearchClaimProposal:
    return ResearchClaimProposal(
        claim_id=claim_id,
        source_role=role,
        claim_type=claim_type,
        statement=f"Claim {claim_id} changes the measured outcome.",
        null_statement=f"Claim {claim_id} does not change the measured outcome.",
        rationale="A falsifiable contribution.",
        mechanism="A candidate mechanism" if role == DirectorRole.EXPANSION else "",
        dependencies=dependencies or [],
        distinctive_prediction=f"{claim_id} predicts a unique directional change.",
        falsification_condition="Reject when the paired 95% interval includes 0.",
        alternative_explanations=["Measurement noise"],
        positive_result_value="Supports the bounded claim.",
        negative_result_value="Rejects the bounded claim.",
        null_result_value="Constrains the effect size.",
        minimum_experiment="Run a paired controlled experiment over 5 seeds.",
        required_data="A fixed public split.",
        required_resources=["CPU"],
        compute_estimate="One hour.",
        uncertainties=["Seed variance"],
        evidence_ids=[evidence_id],
        controlled_variables=["data", "seed"],
        manipulated_variables=["treatment"],
        measurement="paired outcome difference",
        decision_threshold="95% interval excludes 0 and effect exceeds 0.01",
    )


def brief(depth: ResearchDepth = ResearchDepth.THESIS) -> ResearchBrief:
    return ResearchBrief(
        research_objective="Explain and test a bounded effect.",
        core_question="Does treatment B improve outcome Y over A?",
        research_depth=depth,
        required_contributions=["EMPIRICAL_ANCHOR", "MECHANISTIC_EXTENSION"],
    )


def director_outputs() -> tuple[ClaimDirectorOutput, ClaimDirectorOutput]:
    anchor = ClaimDirectorOutput(
        artifact_version="1.0",
        director_role=DirectorRole.ANCHOR,
        research_objective=brief().research_objective,
        core_question=brief().core_question,
        scope="A bounded paired comparison.",
        assumptions=[],
        evidence=[evidence("E1")],
        claims=[claim("A1", DirectorRole.ANCHOR, ProgramClaimType.EMPIRICAL, "E1")],
        search_limitations=[],
    )
    expansion = ClaimDirectorOutput(
        artifact_version="1.0",
        director_role=DirectorRole.EXPANSION,
        research_objective=brief().research_objective,
        core_question=brief().core_question,
        scope="A discriminating mechanism extension.",
        assumptions=[],
        evidence=[evidence("E2")],
        claims=[
            claim(
                "X1",
                DirectorRole.EXPANSION,
                ProgramClaimType.MECHANISTIC,
                "E2",
                dependencies=[
                    ClaimDependency(
                        claim_id="A1",
                        relation=ClaimDependencyRelation.REQUIRES_TEST,
                    )
                ],
            )
        ],
        search_limitations=[],
    )
    return anchor, expansion


def test_trace_primary_anchor_is_revised_when_only_auxiliary_anchor_is_locked() -> None:
    anchor, _ = director_outputs()
    anchor = anchor.model_copy(
        update={
            "claims": [
                *anchor.claims,
                claim("A2", DirectorRole.ANCHOR, ProgramClaimType.ENGINEERING, "E1"),
            ]
        }
    )
    note = ClaimErrorNote(
        claim_id="A1",
        source_role=DirectorRole.ANCHOR,
        failed_gates=["Feasibility"],
        counterexample="The primary design is underpowered.",
        failure_cause="The primary claim needs repair.",
        forbidden_revision="Do not substitute an auxiliary claim.",
        required_revision="Repair A1 while preserving A2.",
        preserve_claim_ids=["A2"],
    )

    assert DualDirectorResearchWorkflow._needs_role_revision(
        anchor,
        {"A2"},
        [note],
        required_promoted_count=1,
        required_claim_ids={"A1"},
    )


def test_error_notebook_accumulates_across_rounds_and_drops_locked_claims() -> None:
    first = ClaimErrorNote(
        claim_id="X1",
        source_role=DirectorRole.EXPANSION,
        failed_gates=["Evidence Support"],
        counterexample="The source does not entail the threshold.",
        failure_cause="Threshold is unsupported.",
        forbidden_revision="Do not invent evidence.",
        required_revision="Reframe the claim as exploratory.",
        preserve_claim_ids=["A1"],
    )
    second = first.model_copy(
        update={
            "failed_gates": ["Discriminating Power"],
            "counterexample": "The null can pass the current rule.",
            "failure_cause": "The decision rule is permissive.",
            "required_revision": "Add a corrected paired test.",
        }
    )

    accumulated = DualDirectorResearchWorkflow._accumulate_error_notes(
        [first], [second], set()
    )

    assert len(accumulated) == 1
    assert accumulated[0].failed_gates == [
        "Evidence Support",
        "Discriminating Power",
    ]
    assert "Reframe the claim as exploratory." in accumulated[0].required_revision
    assert "Add a corrected paired test." in accumulated[0].required_revision
    assert DualDirectorResearchWorkflow._accumulate_error_notes(
        accumulated, [], {"X1"}
    ) == []


def evaluator(
    role: str,
    gate_names: set[str],
    *,
    failed_claim: str | None = None,
    claim_rows: tuple[tuple[str, DirectorRole, str], ...] | None = None,
) -> ClaimEvaluatorReport:
    evaluations = []
    notes = []
    for claim_id, source_role, evidence_id in claim_rows or (
        ("A1", DirectorRole.ANCHOR, "E1"),
        ("X1", DirectorRole.EXPANSION, "E2"),
    ):
        failed = claim_id == failed_claim
        gates = [
            TargetGateScore(
                gate=name,
                score=2 if failed and index == 0 else 4,
                passed=not (failed and index == 0),
                evidence_ids=[evidence_id],
                reason="Claim-scoped assessment.",
                counterargument="A bounded counterargument.",
                fatal_issue=False,
            )
            for index, name in enumerate(sorted(gate_names))
        ]
        evaluations.append(
            ClaimEvaluation(
                claim_id=claim_id,
                gates=gates,
                fatal_issues=[],
                recommended_decision=(
                    EvaluationDecision.REVISE
                    if failed
                    else EvaluationDecision.PROMOTE
                ),
            )
        )
        if failed:
            notes.append(
                ClaimErrorNote(
                    claim_id=claim_id,
                    source_role=source_role,
                    failed_gates=[gates[0].gate],
                    counterexample="The current prediction does not separate the null.",
                    failure_cause="Insufficient discrimination.",
                    forbidden_revision="Do not make a cosmetic wording change.",
                    required_revision="Add a condition with an opposing prediction.",
                    preserve_claim_ids=["A1"] if claim_id == "X1" else [],
                )
            )
    return ClaimEvaluatorReport(
        evaluator_role=role,
        rubric_version="1.0",
        artifact_version="1.0",
        discovered_evidence=[],
        claim_evaluations=evaluations,
        error_notebook=notes,
        overall_decision=(
            EvaluationDecision.REVISE
            if failed_claim
            else EvaluationDecision.PROMOTE
        ),
        rationale="Independent claim-level evaluation.",
    )


def composition() -> ResearchProgramComposition:
    return ResearchProgramComposition(
        action=WorkflowAction.PROMOTE,
        integrated_claim_ids=["A1", "X1"],
        deferred_claim_ids=[],
        stages=[
            ResearchProgramStage(
                stage_number=1,
                name="Establish anchor",
                claim_ids=["A1"],
                purpose="Test the primary effect.",
                entry_condition="Research program approved.",
                completion_gate="A1 receives a valid result.",
            ),
            ResearchProgramStage(
                stage_number=2,
                name="Discriminate mechanism",
                claim_ids=["X1"],
                purpose="Test the explanatory extension.",
                entry_condition="A1 has been tested.",
                completion_gate="X1 alternatives are discriminated.",
            ),
        ],
        scope="A bounded empirical and mechanistic program.",
        mode_rationale="The direct anchor is paired with one explanatory extension.",
        claim_ceiling="Empirical and mechanistic claims only in the tested scope.",
        failure_notebook=[],
        rationale="Both independently promoted claims form a coherent program.",
    )


def assessment() -> ResearchModeAssessment:
    return ResearchModeAssessment(
        original_question=brief().core_question,
        proposed_mode=ResearchMode.HYBRID_RESEARCH,
        classification_reason="The thesis-depth objective requires an extension.",
        direct_testable_claim="B improves Y over A.",
        requires_competing_hypotheses=True,
        comparison_entities=["A", "B"],
        primary_outcome="Y",
        claim_ceiling="Evidence-gated research program.",
        confidence=0.9,
        unresolved_ambiguities=[],
        research_depth=ResearchDepth.THESIS,
        surface_mode=ResearchMode.DIRECT_TEST,
    )


def test_director_and_evaluator_claim_gates_pass() -> None:
    anchor, expansion = director_outputs()
    report = validate_claim_director(
        anchor,
        brief=brief(),
        role=DirectorRole.ANCHOR,
    )
    assert report.valid
    assert validate_claim_dependencies(anchor, expansion).valid

    evaluation_a = evaluator("literature", EVALUATOR_A_GATES)
    validation = validate_claim_evaluator(
        evaluation_a,
        claim_ids={"A1", "X1"},
        required_gates=EVALUATOR_A_GATES,
        evidence_ids={"E1", "E2"},
        rubric_version="1.0",
        minimum_passing_score=3,
    )
    assert validation.valid


def test_promotion_requires_both_evaluators_and_returns_individual_note() -> None:
    evaluation_a = evaluator("literature", EVALUATOR_A_GATES)
    evaluation_b = evaluator("methods", EVALUATOR_B_GATES, failed_claim="X1")

    decision = compute_claim_promotions(evaluation_a, evaluation_b)

    assert decision.promoted_ids == frozenset({"A1"})
    assert decision.failed_ids == frozenset({"X1"})
    assert evaluation_b.error_notebook[0].claim_id == "X1"
    assert evaluation_b.error_notebook[0].source_role == DirectorRole.EXPANSION


def test_locked_claim_cannot_be_downgraded_without_reopening_program() -> None:
    evaluation = evaluator(
        "methods",
        EVALUATOR_B_GATES,
        failed_claim="A1",
    )

    validation = validate_claim_evaluator(
        evaluation,
        claim_ids={"A1", "X1"},
        required_gates=EVALUATOR_B_GATES,
        evidence_ids={"E1", "E2"},
        rubric_version="1.0",
        minimum_passing_score=3,
        locked_claim_ids={"A1"},
    )

    assert not validation.valid
    assert any(
        issue.code == "LOCKED_CLAIM_DOWNGRADED"
        for issue in validation.issues
    )


def test_hybrid_mode_does_not_force_mechanistic_competition() -> None:
    value = assessment().model_copy(
        update={"requires_competing_hypotheses": False}
    )

    validated = ResearchModeAssessment.model_validate(
        value.model_dump(mode="json")
    )

    assert validated.proposed_mode == ResearchMode.HYBRID_RESEARCH
    assert not validated.requires_competing_hypotheses


def test_dependency_blocks_child_when_anchor_does_not_pass() -> None:
    anchor, expansion = director_outputs()
    claims = {item.claim_id: item for item in [*anchor.claims, *expansion.claims]}

    eligible = filter_promotions_by_dependencies(claims, {"X1"})

    assert eligible == set()


def test_unanchored_expansion_claim_is_rejected() -> None:
    anchor, expansion = director_outputs()
    expansion = expansion.model_copy(
        update={
            "claims": [
                expansion.claims[0].model_copy(update={"dependencies": []})
            ]
        }
    )

    validation = validate_claim_dependencies(anchor, expansion)

    assert not validation.valid
    assert any(
        issue.code == "UNANCHORED_EXPANSION_CLAIM"
        for issue in validation.issues
    )


def test_anchor_cannot_depend_on_expansion_claim() -> None:
    anchor, expansion = director_outputs()
    anchor = anchor.model_copy(
        update={
            "claims": [
                anchor.claims[0].model_copy(
                    update={
                        "dependencies": [
                            ClaimDependency(
                                claim_id="X1",
                                relation=ClaimDependencyRelation.REQUIRES_TEST,
                            )
                        ]
                    }
                )
            ]
        }
    )

    validation = validate_claim_dependencies(anchor, expansion)

    assert not validation.valid
    assert any(
        issue.code == "ANCHOR_DEPENDS_ON_EXPANSION"
        for issue in validation.issues
    )


def test_program_composer_cannot_revive_unpromoted_claim() -> None:
    anchor, expansion = director_outputs()
    value = composition()
    validation = validate_program_composition(
        value,
        all_claim_ids={"A1", "X1"},
        promoted_claim_ids={"A1"},
        anchor_claim_ids={"A1"},
        expansion_claim_ids={"X1"},
        claim_dependencies={
            item.claim_id: item.dependencies
            for item in [*anchor.claims, *expansion.claims]
        },
        brief=brief(),
        max_integrated_claims=2,
    )
    assert not validation.valid
    assert any(
        issue.code == "UNPROMOTED_CLAIM_IN_PROGRAM"
        for issue in validation.issues
    )


def test_program_builds_hybrid_contract_from_promoted_claims() -> None:
    anchor, expansion = director_outputs()
    evaluation_a = evaluator("literature", EVALUATOR_A_GATES)
    evaluation_b = evaluator("methods", EVALUATOR_B_GATES)
    value = composition()
    validation = validate_program_composition(
        value,
        all_claim_ids={"A1", "X1"},
        promoted_claim_ids={"A1", "X1"},
        anchor_claim_ids={"A1"},
        expansion_claim_ids={"X1"},
        claim_dependencies={
            item.claim_id: item.dependencies
            for item in [*anchor.claims, *expansion.claims]
        },
        brief=brief(),
        max_integrated_claims=2,
    )
    assert validation.valid

    contract = build_research_contract_from_program(
        brief=brief(),
        assessment=assessment(),
        anchor=anchor,
        expansion=expansion,
        evaluator_a=evaluation_a,
        evaluator_b=evaluation_b,
        composition=value,
    )

    assert contract.research_mode == ResearchMode.HYBRID_RESEARCH
    assert contract.readiness == ResearchReadiness.PROGRAM_READY
    assert contract.selected_target_ids == ["A1", "X1"]
    assert validate_research_contract(
        contract,
        expected_mode=ResearchMode.HYBRID_RESEARCH,
    ).valid


def test_workflow_revises_only_failed_director_and_passes_error_note(
    tmp_path,
) -> None:
    anchor, expansion = director_outputs()
    revised_expansion = expansion.model_copy(
        update={"scope": "A repaired discriminating mechanism extension."}
    )
    anchor_agent = FakeAgent([anchor])
    expansion_agent = FakeAgent([expansion, revised_expansion])
    evaluator_a_agent = FakeAgent(
        [
            evaluator("literature", EVALUATOR_A_GATES),
            evaluator("literature", EVALUATOR_A_GATES),
        ]
    )
    evaluator_b_agent = FakeAgent(
        [
            evaluator("methods", EVALUATOR_B_GATES, failed_claim="X1"),
            evaluator("methods", EVALUATOR_B_GATES),
        ]
    )
    composer_agent = FakeAgent([composition()])
    workflow = DualDirectorResearchWorkflow(
        Settings(
            max_hypothesis_rounds=2,
            target_promoted_hypotheses=2,
            max_component_repair_attempts=0,
        ),
        ArtifactStore(tmp_path, "run-dual"),
        anchor_agent,
        expansion_agent,
        evaluator_a_agent,
        evaluator_b_agent,
        composer_agent,
    )

    result = asyncio.run(workflow.run(brief(), assessment()))

    assert result.contract.research_mode == ResearchMode.HYBRID_RESEARCH
    assert len(anchor_agent.payloads) == 1
    assert len(expansion_agent.payloads) == 2
    second_expansion_payload = expansion_agent.payloads[1][0]
    assert second_expansion_payload["individual_error_notebook"][0]["claim_id"] == "X1"
    assert second_expansion_payload["locked_claims"] == []


def test_publication_depth_requests_an_additional_expansion_claim(
    tmp_path,
) -> None:
    anchor, expansion = director_outputs()
    x2 = claim(
        "X2",
        DirectorRole.EXPANSION,
        ProgramClaimType.GENERALIZATION,
        "E3",
        dependencies=[
            ClaimDependency(
                claim_id="A1",
                relation=ClaimDependencyRelation.GENERALIZES,
            )
        ],
    )
    revised_expansion = expansion.model_copy(
        update={
            "evidence": [*expansion.evidence, evidence("E3")],
            "claims": [*expansion.claims, x2],
        }
    )
    rows_round_1 = (
        ("A1", DirectorRole.ANCHOR, "E1"),
        ("X1", DirectorRole.EXPANSION, "E2"),
    )
    rows_round_2 = (
        *rows_round_1,
        ("X2", DirectorRole.EXPANSION, "E3"),
    )
    publication_composition = ResearchProgramComposition(
        action=WorkflowAction.PROMOTE,
        integrated_claim_ids=["A1", "X1", "X2"],
        deferred_claim_ids=[],
        stages=[
            ResearchProgramStage(
                stage_number=1,
                name="Establish anchor",
                claim_ids=["A1"],
                purpose="Test the primary effect.",
                entry_condition="Research program approved.",
                completion_gate="A1 receives a valid result.",
            ),
            ResearchProgramStage(
                stage_number=2,
                name="Test independent extensions",
                claim_ids=["X1", "X2"],
                purpose="Test mechanism and generalization claims.",
                entry_condition="A1 has been tested.",
                completion_gate="Both extensions receive valid results.",
            ),
        ],
        scope="A bounded publication-depth empirical program.",
        mode_rationale="One anchor and two independent extensions passed.",
        claim_ceiling="Only the three tested claims may be reported.",
        failure_notebook=[],
        rationale="The staged program satisfies publication-depth coverage.",
    )
    anchor_agent = FakeAgent([anchor])
    expansion_agent = FakeAgent([expansion, revised_expansion])
    evaluator_a_agent = FakeAgent(
        [
            evaluator("literature", EVALUATOR_A_GATES, claim_rows=rows_round_1),
            evaluator("literature", EVALUATOR_A_GATES, claim_rows=rows_round_2),
        ]
    )
    evaluator_b_agent = FakeAgent(
        [
            evaluator("methods", EVALUATOR_B_GATES, claim_rows=rows_round_1),
            evaluator("methods", EVALUATOR_B_GATES, claim_rows=rows_round_2),
        ]
    )
    workflow = DualDirectorResearchWorkflow(
        Settings(
            research_depth="publication",
            max_hypothesis_rounds=2,
            target_promoted_hypotheses=3,
            max_component_repair_attempts=0,
        ),
        ArtifactStore(tmp_path, "run-publication-depth"),
        anchor_agent,
        expansion_agent,
        evaluator_a_agent,
        evaluator_b_agent,
        FakeAgent([publication_composition]),
    )
    publication_brief = brief(ResearchDepth.PUBLICATION)
    publication_assessment = assessment().model_copy(
        update={"research_depth": ResearchDepth.PUBLICATION}
    )

    result = asyncio.run(
        workflow.run(publication_brief, publication_assessment)
    )

    assert result.contract.selected_target_ids == ["A1", "X1", "X2"]
    assert len(anchor_agent.payloads) == 1
    assert len(expansion_agent.payloads) == 2
    revision_payload = expansion_agent.payloads[1][0]
    assert revision_payload["individual_error_notebook"][0]["claim_id"] == "X2"
    assert revision_payload["individual_error_notebook"][0]["failed_gates"] == [
        "Research Depth Coverage"
    ]
    assert [item["claim_id"] for item in revision_payload["locked_claims"]] == [
        "X1"
    ]
