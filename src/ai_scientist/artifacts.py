from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


class ArtifactStore:
    """Append-only local artifact store with simple dependency metadata."""

    def __init__(self, runs_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self.run_dir = runs_dir / run_id
        self.artifacts_dir = self.run_dir / "artifacts"
        self.checkpoints_dir = self.run_dir / "checkpoints"
        self.experiments_dir = self.run_dir / "experiments"
        self.paper_dir = self.run_dir / "paper"
        for directory in (
            self.artifacts_dir,
            self.checkpoints_dir,
            self.experiments_dir,
            self.paper_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.status_path = self.run_dir / "artifact_status.json"
        self._artifact_ids = self._discover_artifact_ids()

    @property
    def artifact_ids(self) -> list[str]:
        return list(self._artifact_ids)

    @property
    def valid_artifact_ids(self) -> list[str]:
        statuses = self._load_statuses()
        return [
            artifact_id
            for artifact_id in self._artifact_ids
            if statuses.get(artifact_id, {}).get("status", "VALID") == "VALID"
        ]

    def save(
        self,
        kind: str,
        value: BaseModel | dict[str, Any] | list[Any],
        *,
        dependencies: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        artifact_id = f"{kind}:{uuid4().hex[:12]}"
        payload = self._serialize(value)
        envelope = {
            "artifact_id": artifact_id,
            "kind": kind,
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dependencies": dependencies or [],
            "metadata": metadata or {},
            "status": "VALID",
            "payload": payload,
        }
        path = self.artifacts_dir / f"{artifact_id.replace(':', '_')}.json"
        self._write_json(path, envelope)
        self._artifact_ids.append(artifact_id)
        statuses = self._load_statuses()
        statuses[artifact_id] = {
            "status": "VALID",
            "reason": None,
            "updated_at": envelope["created_at"],
        }
        self._write_json(self.status_path, statuses)
        self.event("artifact.saved", {"artifact_id": artifact_id, "kind": kind})
        return artifact_id

    def checkpoint(self, stage: str, value: BaseModel | dict[str, Any]) -> Path:
        payload = self._serialize(value)
        path = self.checkpoints_dir / f"{stage}.json"
        self._write_json(path, payload)
        self.event("checkpoint.saved", {"stage": stage, "path": str(path)})
        return path

    def event(self, event_type: str, data: dict[str, Any]) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "data": data,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_manifest(self, value: BaseModel | dict[str, Any]) -> Path:
        payload = self._serialize(value)
        path = self.run_dir / "manifest.json"
        self._write_json(path, payload)
        return path

    def load_checkpoint(self, stage: str, model: type[ModelT]) -> ModelT | None:
        path = self.checkpoints_dir / f"{stage}.json"
        if not path.exists():
            return None
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.event(
                "checkpoint.invalid",
                {"stage": stage, "path": str(path)},
            )
            return None

    def latest_envelope(self, kind: str, *, valid_only: bool = True) -> dict[str, Any] | None:
        statuses = self._load_statuses()
        candidates: list[dict[str, Any]] = []
        for path in self.artifacts_dir.glob(f"{kind.replace(':', '_')}_*.json"):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if envelope.get("kind") != kind:
                continue
            artifact_id = envelope.get("artifact_id", "")
            status = statuses.get(artifact_id, {}).get(
                "status", envelope.get("status", "VALID")
            )
            if valid_only and status != "VALID":
                continue
            envelope["projected_status"] = status
            candidates.append(envelope)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.get("created_at", ""))

    def artifact_envelope(self, artifact_id: str) -> dict[str, Any] | None:
        path = self.artifacts_dir / f"{artifact_id.replace(':', '_')}.json"
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        envelope["projected_status"] = self.status_of(artifact_id)
        return envelope

    def status_of(self, artifact_id: str) -> str:
        statuses = self._load_statuses()
        return statuses.get(artifact_id, {}).get("status", "VALID")

    def invalidate(
        self,
        artifact_ids: list[str] | set[str],
        *,
        reason: str,
        cascade: bool = True,
    ) -> list[str]:
        """Mark immutable artifacts stale in a separate status projection."""

        requested = {item for item in artifact_ids if item in self._artifact_ids}
        if not requested:
            return []
        all_envelopes = {
            artifact_id: self.artifact_envelope(artifact_id)
            for artifact_id in self._artifact_ids
        }
        affected = set(requested)
        if cascade:
            changed = True
            while changed:
                changed = False
                for artifact_id, envelope in all_envelopes.items():
                    if artifact_id in affected or envelope is None:
                        continue
                    dependencies = set(envelope.get("dependencies", []))
                    if dependencies.intersection(affected):
                        affected.add(artifact_id)
                        changed = True
        statuses = self._load_statuses()
        now = datetime.now(timezone.utc).isoformat()
        newly_stale: list[str] = []
        for artifact_id in sorted(affected):
            if statuses.get(artifact_id, {}).get("status") == "STALE":
                continue
            statuses[artifact_id] = {
                "status": "STALE",
                "reason": reason,
                "updated_at": now,
            }
            newly_stale.append(artifact_id)
        if newly_stale:
            self._write_json(self.status_path, statuses)
            self.event(
                "artifact.invalidated",
                {
                    "root_artifact_ids": sorted(requested),
                    "stale_artifact_ids": newly_stale,
                    "reason": reason,
                },
            )
        return newly_stale

    def find_artifact_ids(
        self,
        *,
        kind: str | None = None,
        metadata: dict[str, Any] | None = None,
        valid_only: bool = True,
    ) -> list[str]:
        found: list[str] = []
        for artifact_id in self._artifact_ids:
            if valid_only and self.status_of(artifact_id) != "VALID":
                continue
            envelope = self.artifact_envelope(artifact_id)
            if envelope is None:
                continue
            if kind is not None and envelope.get("kind") != kind:
                continue
            actual_metadata = envelope.get("metadata", {})
            if metadata and any(
                actual_metadata.get(key) != value for key, value in metadata.items()
            ):
                continue
            found.append(artifact_id)
        return found

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(
                mode="json",
                exclude_computed_fields=True,
            )
        return value

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _discover_artifact_ids(self) -> list[str]:
        discovered: list[tuple[str, str]] = []
        for path in self.artifacts_dir.glob("*.json"):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            artifact_id = envelope.get("artifact_id")
            if isinstance(artifact_id, str):
                discovered.append((envelope.get("created_at", ""), artifact_id))
        return [item[1] for item in sorted(discovered)]

    def _load_statuses(self) -> dict[str, dict[str, Any]]:
        if not self.status_path.exists():
            return {}
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
