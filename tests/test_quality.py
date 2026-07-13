from ai_scientist.config import Settings
from ai_scientist.quality import compare_quality
from ai_scientist.schemas import CriterionScore


def score(name: str, value: int) -> CriterionScore:
    return CriterionScore(
        criterion=name,
        score=value,
        evidence_ids=["E-1"],
        reason="reason",
        counterargument="counter",
        confidence=0.8,
        missing_information=[],
    )


def test_detects_critical_dimension_drop() -> None:
    delta = compare_quality(
        [score("Falsifiability", 3), score("Importance", 4)],
        [score("Falsifiability", 5), score("Importance", 4)],
        critical_dimensions={"Falsifiability"},
        settings=Settings(),
    )
    assert delta.significant_degradation
    assert delta.critical_drops["Falsifiability"] <= -1.0


def test_first_round_has_no_degradation() -> None:
    delta = compare_quality(
        [score("Importance", 4)],
        None,
        critical_dimensions={"Importance"},
        settings=Settings(),
    )
    assert not delta.significant_degradation
    assert delta.previous_average is None
