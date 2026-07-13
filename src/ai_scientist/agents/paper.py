from __future__ import annotations

from ..schemas import PaperDraft, ReviewReport
from .base import StructuredAgent


class WriterAgent(StructuredAgent[PaperDraft]):
    role = "writer"
    prompt_name = "writer"
    output_schema = PaperDraft


class ReviewerAgent(StructuredAgent[ReviewReport]):
    role = "reviewer"
    prompt_name = "reviewer"
    output_schema = ReviewReport

