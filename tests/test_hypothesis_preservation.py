from ai_scientist.schemas import DirectorOutput, Hypothesis
from ai_scientist.workflows.hypothesis import _preserve_locked_hypotheses


def hypothesis(hypothesis_id: str, statement: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        tension_id="T1",
        mechanism="mechanism",
        nearest_work_difference="difference",
        knowledge_change="knowledge",
        decision_change="decision",
        distinctive_prediction="prediction",
        falsification_condition="falsification",
        alternative_explanations=[],
        positive_result_value="positive",
        negative_result_value="negative",
        null_result_value="null",
        minimum_experiment="experiment",
        required_data="data",
        compute_estimate="compute",
        uncertainties=[],
        evidence_ids=[],
    )


def test_locked_hypothesis_survives_revision_verbatim() -> None:
    old_h1 = hypothesis("H1", "passed hypothesis")
    old_h2 = hypothesis("H2", "failed hypothesis")
    rewritten_h1 = hypothesis("H1", "should not replace locked version")
    new_h2 = hypothesis("H2", "revised failed hypothesis")
    previous = DirectorOutput.model_construct(
        hypotheses=[old_h1, old_h2], evidence=[], tensions=[]
    )
    current = DirectorOutput.model_construct(
        hypotheses=[rewritten_h1, new_h2], evidence=[], tensions=[]
    )

    merged = _preserve_locked_hypotheses(previous, current, {"H1"})

    by_id = {item.hypothesis_id: item for item in merged.hypotheses}
    assert by_id["H1"] == old_h1
    assert by_id["H2"] == new_h2

