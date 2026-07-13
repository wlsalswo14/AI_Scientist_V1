from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from .schemas import (
    PaperDraft,
    ResearchProfile,
    ReviewReport,
    SubmissionFormatAudit,
)


def render_paper(draft: PaperDraft, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_outputs(output_dir, "audit_report.md", "unaccepted_draft.md")
    markdown_path = output_dir / "paper.md"
    pdf_path = output_dir / "paper.pdf"
    rendered_markdown = _paper_markdown(draft)
    markdown_path.write_text(rendered_markdown, encoding="utf-8")

    if draft.research_profile == ResearchProfile.TRACE_AUDIT:
        _render_two_column_short_paper(
            draft,
            pdf_path,
            include_references=True,
        )
        return markdown_path, pdf_path

    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=draft.title,
    )
    story = [Paragraph(_escape(draft.title), styles["Title"]), Spacer(1, 12)]
    skipped_markdown_title = False
    for raw_line in rendered_markdown.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 7))
        elif line.startswith("# "):
            if not skipped_markdown_title and line[2:].strip() == draft.title.strip():
                skipped_markdown_title = True
                continue
            if len(story) > 2:
                story.append(PageBreak())
            story.append(Paragraph(_escape(line[2:]), styles["Title"]))
        elif line.startswith("## "):
            story.append(Paragraph(_escape(line[3:]), styles["Heading2"]))
        elif line.startswith("### "):
            story.append(Paragraph(_escape(line[4:]), styles["Heading3"]))
        elif line.startswith("- "):
            story.append(Paragraph("• " + _escape(line[2:]), styles["BodyText"]))
        else:
            story.append(Paragraph(_escape(line), styles["BodyText"]))
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return markdown_path, pdf_path


