from ai_scientist.schemas import (
    ContaminationStatus,
    ContractComposerReport,
    CriterionScore,
    ExecutionResult,
    ExEvaluatorReport,
    ExperimentContract,
    ExperimentorContamination,
    ExperimentorOutput,
    GeneratedFile,
    HypothesisExperimentSpec,
    HypothesisResultJudgment,
    LinkedPaperClaim,
    PaperDraft,
    ResearchMode,
    ResearchResultStatus,
    ReviewReport,
    TargetEvaluation,
    TargetGateScore,
    WorkflowAction,
)
from ai_scientist.validation import (
    validate_contract_composer,
    validate_ex_evaluator,
    validate_execution_bundle,
    validate_experiment_contract,
    validate_paper_draft,
    validate_review,
)
from ai_scientist.workflows.experiment import (
    EX_EVALUATOR_CRITERIA,
    EX_EVALUATOR_HARD_GATES,
)
from ai_scientist.workflows.paper import REVIEW_CRITERIA, REVIEW_HARD_GATES
from ai_scientist.workflows.planning import (
    CONTRACT_EVALUATOR_A_HARD_GATES,
    CONTRACT_EVALUATOR_A_TARGET_GATES,
    CONTRACT_EVALUATOR_B_HARD_GATES,
    CONTRACT_EVALUATOR_B_TARGET_GATES,
)


def criterion(name: str, score: int, evidence_ids=None) -> CriterionScore:
    return CriterionScore(
        criterion=name,
        score=score,
        evidence_ids=evidence_ids or [],
        reason="Traceable reason",
        counterargument="Concrete counterargument",
        confidence=0.8,
        missing_information=[],
    )


def target_evaluation(gates: set[str], *, failing_gate: str | None = None):
    return TargetEvaluation(
        target_id="T1",
        gates=[
            TargetGateScore(
                gate=name,
                score=2 if name == failing_gate else 3,
                passed=name != failing_gate,
                evidence_ids=[],
                reason="Target-specific reason",
                counterargument="Target-specific counterargument",
            )
            for name in sorted(gates)
        ],
        fatal_issues=[],
        recommended_decision="REVISE" if failing_gate else "PROMOTE",
    )


def evaluator_report(
    criteria: set[str],
    hard_criteria: set[str],
    target_gates: set[str],
    *,
    failing_gate: str | None = None,
):
    from ai_scientist.schemas import EvaluatorReport

    return EvaluatorReport(
        evaluator_role="isolated",
        rubric_version="1.0",
        artifact_version="1.0",
        discovered_evidence=[],
        criteria=[
            criterion(name, 3 if name in hard_criteria else 2)
            for name in sorted(criteria)
        ],
        target_evaluations=[
            target_evaluation(target_gates, failing_gate=failing_gate)
        ],
        fatal_issues=[],
        concrete_counterexamples=[],
        recommended_decision="REVISE" if failing_gate else "PROMOTE",
    )


def experiment_output() -> ExperimentorOutput:
    return ExperimentorOutput(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        files=[GeneratedFile(path="experiment.py", content="print('run')\n")],
        entrypoint="experiment.py",
        expected_result_file="result.json",
        protocol_notes=[],
    )


def failed_execution() -> ExecutionResult:
    return ExecutionResult(
        hypothesis_id="T1",
        experiment_id="EXP-T1",
        exit_code=1,
        stdout="",
        stderr="failure",
        output_files={},
        result_ids=[],
        code_hash="hash-T1",
        workspace="workspace",
    )


