from __future__ import annotations

from ..schemas import ComposerReport, DirectorOutput, EvaluatorReport
from .base import StructuredAgent


class DirectorAgent(StructuredAgent[DirectorOutput]):
    role = "director"
    prompt_name = "director"
    output_schema = DirectorOutput
    tools = [{"type": "web_search"}]


class EvaluatorAAgent(StructuredAgent[EvaluatorReport]):
    role = "evaluator_a"
    prompt_name = "evaluator_a"
    output_schema = EvaluatorReport
    tools = [{"type": "web_search"}]


class EvaluatorBAgent(StructuredAgent[EvaluatorReport]):
    role = "evaluator_b"
    prompt_name = "evaluator_b"
    output_schema = EvaluatorReport


class ComposerAgent(StructuredAgent[ComposerReport]):
    role = "composer"
    prompt_name = "composer"
    output_schema = ComposerReport