def render_submission_artifacts(
    draft: PaperDraft,
    review: ReviewReport,
    output_dir: Path,
    *,
    require_official_pdf: bool = False,
) -> tuple[Path, Path, Path, SubmissionFormatAudit]:
    """Emit the anonymous short-paper package and deterministic format audit."""

    output_dir.mkdir(parents=True, exist_ok=True)
    latex_path = output_dir / "paper.tex"
    metadata_path = output_dir / "submission.json"
    self_review_path = output_dir / "self_review.md"
    audit_path = output_dir / "format_audit.json"
    latex_source = _icml_latex_source(draft)
    latex_path.write_text(latex_source, encoding="utf-8")
    official_compiled, compile_warning, official_main_pages = (
        _compile_icml_latex(output_dir)
    )
    metadata_path.write_text(
        json.dumps(
            {
                "title": draft.title,
                "abstract": draft.abstract,
                "anonymous": True,
                "main_body_page_limit": 4,
                "references_excluded_from_limit": True,
                "research_profile": draft.research_profile.value,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    self_review_path.write_text(
        _self_review_markdown(draft, review),
        encoding="utf-8",
    )
    temporary_main = output_dir / ".main-body-count.pdf"
    _render_two_column_short_paper(
        draft,
        temporary_main,
        include_references=False,
    )
    main_body_pages = len(PdfReader(str(temporary_main)).pages)
    temporary_main.unlink(missing_ok=True)
    if official_main_pages is not None:
        main_body_pages = official_main_pages
    abstract_sentences = _sentence_count(draft.abstract)
    anonymous_issues = _anonymity_issues(draft, latex_source)
    anonymous_issues.extend(_pdf_anonymity_issues(output_dir / "paper.pdf"))
    issues: list[str] = list(anonymous_issues)
    if not 4 <= abstract_sentences <= 6:
        issues.append(
            f"Abstract has {abstract_sentences} sentences; expected 4-6"
        )
    if main_body_pages > 4:
        issues.append(
            f"Main body occupies {main_body_pages} pages; hard limit is 4"
        )
    warnings: list[str] = []
    if compile_warning:
        warnings.append(compile_warning)
    if require_official_pdf and not official_compiled:
        issues.append(
            "Official ICML LaTeX PDF could not be compiled; fallback PDF is not submission-ready"
        )
    audit = SubmissionFormatAudit(
        anonymous=not anonymous_issues,
        abstract_sentence_count=abstract_sentences,
        abstract_valid=4 <= abstract_sentences <= 6,
        main_body_pages=main_body_pages,
        main_body_within_limit=main_body_pages <= 4,
        references_excluded=True,
        icml_latex_source=("\\usepackage{icml2026}" in latex_source),
        official_pdf_compiled=official_compiled,
        pdf_backend=("icml2026-latex" if official_compiled else "reportlab-fallback"),
        self_review_present=self_review_path.stat().st_size > 0,
        issues=issues,
        warnings=warnings,
    )
    audit_path.write_text(
        audit.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return latex_path, metadata_path, self_review_path, audit


def render_unaccepted_draft(
    draft: PaperDraft,
    review: ReviewReport,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_outputs(output_dir, "paper.md", "paper.pdf", "audit_report.md")
    path = output_dir / "unaccepted_draft.md"
    warning = (
        "# UNACCEPTED DRAFT\n\n"
        f"Reviewer action: `{review.action.value}`\n\n"
        f"Reviewer rationale: {review.rationale}\n\n"
        "This draft did not pass the Reviewer hard gates and is not the final paper.\n\n"
        "---\n\n"
    )
    path.write_text(warning + _paper_markdown(draft), encoding="utf-8")
    return path


def render_audit_report(
    output_dir: Path,
    *,
    question: str,
    final_stage: str,
    status: str,
    details: list[str],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_outputs(output_dir, "paper.md", "paper.pdf", "unaccepted_draft.md")
    path = output_dir / "audit_report.md"
    lines = [
        "# AI Scientist Audit Report",
        "",
        f"- Status: `{status}`",
        f"- Final stage: `{final_stage}`",
        f"- Question: {question}",
        "",
        "## Unresolved issues",
        "",
    ]
    if details:
        lines.extend(f"- {item}" for item in dict.fromkeys(details))
    else:
        lines.append("- No detailed issue was recorded.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _remove_outputs(output_dir: Path, *names: str) -> None:
    for name in names:
        path = output_dir / name
        if path.is_file():
            path.unlink()


def _paper_markdown(draft: PaperDraft) -> str:
    markdown = draft.markdown.strip()
    first_nonempty = next(
        (line.strip() for line in markdown.splitlines() if line.strip()),
        "",
    )
    if first_nonempty != f"# {draft.title.strip()}":
        markdown = f"# {draft.title.strip()}\n\n{markdown}"
    has_references = any(
        line.strip().lower() == "## references"
        for line in markdown.splitlines()
    )
    if draft.references and not has_references:
        lines = [markdown, "", "## References", ""]
        for reference in draft.references:
            authors = ", ".join(reference.authors) or "Unknown author"
            year = str(reference.year) if reference.year is not None else "n.d."
            lines.append(
                f"- [{reference.evidence_id}] {authors} ({year}). "
                f"{reference.title}. {reference.url}"
            )
        markdown = "\n".join(lines)
    return markdown.rstrip() + "\n"


def _footer(canvas, document) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillGray(0.4)
    canvas.drawCentredString(A4[0] / 2, 10 * mm, f"Page {document.page}")
    canvas.restoreState()


def _render_two_column_short_paper(
    draft: PaperDraft,
    pdf_path: Path,
    *,
    include_references: bool,
) -> None:
    page_width, page_height = letter
    margin_x = 0.75 * 72
    margin_top = 0.75 * 72
    margin_bottom = 0.65 * 72
    gutter = 0.25 * 72
    column_width = (page_width - 2 * margin_x - gutter) / 2
    frame_height = page_height - margin_top - margin_bottom
    frames = [
        Frame(
            margin_x,
            margin_bottom,
            column_width,
            frame_height,
            leftPadding=0,
            rightPadding=5,
            topPadding=0,
            bottomPadding=0,
            id="left",
        ),
        Frame(
            margin_x + column_width + gutter,
            margin_bottom,
            column_width,
            frame_height,
            leftPadding=5,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
            id="right",
        ),
    ]
    document = BaseDocTemplate(
        str(pdf_path),
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_top,
        bottomMargin=margin_bottom,
        title=draft.title,
        author="",
        subject="Anonymous short-paper submission",
    )
    document.addPageTemplates(
        [PageTemplate(id="two-column", frames=frames, onPage=_short_paper_footer)]
    )
    styles = _short_paper_styles()
    story = [
        Paragraph(_escape(draft.title), styles["Title"]),
        Spacer(1, 5),
        Paragraph("Abstract", styles["AbstractHeading"]),
        Paragraph(_escape(draft.abstract), styles["Abstract"]),
        Spacer(1, 7),
    ]
    body, references = _split_markdown_references(_paper_markdown(draft))
    story.extend(_markdown_flowables(body, styles, skip_title=draft.title))
    if include_references and references:
        story.append(Paragraph("References", styles["Heading2"]))
        story.extend(_markdown_flowables(references, styles, skip_title=""))
    document.build(story)


def _short_paper_styles() -> dict[str, ParagraphStyle]:
    return {
        "Title": ParagraphStyle(
            "ShortTitle",
            fontName="Times-Bold",
            fontSize=14,
            leading=16,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "AbstractHeading": ParagraphStyle(
            "AbstractHeading",
            fontName="Times-Bold",
            fontSize=10,
            leading=11,
            alignment=TA_CENTER,
            spaceAfter=2,
        ),
        "Abstract": ParagraphStyle(
            "Abstract",
            fontName="Times-Roman",
            fontSize=9,
            leading=10,
            leftIndent=8,
            rightIndent=8,
            spaceAfter=4,
        ),
        "Heading2": ParagraphStyle(
            "Heading2",
            fontName="Times-Bold",
            fontSize=11,
            leading=12,
            spaceBefore=7,
            spaceAfter=3,
        ),
        "Heading3": ParagraphStyle(
            "Heading3",
            fontName="Times-Bold",
            fontSize=10,
            leading=11,
            spaceBefore=5,
            spaceAfter=2,
        ),
        "Body": ParagraphStyle(
            "Body",
            fontName="Times-Roman",
            fontSize=10,
            leading=11,
            spaceAfter=4,
        ),
    }


def _markdown_flowables(
    markdown: str,
    styles: dict[str, ParagraphStyle],
    *,
    skip_title: str,
) -> list[object]:
    flowables: list[object] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            if line[2:].strip() == skip_title.strip():
                continue
            flowables.append(Paragraph(_escape(line[2:]), styles["Title"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(_escape(line[3:]), styles["Heading2"]))
        elif line.startswith("### "):
            flowables.append(Paragraph(_escape(line[4:]), styles["Heading3"]))
        elif line.startswith("- "):
            flowables.append(
                Paragraph("&bull; " + _escape(line[2:]), styles["Body"])
            )
        else:
            flowables.append(Paragraph(_escape(line), styles["Body"]))
    return flowables


def _split_markdown_references(markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "## references":
            return "\n".join(lines[:index]), "\n".join(lines[index + 1 :])
    return markdown, ""


def _icml_latex_source(draft: PaperDraft) -> str:
    references = []
    for index, reference in enumerate(draft.references, start=1):
        authors = ", ".join(reference.authors) or "Unknown author"
        year = reference.year if reference.year is not None else "n.d."
        citation_author = (
            reference.authors[0].split()[-1]
            if reference.authors and reference.authors[0].split()
            else f"Source{index}"
        )
        citation_year = reference.year if reference.year is not None else 0
        references.append(
            "\\bibitem["
            + _latex_escape(f"{citation_author}({citation_year})")
            + "]{"
            + _latex_escape(reference.evidence_id)
            + "} "
            + _latex_escape(f"{authors} ({year}). {reference.title}. {reference.url}")
        )
    bibliography = (
        "\n".join(references)
        or "\\bibitem[Source(0)]{none} No references."
    )
    # Validated Markdown is the single paper source of truth. Accepting an
    # independently authored LaTeX body would allow claims or numbers to diverge
    # after the paper hard gates have run.
    body = _markdown_to_latex(draft.markdown)
    return (
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{icml2026}\n"
        "\\usepackage{microtype}\n"
        "\\icmltitlerunning{" + _latex_escape(draft.title[:80]) + "}\n"
        "\\begin{document}\n"
        "\\twocolumn[\n"
        "\\icmltitle{" + _latex_escape(draft.title) + "}\n"
        "\\begin{icmlauthorlist}\n"
        "\\end{icmlauthorlist}\n"
        "\\vskip 0.3in\n"
        "]\n"
        "\\printAffiliationsAndNotice{}\n"
        "\\begin{abstract}\n"
        + _latex_escape(draft.abstract)
        + "\n\\end{abstract}\n"
        + body
        + "\n\\label{trace:lastmainpage}\n"
        + "\n\\begin{thebibliography}{99}\n"
        + bibliography
        + "\n\\end{thebibliography}\n"
        "\\end{document}\n"
    )


def _markdown_to_latex(markdown: str) -> str:
    rendered: list[str] = []
    source = markdown.splitlines()
    index = 0
    while index < len(source):
        line = source[index].strip()
        if not line or line.startswith("# "):
            index += 1
            continue
        if (
            line.startswith("|")
            and index + 1 < len(source)
            and _markdown_table_separator(source[index + 1].strip())
        ):
            headers = _markdown_table_cells(line)
            rows: list[list[str]] = []
            index += 2
            while index < len(source) and source[index].strip().startswith("|"):
                rows.append(_markdown_table_cells(source[index].strip()))
                index += 1
            column_count = max(
                [len(headers), *(len(row) for row in rows)],
                default=1,
            )
            normalized_headers = headers + [""] * (column_count - len(headers))
            normalized_rows = [
                row + [""] * (column_count - len(row)) for row in rows
            ]
            rendered.extend(
                [
                    "\\begin{table}[t]",
                    "\\caption{Condition-level TRACE-AUDIT results.}",
                    "\\centering\\scriptsize",
                    "\\resizebox{\\columnwidth}{!}{%",
                    "\\begin{tabular}{" + "l" * column_count + "}",
                    "\\hline",
                    " & ".join(_latex_escape(cell) for cell in normalized_headers)
                    + r" \\",
                    "\\hline",
                    *[
                        " & ".join(_latex_escape(cell) for cell in row) + r" \\"
                        for row in normalized_rows
                    ],
                    "\\hline",
                    "\\end{tabular}%",
                    "}",
                    "\\end{table}",
                ]
            )
            continue
        if line.startswith("## "):
            if line[3:].strip().lower() == "references":
                break
            rendered.append("\\section{" + _latex_escape(line[3:]) + "}")
        elif line.startswith("### "):
            rendered.append("\\subsection{" + _latex_escape(line[4:]) + "}")
        elif line.startswith("- "):
            rendered.append("\\paragraph{} " + _latex_escape(line[2:]))
        else:
            rendered.append(_latex_escape(line) + "\n")
        index += 1
    return "\n".join(rendered)


def _markdown_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _markdown_table_separator(line: str) -> bool:
    if not line.startswith("|"):
        return False
    cells = _markdown_table_cells(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) is not None
        for cell in cells
    )


def _latex_escape(value: str) -> str:
    translations = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(translations.get(character, character) for character in value)


def _sentence_count(value: str) -> int:
    return len(
        [
            item
            for item in re.split(r"(?<=[.!?])\s+", value.strip())
            if item.strip()
        ]
    )


def _anonymity_issues(draft: PaperDraft, latex_source: str) -> list[str]:
    issues: list[str] = []
    searchable = "\n".join(
        [draft.title, draft.abstract, draft.markdown]
    )
    patterns = {
        "author command": r"\\(?:icml)?author\s*\{",
        "affiliation command": r"\\(?:icml)?affiliation\s*\{",
        "acknowledgements": r"\backnowledg(?:e)?ments?\b",
        "email address": r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    }
    for label, pattern in patterns.items():
        if re.search(pattern, searchable, flags=re.IGNORECASE):
            issues.append(f"Anonymous submission contains {label}")
    if "\\usepackage{icml2026}" not in latex_source:
        issues.append("LaTeX source does not load the ICML 2026 style")
    return issues


def _pdf_anonymity_issues(pdf_path: Path) -> list[str]:
    if not pdf_path.is_file():
        return ["Submission PDF is missing during anonymity audit"]
    try:
        reader = PdfReader(str(pdf_path))
        metadata = reader.metadata
        author = (metadata.author or "").strip() if metadata else ""
        text = "\n".join(
            page.extract_text() or "" for page in reader.pages[:4]
        )
    except Exception as exc:  # pragma: no cover - defensive PDF parser boundary
        return [f"Submission PDF could not be inspected for anonymity: {exc}"]
    issues: list[str] = []
    if author and author.casefold() not in {"anonymous", "anonymous authors"}:
        issues.append("Submission PDF metadata contains an author")
    if re.search(
        r"Anonymous Authors?|Anonymous Institution|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        text,
        flags=re.IGNORECASE,
    ):
        issues.append("Submission PDF visibly contains author, affiliation, or email text")
    return issues


def _self_review_markdown(draft: PaperDraft, review: ReviewReport) -> str:
    strengths = [
        item.criterion
        for item in review.criteria
        if item.score >= 4 and not item.fatal_issue
    ]
    weaknesses = [
        issue.issue for issue in [*review.fatal_issues, *review.non_fatal_issues]
    ]
    if not weaknesses:
        weaknesses = list(draft.limitations)
    return (
        "# Self-Review\n\n"
        "## Summary\n\n"
        f"{review.rationale}\n\n"
        "## Strengths\n\n"
        + "\n".join(f"- {item}" for item in strengths or ["No score reached 4."])
        + "\n\n## Weaknesses and limitations\n\n"
        + "\n".join(f"- {item}" for item in weaknesses)
        + "\n\n## Claim and evidence audit\n\n"
        f"- Linked claims: {len(draft.linked_claims)}\n"
        f"- Disclosed negative results: {len(draft.disclosed_negative_results)}\n"
        f"- Reviewer action: {review.action.value}\n"
    )


def _compile_icml_latex(
    output_dir: Path,
) -> tuple[bool, str | None, int | None]:
    vendor_dir = Path(__file__).resolve().parents[2] / "vendor" / "icml2026"
    style_path = vendor_dir / "icml2026.sty"
    if style_path.is_file():
        for source in vendor_dir.iterdir():
            if source.suffix.lower() in {".sty", ".bst"}:
                shutil.copy2(source, output_dir / source.name)
    executable = shutil.which("pdflatex")
    engine = "pdflatex"
    if executable is None:
        executable = shutil.which("tectonic")
        engine = "tectonic"
    if executable is None or not (output_dir / "icml2026.sty").is_file():
        return (
            False,
            "A LaTeX engine or the official icml2026.sty was not available; "
            "paper.pdf uses the two-column fallback renderer.",
            None,
        )
    command = (
        [
            executable,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "paper.tex",
        ]
        if engine == "pdflatex"
        else [
            executable,
            "--keep-intermediates",
            "--keep-logs",
            "paper.tex",
        ]
    )
    try:
        for _ in range(2):
            completed = subprocess.run(
                command,
                cwd=output_dir,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            if completed.returncode != 0:
                diagnostic = (completed.stderr or completed.stdout)[-1000:]
                return (
                    False,
                    "ICML LaTeX compilation failed: " + diagnostic.replace("\n", " "),
                    None,
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ICML LaTeX compilation failed: {exc}", None
    pdf_path = output_dir / "paper.pdf"
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        return False, "ICML LaTeX compilation produced no paper.pdf", None
    main_pages = None
    aux_path = output_dir / "paper.aux"
    if aux_path.is_file():
        match = re.search(
            r"\\newlabel\{trace:lastmainpage\}\{\{[^}]*\}\{(\d+)\}",
            aux_path.read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            main_pages = int(match.group(1))
    return True, None, main_pages


def _short_paper_footer(canvas, document) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    canvas.setFont("Times-Roman", 8)
    canvas.drawCentredString(letter[0] / 2, 0.35 * 72, str(document.page))
    canvas.restoreState()
