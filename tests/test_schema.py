from __future__ import annotations

from typing import Any

from ai_scientist.llm import _compact_diagnostic, _strict_json_schema
from ai_scientist.schemas import (
    DirectorOutput,
    EvaluatorReport,
    ExEvaluatorReport,
    PaperDraft,
    ResearchContract,
    ResearchModeAssessment,
    ReviewReport,
)


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_strict_schema_requires_every_property() -> None:
    document = _strict_json_schema(DirectorOutput)
    for node in walk(document):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            assert set(node["required"]) == set(node["properties"])
            assert node["additionalProperties"] is False


def test_ex_evaluator_schema_has_no_dynamic_object_map() -> None:
    document = _strict_json_schema(ExEvaluatorReport)
    for node in walk(document):
        if isinstance(node, dict) and "additionalProperties" in node:
            assert node["additionalProperties"] is False


def test_research_routing_schemas_are_strict() -> None:
    for schema in (
        ResearchModeAssessment,
        ResearchContract,
        EvaluatorReport,
        ExEvaluatorReport,
        PaperDraft,
        ReviewReport,
    ):
        document = _strict_json_schema(schema)
        for node in walk(document):
            if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False


def test_strict_schema_removes_defaults_that_conflict_with_refs() -> None:
    document = _strict_json_schema(ResearchModeAssessment)

    assert all(
        "default" not in node
        for node in walk(document)
        if isinstance(node, dict)
    )


def test_compact_diagnostic_extracts_nested_provider_message() -> None:
    raw = '''
user
private prompt contents
ERROR: {
  "error": {
    "code": "invalid_json_schema",
    "message": "A $ref cannot have a default."
  }
}
'''

    diagnostic = _compact_diagnostic(raw)

    assert diagnostic == "invalid_json_schema: A $ref cannot have a default."
    assert "private prompt" not in diagnostic
