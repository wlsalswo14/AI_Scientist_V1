from __future__ import annotations

from ..schemas import (
    EvidenceConcernResolution,
    EvidenceCriticReport,
    EvidenceGlobalAuditReport,
    ExEvaluatorReport,
    ExperimentContract,
    ExperimentorOutput,
)
from .base import StructuredAgent


class ExperimentDesignerAgent(StructuredAgent[ExperimentContract]):
    role = "experiment_designer"
    prompt_name = "experiment_designer"
    output_schema = ExperimentContract


class ExperimentorAgent(StructuredAgent[ExperimentorOutput]):
    role = "experimentor"
    prompt_name = "experimentor"
    output_schema = ExperimentorOutput


class ExEvaluatorAgent(StructuredAgent[ExEvaluatorReport]):
    role = "ex_evaluator"
    prompt_name = "ex_evaluator"
    output_schema = ExEvaluatorReport


class EvidenceCriticAgent(StructuredAgent[EvidenceCriticReport]):
    role = "evidence_critic"
    prompt_name = "evidence_critic"
    output_schema = EvidenceCriticReport


class EvidenceResolverAgent(StructuredAgent[EvidenceConcernResolution]):
    role = "evidence_resolver"
    prompt_name = "evidence_resolver"
    output_schema = EvidenceConcernResolution


class EvidenceGlobalAuditAgent(StructuredAgent[EvidenceGlobalAuditReport]):
    role = "evidence_global_audit"
    prompt_name = "evidence_global_audit"
    output_schema = EvidenceGlobalAuditReport
