from ai_scientist.evidence_audit import (
    accumulate_repair_issues,
    global_audit_issues,
    materialize_concerns,
    resolution_issues,
)
from ai_scientist.schemas import (
    EvidenceAuditManifest,
    EvidenceAuditOutcome,
    EvidenceAuditUnit,
    EvidenceConcernCategory,
    EvidenceConcernResolution,
    EvidenceConcernSeverity,
    EvidenceCriticReport,
    EvidenceGlobalAuditReport,
    EvidenceQuestionDraft,
    EvidenceResolutionStatus,
    ExEvaluatorReport,
    WorkflowAction,
)
from ai_scientist.workflows.experiment import _apply_evidence_audit_gate


def test_repair_issue_history_accumulates_without_duplicates() -> None:
    first = {"path": "/questions/0", "code": "UNKNOWN_TARGET"}
    second = {"path": "/critic_lens", "code": "WRONG_LENS"}

    history = accumulate_repair_issues([], [first])
    history = accumulate_repair_issues(history, [second, first])

    assert history == [first, second]


def _critic_report() -> EvidenceCriticReport:
    return EvidenceCriticReport(
        critic_lens="construct",
        questions=[
            EvidenceQuestionDraft(
                category=EvidenceConcernCategory.TARGET_EVIDENCE_ALIGNMENT,
                target_ids=["H1"],
                question="Does the generated fixture represent the claimed real population?",
                evidence_obligation=["An independent real-world validation bridge"],
                why_material="The claim concerns real research rather than fixture conformance.",
                proposed_severity=EvidenceConcernSeverity.FATAL,
            )
        ],
    )


def test_materialize_concerns_assigns_stable_ids() -> None:
    first = materialize_concerns([_critic_report()], {"H1"})
    second = materialize_concerns([_critic_report()], {"H1"})

    assert first[0].concern_id == second[0].concern_id
    assert first[0].category == EvidenceConcernCategory.TARGET_EVIDENCE_ALIGNMENT


def test_promoted_resolution_cannot_recommend_pass() -> None:
    resolution = EvidenceConcernResolution(
        concern_id="EC-1",
        status=EvidenceResolutionStatus.PROMOTED,
        severity=EvidenceConcernSeverity.FATAL,
        evidence_unit_ids=["EA-1"],
        finding="Only generated fixtures were evaluated.",
        unresolved_gap="No real-world bridge exists.",
        recommended_action=WorkflowAction.PASS,
    )

    assert "promoted concern cannot recommend PASS" in resolution_issues(
        resolution,
        "EC-1",
        {"EA-1"},
    )


def test_global_audit_must_partition_promoted_concerns() -> None:
    report = EvidenceGlobalAuditReport(
        kept_concern_ids=["EC-1"],
        discarded=[],
        rationale="One concern retained.",
    )

    assert global_audit_issues(report, {"EC-1", "EC-2"})


def test_unresolved_fatal_concern_forces_return_to_hypothesis() -> None:
    critic = _critic_report()
    concern = materialize_concerns([critic], {"H1"})[0]
    resolution = EvidenceConcernResolution(
        concern_id=concern.concern_id,
        status=EvidenceResolutionStatus.PROMOTED,
        severity=EvidenceConcernSeverity.FATAL,
        evidence_unit_ids=["EA-1"],
        finding="The observed and claimed populations differ.",
        unresolved_gap="No independent real-world evidence bridge exists.",
        recommended_action=WorkflowAction.RETURN_TO_HYPOTHESIS,
    )
    outcome = EvidenceAuditOutcome(
        manifest=EvidenceAuditManifest(
            manifest_version="1.0",
            units=[
                EvidenceAuditUnit(
                    unit_id="EA-1",
                    unit_type="research-contract",
                    target_ids=["H1"],
                    content={"claim": "real-world effect"},
                )
            ],
        ),
        critic_reports=[critic],
        concerns=[concern],
        resolutions=[resolution],
        global_audit=EvidenceGlobalAuditReport(
            kept_concern_ids=[concern.concern_id],
            discarded=[],
            rationale="Fatal target mismatch retained.",
        ),
        unresolved_major_ids=[],
        unresolved_fatal_ids=[concern.concern_id],
        recommended_action=WorkflowAction.RETURN_TO_HYPOTHESIS,
        paper_eligible=False,
        complete=True,
    )
    evaluation = ExEvaluatorReport(
        action=WorkflowAction.PASS,
        rubric_version="1.0",
        criteria=[],
        judgments=[],
        affected_hypothesis_ids=[],
        failure_notebook=[],
        contamination_by_experimentor=[],
        rationale="Execution passed.",
    )

    gated = _apply_evidence_audit_gate(evaluation, outcome)

    assert gated.action == WorkflowAction.RETURN_TO_HYPOTHESIS
    assert concern.concern_id in gated.rationale
