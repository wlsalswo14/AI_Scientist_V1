from __future__ import annotations

from ..schemas import (
    ClaimDirectorOutput,
    ClaimEvaluatorReport,
    ContractComposerReport,
    EvaluatorReport,
    ResearchContract,
    ResearchModeAssessment,
    ResearchProgramComposition,
)
from .base import StructuredAgent


class ResearchModeDirectorAgent(StructuredAgent[ResearchModeAssessment]):
    role = "research_mode_director"
    prompt_name = "research_mode_director"
    output_schema = ResearchModeAssessment


class DirectStudyDirectorAgent(StructuredAgent[ResearchContract]):
    role = "direct_study_director"
    prompt_name = "direct_study_director"
    output_schema = ResearchContract
    tools = [{"type": "web_search"}]


class ContractEvaluatorAAgent(StructuredAgent[EvaluatorReport]):
    role = "contract_evaluator_a"
    prompt_name = "contract_evaluator_a"
    output_schema = EvaluatorReport
    tools = [{"type": "web_search"}]


class ContractEvaluatorBAgent(StructuredAgent[EvaluatorReport]):
    role = "contract_evaluator_b"
    prompt_name = "contract_evaluator_b"
    output_schema = EvaluatorReport


class ContractComposerAgent(StructuredAgent[ContractComposerReport]):
    role = "contract_composer"
    prompt_name = "contract_composer"
    output_schema = ContractComposerReport


class AnchorDirectorAgent(StructuredAgent[ClaimDirectorOutput]):
    role = "anchor_director"
    prompt_name = "anchor_director"
    output_schema = ClaimDirectorOutput
    tools = [{"type": "web_search"}]


class ExpansionDirectorAgent(StructuredAgent[ClaimDirectorOutput]):
    role = "expansion_director"
    prompt_name = "expansion_director"
    output_schema = ClaimDirectorOutput
    tools = [{"type": "web_search"}]


class ProgramEvaluatorAAgent(StructuredAgent[ClaimEvaluatorReport]):
    role = "program_evaluator_a"
    prompt_name = "program_evaluator_a"
    output_schema = ClaimEvaluatorReport
    tools = [{"type": "web_search"}]


class ProgramEvaluatorBAgent(StructuredAgent[ClaimEvaluatorReport]):
    role = "program_evaluator_b"
    prompt_name = "program_evaluator_b"
    output_schema = ClaimEvaluatorReport


class ResearchProgramComposerAgent(StructuredAgent[ResearchProgramComposition]):
    role = "research_program_composer"
    prompt_name = "research_program_composer"
    output_schema = ResearchProgramComposition
