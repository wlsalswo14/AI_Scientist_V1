from __future__ import annotations

from ..schemas import TraceBenchmarkPlan, TraceCorruptionPlan
from .base import StructuredAgent


class TraceBenchmarkCuratorAgent(StructuredAgent[TraceBenchmarkPlan]):
    role = "trace_benchmark_curator"
    prompt_name = "trace_benchmark_curator"
    output_schema = TraceBenchmarkPlan


class TraceFaultInjectorAgent(StructuredAgent[TraceCorruptionPlan]):
    role = "trace_fault_injector"
    prompt_name = "trace_fault_injector"
    output_schema = TraceCorruptionPlan
