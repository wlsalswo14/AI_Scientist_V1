import pytest
from pydantic import ValidationError

from ai_scientist.schemas import (
    ResearchCondition,
    ResearchContract,
    ResearchMode,
    ResearchModeAssessment,
    ResearchPredictionCell,
    ResearchReadiness,
    ResearchTarget,
    ResearchTargetType,
)
from ai_scientist.validation import validate_research_contract
from ai_scientist.workflows.planning import _preserve_locked_targets


def direct_target() -> ResearchTarget:
    return ResearchTarget(
        target_id="T1",
        target_type=ResearchTargetType.TEST_CLAIM,
        statement="Optimizer B lowers validation loss relative to A.",
        null_statement="B does not lower validation loss relative to A.",
        rationale="The user supplied a direct comparative claim.",
        mechanism="",
        distinctive_prediction="The paired loss difference B-A is below zero.",
        falsification_condition="The confidence interval excludes the minimum effect.",
        alternative_explanations=["Unequal tuning budget"],
        positive_result_value="B improves validation loss.",
        negative_result_value="B worsens validation loss.",
        null_result_value="No practically meaningful difference is resolved.",
        minimum_experiment="Matched small-model training runs.",
        required_data="A fixed train/validation split.",
        compute_estimate="Two optimizers across paired seeds.",
        uncertainties=["Seed variance"],
        evidence_ids=[],
    )


def direct_contract() -> ResearchContract:
    return ResearchContract(
        contract_version="1.0",
        original_question="Does optimizer B improve validation loss over A?",
        research_mode=ResearchMode.DIRECT_TEST,
        readiness=ResearchReadiness.PROPOSED,
        selected_domain="small language-model training",
        scope="matched optimizer comparison",
        mode_rationale="The question already contains the claim to test.",
        claim_ceiling="Only claim an effect in the tested model and budget range.",
        evidence=[],
        claims=[],
        targets=[direct_target()],
        selected_target_ids=[],
        prediction_matrix=[
            ResearchCondition(
                condition_id="C1",
                description="Matched A/B training",
                controlled_variables=["model", "data", "seed", "token budget"],
                manipulated_variables=["optimizer"],
                measurement="paired final validation-loss difference",
                decision_threshold="predeclared minimum loss reduction of 0.010",
                predictions=[
                    ResearchPredictionCell(
                        target_id="T1",
                        direction="B lower than A",
                        expected_pattern="B-A is below the minimum-effect threshold",
                        rejection_condition="the interval fails the threshold",
                    )
                ],
            )
        ],
        search_limitations=[],
    )


def test_direct_mode_does_not_force_three_hypotheses() -> None:
    contract = direct_contract()
    validation = validate_research_contract(
        contract,
        expected_mode=ResearchMode.DIRECT_TEST,
    )

    assert len(contract.targets) == 1
    assert validation.valid


def test_direct_mode_requires_a_numeric_decision_rule() -> None:
    contract = direct_contract()
    condition = contract.prediction_matrix[0].model_copy(
        update={"decision_threshold": "a minimum meaningful loss reduction"}
    )
    invalid = contract.model_copy(update={"prediction_matrix": [condition]})

    validation = validate_research_contract(
        invalid,
        expected_mode=ResearchMode.DIRECT_TEST,
    )

    assert not validation.valid
    assert any(issue.code == "NON_NUMERIC_DECISION_RULE" for issue in validation.issues)


def test_direct_mode_assessment_forbids_forced_competing_hypotheses() -> None:
    with pytest.raises(ValidationError, match="must not force competing hypotheses"):
        ResearchModeAssessment(
            original_question="Does B improve validation loss over A?",
            proposed_mode=ResearchMode.DIRECT_TEST,
            classification_reason="Direct comparison",
            direct_testable_claim="B lowers validation loss relative to A.",
            requires_competing_hypotheses=True,
            comparison_entities=["A", "B"],
            primary_outcome="validation loss",
            claim_ceiling="tested setting only",
            confidence=0.9,
            unresolved_ambiguities=[],
        )


def test_explanatory_mode_requires_three_targets() -> None:
    payload = direct_contract().model_dump(mode="json")
    payload.update(
        {
            "research_mode": ResearchMode.EXPLANATORY_RESEARCH,
            "targets": [
                {
                    **direct_target().model_dump(mode="json"),
                    "target_type": ResearchTargetType.MECHANISTIC_HYPOTHESIS,
                }
            ],
        }
    )

    with pytest.raises(ValidationError, match="requires 3-5 targets"):
        ResearchContract.model_validate(payload)


def test_locked_direct_target_preserves_its_prediction_contract() -> None:
    previous = direct_contract()
    changed_target = direct_target().model_copy(
        update={"statement": "A rewritten statement that must not replace T1."}
    )
    changed_condition = previous.prediction_matrix[0].model_copy(
        update={"decision_threshold": "a weakened post-hoc threshold"}
    )
    current = previous.model_copy(
        update={
            "targets": [changed_target],
            "prediction_matrix": [changed_condition],
        }
    )

    merged = _preserve_locked_targets(previous, current, {"T1"})

    assert merged.targets[0] == previous.targets[0]
    assert merged.prediction_matrix[0] == previous.prediction_matrix[0]
