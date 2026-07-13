import pytest
from pydantic import ValidationError

from ai_scientist.schemas import (
    CriterionScore,
    EvidenceLocation,
    EvidenceUnit,
    EvaluatorReport,
    TargetEvaluation,
    TargetGateScore,
    VerificationStatus,
)
from ai_scientist.validation import validate_evaluator


def _evidence(evidence_id: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        title="A traceable source",
        authors=["Researcher"],
        year=2026,
        url="https://example.test/paper",
        evidence_type="paper",
        location=EvidenceLocation(section="Results", sentence=1),
        verbatim_excerpt="A bounded result excerpt.",
        context_summary="The result supports the linked criterion.",
        verification_status=VerificationStatus.FULL_TEXT_VERIFIED,
    )


def _criterion(evidence_id: str, *, score: int = 3) -> CriterionScore:
    return CriterionScore(
        criterion="Novelty",
        score=score,
        evidence_ids=[evidence_id],
        reason="Evidence-linked reason",
        counterargument="A concrete counterargument",
        confidence=0.7,
        missing_information=[],
    )


def test_discovered_evidence_can_be_referenced() -> None:
    report = EvaluatorReport(
        evaluator_role="A",
        rubric_version="1.0",
        artifact_version="1.0",
        discovered_evidence=[_evidence("EA1")],
        criteria=[_criterion("EA1")],
        target_evaluations=[
            TargetEvaluation(
                target_id="T1",
                gates=[
                    TargetGateScore(
                        gate="Evidence Support",
                        score=3,
                        passed=True,
                        evidence_ids=["EA1"],
                        reason="Supported",
                        counterargument="Limited scope",
                    )
                ],
                fatal_issues=[],
                recommended_decision="PROMOTE",
            )
        ],
        fatal_issues=[],
        concrete_counterexamples=[],
        recommended_decision="PROMOTE",
    )

    validation = validate_evaluator(
        report,
        required_criteria={"Novelty"},
        evidence_ids={"E1"},
        rubric_version="1.0",
    )

    assert validation.valid


def test_fractional_normalized_score_is_rejected() -> None:
    with pytest.raises(ValidationError, match="valid integer"):
        CriterionScore(
            criterion="Novelty",
            score=0.5,
            evidence_ids=["E1"],
            reason="reason",
            counterargument="counterargument",
            confidence=0.7,
            missing_information=[],
        )