def test_composer_cannot_promote_target_with_failed_target_gate() -> None:
    evaluator_a = evaluator_report(
        {
            "Mode Fit",
            "Literature Coverage",
            "Evidence Entailment",
            "Citation Accuracy",
            "Nearest-work Coverage",
            "Comparison Precedent",
            "Contradictory Evidence Coverage",
            "Evidence Quality",
        },
        CONTRACT_EVALUATOR_A_HARD_GATES,
        CONTRACT_EVALUATOR_A_TARGET_GATES,
        failing_gate="Evidence Support",
    )
    evaluator_b = evaluator_report(
        CONTRACT_EVALUATOR_B_HARD_GATES,
        CONTRACT_EVALUATOR_B_HARD_GATES,
        CONTRACT_EVALUATOR_B_TARGET_GATES,
    )
    composer = ContractComposerReport(
        action=WorkflowAction.PROMOTE,
        promoted_target_ids=["T1"],
        agreed=[],
        unique_but_critical=[],
        disagreements=[],
        evidence_failures=[],
        failure_notebook=[],
        contamination_status=ContaminationStatus.CLEAN,
        rationale="Incorrect attempted promotion",
    )

    validation = validate_contract_composer(
        composer,
        [evaluator_a, evaluator_b],
        target_ids={"T1"},
        evaluator_a_hard_criteria=CONTRACT_EVALUATOR_A_HARD_GATES,
        evaluator_b_hard_criteria=CONTRACT_EVALUATOR_B_HARD_GATES,
        evaluator_a_target_gates=CONTRACT_EVALUATOR_A_TARGET_GATES,
        evaluator_b_target_gates=CONTRACT_EVALUATOR_B_TARGET_GATES,
    )

    assert not validation.valid
    assert any(issue.code == "TARGET_HARD_GATE_FAILED" for issue in validation.issues)


def test_partial_contract_promotion_is_not_blocked_by_other_target_global_failure() -> None:
    evaluator_a = evaluator_report(
        {
            "Mode Fit",
            "Literature Coverage",
            "Evidence Entailment",
            "Citation Accuracy",
            "Nearest-work Coverage",
            "Comparison Precedent",
            "Contradictory Evidence Coverage",
            "Evidence Quality",
        },
        CONTRACT_EVALUATOR_A_HARD_GATES,
        CONTRACT_EVALUATOR_A_TARGET_GATES,
        failing_gate="Evidence Support",
    )
    a_t2 = target_evaluation(CONTRACT_EVALUATOR_A_TARGET_GATES).model_copy(
        update={"target_id": "T2"}
    )
    evaluator_a = evaluator_a.model_copy(
        update={
            "target_evaluations": [evaluator_a.target_evaluations[0], a_t2],
            "fatal_issues": ["T1 has an unresolved pair-specific literature defect"],
            "recommended_decision": "REJECT",
        }
    )
    evaluator_b = evaluator_report(
        CONTRACT_EVALUATOR_B_HARD_GATES,
        CONTRACT_EVALUATOR_B_HARD_GATES,
        CONTRACT_EVALUATOR_B_TARGET_GATES,
    )
    b_t2 = target_evaluation(CONTRACT_EVALUATOR_B_TARGET_GATES).model_copy(
        update={"target_id": "T2"}
    )
    evaluator_b = evaluator_b.model_copy(
        update={
            "target_evaluations": [evaluator_b.target_evaluations[0], b_t2],
        }
    )
    composer = ContractComposerReport(
        action=WorkflowAction.REVISE,
        promoted_target_ids=["T2"],
        agreed=[],
        unique_but_critical=[],
        disagreements=[],
        evidence_failures=["T1 needs revision"],
        failure_notebook=[],
        contamination_status=ContaminationStatus.VALID_DOWNGRADE,
        rationale="Lock T2 and revise only T1.",
    )

    validation = validate_contract_composer(
        composer,
        [evaluator_a, evaluator_b],
        target_ids={"T1", "T2"},
        evaluator_a_hard_criteria=CONTRACT_EVALUATOR_A_HARD_GATES,
        evaluator_b_hard_criteria=CONTRACT_EVALUATOR_B_HARD_GATES,
        evaluator_a_target_gates=CONTRACT_EVALUATOR_A_TARGET_GATES,
        evaluator_b_target_gates=CONTRACT_EVALUATOR_B_TARGET_GATES,
    )

    assert validation.valid


