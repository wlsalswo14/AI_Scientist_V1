from __future__ import annotations

import hashlib
import json

from .artifacts import ArtifactStore
from .schemas import (
    ClaimLedger,
    ExperimentStageResult,
    ProvenanceEdge,
    ProvenanceGraph,
    ProvenanceNode,
)


def _hash(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_provenance_graph(
    store: ArtifactStore,
    ledger: ClaimLedger,
    experiment_stage: ExperimentStageResult,
) -> ProvenanceGraph:
    nodes: dict[str, ProvenanceNode] = {}
    edges: set[tuple[str, str, str]] = set()

    for artifact_id in store.artifact_ids:
        envelope = store.artifact_envelope(artifact_id)
        if envelope is None:
            continue
        nodes[artifact_id] = ProvenanceNode(
            node_id=artifact_id,
            node_type=str(envelope.get("kind", "artifact")),
            status=store.status_of(artifact_id),
            content_hash=_hash(envelope.get("payload")),
        )
        for dependency in envelope.get("dependencies", []):
            edges.add((dependency, artifact_id, "DEPENDS_ON"))

    executions_by_result = {
        result_id: execution
        for execution in experiment_stage.executions
        for result_id in execution.result_ids
    }
    for execution in experiment_stage.executions:
        experiment_node = f"experiment:{execution.experiment_id}"
        code_node = f"code:{execution.code_hash}"
        nodes[experiment_node] = ProvenanceNode(
            node_id=experiment_node,
            node_type="experiment",
            status="VALID" if execution.exit_code == 0 and not execution.timed_out else "FAILED",
            content_hash=_hash(execution.experiment_id),
        )
        nodes[code_node] = ProvenanceNode(
            node_id=code_node,
            node_type="code_hash",
            status="VALID",
            content_hash=execution.code_hash,
        )
        edges.add((experiment_node, code_node, "EXECUTED_CODE"))
        for result_id in execution.result_ids:
            result_node = f"result:{result_id}"
            nodes[result_node] = ProvenanceNode(
                node_id=result_node,
                node_type="result",
                status="VALID",
                content_hash=_hash(
                    execution.output_files.get(result_id.split(":", 1)[-1], "")
                ),
            )
            edges.add((result_node, experiment_node, "PRODUCED_BY"))

    for entry in ledger.entries:
        claim_node = f"claim:{entry.claim_id}"
        nodes[claim_node] = ProvenanceNode(
            node_id=claim_node,
            node_type="paper_claim",
            status="VALID",
            content_hash=_hash(entry.allowed_claim),
        )
        for evidence_id in entry.evidence_ids:
            evidence_node = f"evidence:{evidence_id}"
            nodes.setdefault(
                evidence_node,
                ProvenanceNode(
                    node_id=evidence_node,
                    node_type="evidence",
                    status="VALID",
                    content_hash=_hash(evidence_id),
                ),
            )
            edges.add((claim_node, evidence_node, "SUPPORTED_BY"))
        for result_id in entry.result_ids:
            result_node = f"result:{result_id}"
            execution = executions_by_result.get(result_id)
            if execution is not None:
                nodes.setdefault(
                    result_node,
                    ProvenanceNode(
                        node_id=result_node,
                        node_type="result",
                        status="VALID",
                        content_hash=_hash(result_id),
                    ),
                )
            edges.add((claim_node, result_node, "MEASURED_BY"))

    return ProvenanceGraph(
        graph_version="1.0",
        run_id=store.run_id,
        root_claim_ids=[f"claim:{item.claim_id}" for item in ledger.entries],
        nodes=list(nodes.values()),
        edges=[
            ProvenanceEdge(source_id=source, target_id=target, relation=relation)
            for source, target, relation in sorted(edges)
        ],
    )


def provenance_graph_issues(
    graph: ProvenanceGraph,
    ledger: ClaimLedger,
) -> list[str]:
    issues: list[str] = []
    node_ids = [item.node_id for item in graph.nodes]
    if len(node_ids) != len(set(node_ids)):
        issues.append("Provenance graph node IDs must be unique")
    known = set(node_ids)
    for edge in graph.edges:
        if edge.source_id not in known or edge.target_id not in known:
            issues.append(
                f"Provenance edge {edge.relation} references an unknown node"
            )
    expected_roots = {f"claim:{item.claim_id}" for item in ledger.entries}
    if set(graph.root_claim_ids) != expected_roots:
        issues.append("Provenance roots must equal Claim Ledger entries")
    edge_set = {
        (item.source_id, item.target_id, item.relation) for item in graph.edges
    }
    for entry in ledger.entries:
        claim_node = f"claim:{entry.claim_id}"
        for evidence_id in entry.evidence_ids:
            if (claim_node, f"evidence:{evidence_id}", "SUPPORTED_BY") not in edge_set:
                issues.append(
                    f"Claim {entry.claim_id} is missing Evidence edge {evidence_id}"
                )
        for result_id in entry.result_ids:
            result_node = f"result:{result_id}"
            if (claim_node, result_node, "MEASURED_BY") not in edge_set:
                issues.append(
                    f"Claim {entry.claim_id} is missing Result edge {result_id}"
                )
            producers = [
                edge for edge in graph.edges
                if edge.source_id == result_node and edge.relation == "PRODUCED_BY"
            ]
            if len(producers) != 1:
                issues.append(
                    f"Result {result_id} must resolve to exactly one experiment"
                )
    stale_claim_dependencies = [
        node.node_id
        for node in graph.nodes
        if node.status == "STALE" and node.node_type in {"execution-result", "research-contract-final"}
    ]
    if stale_claim_dependencies:
        issues.append(
            "Provenance graph contains stale scientific roots: "
            + ", ".join(stale_claim_dependencies)
        )
    return list(dict.fromkeys(issues))
