from ai_scientist.rendering import (
    _markdown_to_latex,
    render_audit_report,
    render_paper,
    render_unaccepted_draft,
)
from ai_scientist.schemas import (
    ContaminationStatus,
    PaperDraft,
    PaperReference,
    ResearchMode,
    ReviewReport,
    WorkflowAction,
)


def test_markdown_table_becomes_legible_column_width_latex() -> None:
    latex = _markdown_to_latex(
        "## Results\n\n"
        "| Condition | FAR | Clean acceptance |\n"
        "|---|---:|---:|\n"
        "| C0 | 0.50 | 0.90 |\n"
        "| C3 | 0.10 | 0.85 |\n"
    )

    assert "\\begin{table}[t]" in latex
    assert "\\resizebox{\\columnwidth}{!}" in latex
    assert "Condition & FAR & Clean acceptance" in latex
    assert "C3 & 0.10 & 0.85" in latex


def test_unaccepted_draft_is_not_named_final_paper(tmp_path) -> None:
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="Draft",
        abstract="Abstract",
        markdown="# Draft\n\nUnaccepted result.",
        linked_claims=[],
        disclosed_negative_results=[],
        limitations=[],
    )
    review = ReviewReport(
        action=WorkflowAction.RETURN_TO_WRITER,
        rubric_version="1.0",
        criteria=[],
        fatal_issues=[],
        non_fatal_issues=[],
        acceptance_conditions=["Repair claim traceability"],
        contamination_status=ContaminationStatus.CLEAN,
        rationale="Not accepted",
    )

    path = render_unaccepted_draft(draft, review, tmp_path)

    assert path.name == "unaccepted_draft.md"
    assert not (tmp_path / "paper.md").exists()
    assert "UNACCEPTED DRAFT" in path.read_text(encoding="utf-8")


def test_non_success_run_gets_audit_report(tmp_path) -> None:
    path = render_audit_report(
        tmp_path,
        question="Does B improve over A?",
        final_stage="EXPERIMENT_FAILED",
        status="FAILED_WITH_AUDIT",
        details=["Execution timed out"],
    )

    text = path.read_text(encoding="utf-8")
    assert path.name == "audit_report.md"
    assert "Execution timed out" in text


def test_success_render_removes_stale_failure_outputs(tmp_path) -> None:
    (tmp_path / "audit_report.md").write_text("old failure", encoding="utf-8")
    (tmp_path / "unaccepted_draft.md").write_text("old draft", encoding="utf-8")
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="Accepted",
        abstract="Abstract",
        markdown="# Accepted\n\nFinal result.",
        linked_claims=[],
        disclosed_negative_results=[],
        limitations=[],
    )

    markdown_path, pdf_path = render_paper(draft, tmp_path)

    assert markdown_path.exists()
    assert pdf_path.exists()
    assert not (tmp_path / "audit_report.md").exists()
    assert not (tmp_path / "unaccepted_draft.md").exists()


def test_success_render_adds_structured_title_and_references(tmp_path) -> None:
    draft = PaperDraft(
        research_mode=ResearchMode.DIRECT_TEST,
        claim_ceiling="tested scope only",
        title="A bounded result",
        abstract="Abstract",
        markdown="## Abstract\n\nAbstract",
        linked_claims=[],
        references=[
            PaperReference(
                evidence_id="E1",
                title="Primary source",
                authors=["A. Author"],
                year=2026,
                url="https://example.org/source",
            )
        ],
        disclosed_negative_results=[],
        limitations=[],
    )

    markdown_path, _ = render_paper(draft, tmp_path)
    rendered = markdown_path.read_text(encoding="utf-8")

    assert rendered.startswith("# A bounded result\n")
    assert "## References" in rendered
    assert "[E1] A. Author (2026). Primary source." in rendered