def test_experiment_contract_must_cover_selected_targets_exactly() -> None:
    contract = ExperimentContract(
        contract_version="1.0",
        hypothesis_ids=["T2"],
        dataset_plan="fixed data",
        shared_protocol=["same compute"],
        metrics=["validation loss"],
        seeds=[1, 2],
        statistical_plan="paired comparison",
        stopping_rule="fixed token budget",
        hypothesis_specs=[
            HypothesisExperimentSpec(
                hypothesis_id="T2",
                unique_prediction="lower loss",
                manipulation="optimizer",
                controls=["model"],
                measurement="validation loss",
                expected_pattern="B lower than A",
                rejection_condition="effect below threshold",
            )
        ],
    )

    validation = validate_experiment_contract(
        contract,
        selected_target_ids={"T1"},
    )

    assert not validation.valid
    assert any(issue.code == "TARGET_SET_MISMATCH" for issue in validation.issues)


def test_failed_execution_cannot_form_complete_bundle() -> None:
    validation = validate_execution_bundle(
        selected_target_ids={"T1"},
        experimentor_outputs=[experiment_output()],
        executions=[failed_execution()],
    )

    assert not validation.valid
    codes = {issue.code for issue in validation.issues}
    assert "EXECUTION_FAILED" in codes
    assert "EXPECTED_RESULT_MISSING" in codes


def test_ex_evaluator_cannot_pass_failed_execution() -> None:
    evaluation = ExEvaluatorReport(
        action=WorkflowAction.PASS,
        rubric_version="1.0",
        criteria=[
            criterion(name, 3, ["EXP-T1"])
            for name in sorted(EX_EVALUATOR_CRITERIA)
        ],
        judgments=[
            HypothesisResultJudgment(
                hypothesis_id="T1",
                status=ResearchResultStatus.INCONCLUSIVE,
                rationale="The execution failed.",
                result_ids=[],
            )
        ],
        affected_hypothesis_ids=[],
        failure_notebook=[],
        contamination_by_experimentor=[
            ExperimentorContamination(
                hypothesis_id="T1",
                status=ContaminationStatus.CLEAN,
            )
        ],
        rationale="Incorrect PASS",
    )

    validation = validate_ex_evaluator(
        evaluation,
        required_criteria=EX_EVALUATOR_CRITERIA,
        hard_gate_criteria=EX_EVALUATOR_HARD_GATES,
        selected_target_ids={"T1"},
        experimentor_outputs=[experiment_output()],
        executions=[failed_execution()],
        rubric_version="1.0",
    )

    assert not validation.valid
    assert any(issue.code == "EXECUTION_FAILED" for issue in validation.issues)


def test_ex_evaluator_repair_targets_require_matching_individual_notes() -> None:
    evaluation = ExEvaluatorReport(
        action=WorkflowAction.REPAIR,
        rubric_version="1.0",
        criteria=[
            criterion(name, 2, ["EXP-T1"])
            for name in sorted(EX_EVALUATOR_CRITERIA)
        ],
        judgments=[
            HypothesisResultJudgment(
                hypothesis_id="T1",
                status=ResearchResultStatus.PROTOCOL_VIOLATION,
                rationale="The execution failed and requires repair.",
                result_ids=[],
            )
        ],
        affected_hypothesis_ids=["T1"],
        failure_notebook=[],
        contamination_by_experimentor=[
            ExperimentorContamination(
                hypothesis_id="T1",
                status=ContaminationStatus.CLEAN,
            )
        ],
        rationale="Repair T1 only.",
    )

    validation = validate_ex_evaluator(
        evaluation,
        required_criteria=EX_EVALUATOR_CRITERIA,
        hard_gate_criteria=EX_EVALUATOR_HARD_GATES,
        selected_target_ids={"T1"},
        experimentor_outputs=[experiment_output()],
        executions=[failed_execution()],
        rubric_version="1.0",
    )

    assert not validation.valid
    assert any(
        issue.code == "EXPERIMENT_REPAIR_NOTE_COVERAGE"
        for issue in validation.issues
    )


def test_paper_claim_rejects_unknown_result_id() -> None:
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="Direct test",
        abstract="A bounded result.",
        markdown="# Direct test\n\nResult.",
        linked_claims=[
            LinkedPaperClaim(
                claim_id="PC1",
                claim="B improved validation loss.",
                evidence_ids=[],
                result_ids=["UNKNOWN"],
            )
        ],
        disclosed_negative_results=[],
        limitations=["small scope"],
    )

    validation = validate_paper_draft(
        draft,
        expected_mode=ResearchMode.DIRECT_TEST,
        expected_claim_ceiling="tested scope only",
        evidence_ids=set(),
        result_ids={"EXP-T1:result.json"},
        negative_results_required=False,
    )

    assert not validation.valid
    assert any(issue.code == "UNKNOWN_PAPER_RESULT_ID" for issue in validation.issues)


