from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Settings
from .schemas import CriterionScore


@dataclass(frozen=True, slots=True)
class QualityDelta:
    current_average: float
    previous_average: float | None
    absolute_delta: float | None
    relative_delta: float | None
    critical_drops: dict[str, float]
    significant_degradation: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_quality(
    current: list[CriterionScore],
    previous: list[CriterionScore] | None,
    *,
    critical_dimensions: set[str],
    settings: Settings,
) -> QualityDelta:
    current_map = {item.criterion: item.score for item in current}
    current_average = _average(current_map.values())
    if not previous:
        return QualityDelta(
            current_average=current_average,
            previous_average=None,
            absolute_delta=None,
            relative_delta=None,
            critical_drops={},
            significant_degradation=False,
        )
    previous_map = {item.criterion: item.score for item in previous}
    comparable = sorted(set(current_map).intersection(previous_map))
    if not comparable:
        return QualityDelta(
            current_average=current_average,
            previous_average=None,
            absolute_delta=None,
            relative_delta=None,
            critical_drops={},
            significant_degradation=False,
        )
    current_comparable = _average(current_map[name] for name in comparable)
    previous_average = _average(previous_map[name] for name in comparable)
    absolute_delta = current_comparable - previous_average
    relative_delta = (
        absolute_delta / previous_average if previous_average else None
    )
    critical_drops = {
        name: current_map[name] - previous_map[name]
        for name in comparable
        if name in critical_dimensions
        and current_map[name] - previous_map[name] <= -settings.critical_dimension_drop
    }
    significant = (
        absolute_delta <= -settings.score_drop_absolute
        or (
            relative_delta is not None
            and relative_delta <= -settings.score_drop_relative
        )
        or bool(critical_drops)
    )
    return QualityDelta(
        current_average=current_average,
        previous_average=previous_average,
        absolute_delta=absolute_delta,
        relative_delta=relative_delta,
        critical_drops=critical_drops,
        significant_degradation=significant,
    )


def _average(values) -> float:  # type: ignore[no-untyped-def]
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0