def test_paper_rejects_reversed_lower_is_better_contrast_direction() -> None:
    result_id = "EXP-A2:result.json"
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="Weight decay comparison",
        abstract="A bounded result.",
        markdown=(
            "# Weight decay comparison\n\n"
            "AdamW outperformed Adam on validation loss."
        ),
        linked_claims=[
            LinkedPaperClaim(
                claim_id="PC-A2",
                claim="AdamW outperformed Adam.",
                evidence_ids=[],
                result_ids=[result_id],
            )
        ],
        disclosed_negative_results=[],
        limitations=["surrogate only"],
    )

    validation = validate_paper_draft(
        draft,
        expected_mode=ResearchMode.DIRECT_TEST,
        expected_claim_ceiling="tested scope only",
        evidence_ids=set(),
        result_ids={result_id},
        negative_results_required=False,
        result_payloads={
            result_id: {
                "primary_metric": "validation_cross_entropy",
                "paired_comparisons": {
                    "adam_vs_adamw": {"mean_diff": -0.1536}
                },
            }
        },
    )

    assert not validation.valid
    assert any(
        issue.code == "RESULT_DIRECTION_CONTRADICTION"
        for issue in validation.issues
    )


def test_paper_requires_structured_reference_for_used_evidence() -> None:
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="Evidence-backed study",
        abstract="A bounded result.",
        markdown="# Evidence-backed study\n\nResult.",
        linked_claims=[
            LinkedPaperClaim(
                claim_id="PC1",
                claim="A literature-backed claim.",
                evidence_ids=["E1"],
                result_ids=[],
            )
        ],
        disclosed_negative_results=[],
        limitations=["bounded"],
    )

    validation = validate_paper_draft(
        draft,
        expected_mode=ResearchMode.DIRECT_TEST,
        expected_claim_ceiling="tested scope only",
        evidence_ids={"E1"},
        result_ids=set(),
        negative_results_required=False,
    )

    assert any(
        issue.code == "MISSING_PAPER_REFERENCE"
        for issue in validation.issues
    )


def test_reviewer_cannot_accept_below_hard_gate() -> None:
    review = ReviewReport(
        action=WorkflowAction.ACCEPT,
        rubric_version="1.0",
        criteria=[
            criterion(
                name,
                2 if name == "Claim Ceiling Compliance" else 3,
                ["PC1"],
            )
            for name in sorted(REVIEW_CRITERIA)
        ],
        fatal_issues=[],
        non_fatal_issues=[],
        acceptance_conditions=[],
        contamination_status=ContaminationStatus.CLEAN,
        rationale="Incorrect ACCEPT",
    )

    validation = validate_review(
        review,
        required_criteria=REVIEW_CRITERIA,
        hard_gate_criteria=REVIEW_HARD_GATES,
        known_trace_ids={"PC1"},
        rubric_version="1.0",
    )

    assert not validation.valid
    assert any(issue.code == "ACCEPT_HARD_GATE_FAILED" for issue in validation.issues)


def test_significant_writer_degradation_cannot_remain_clean() -> None:
    review = ReviewReport(
        action=WorkflowAction.RETURN_TO_WRITER,
        rubric_version="1.0",
        criteria=[
            criterion(name, 3, ["PC1"]) for name in sorted(REVIEW_CRITERIA)
        ],
        fatal_issues=[],
        non_fatal_issues=[],
        acceptance_conditions=["Explain the degradation"],
        contamination_status=ContaminationStatus.CLEAN,
        rationale="Needs another Writer round",
    )

    validation = validate_review(
        review,
        required_criteria=REVIEW_CRITERIA,
        hard_gate_criteria=REVIEW_HARD_GATES,
        known_trace_ids={"PC1"},
        rubric_version="1.0",
        significant_degradation=True,
    )

    assert not validation.valid
    assert any(issue.code == "DEGRADATION_MARKED_CLEAN" for issue in validation.issues)
