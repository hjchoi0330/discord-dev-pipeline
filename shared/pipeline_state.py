"""Pipeline execution history management module.

Tracks each pipeline run as a single unit, recording per-stage status,
input/output summaries, and timing information.

Storage path: data/pipeline_runs/{date}_{run_id}.json
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pipeline stage name constants
STAGE_COLLECT = "collect"
STAGE_ANALYZE = "analyze"
STAGE_PLAN = "plan"
STAGE_EXECUTE = "execute"

ALL_STAGES = [STAGE_COLLECT, STAGE_ANALYZE, STAGE_PLAN, STAGE_EXECUTE]


@dataclass
class StageRecord:
    """Execution record for an individual pipeline stage."""

    name: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    started_at: str | None = None
    completed_at: str | None = None
    input_summary: dict[str, Any] = field(default_factory=dict)
    output_summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def start(self, input_summary: dict[str, Any] | None = None) -> None:
        self.status = "running"
        self.started_at = datetime.now(timezone.utc).isoformat()
        if input_summary:
            self.input_summary = input_summary

    def complete(self, output_summary: dict[str, Any] | None = None) -> None:
        self.status = "completed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if output_summary:
            self.output_summary = output_summary

    def fail(self, error: str) -> None:
        self.status = "failed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.error = error

    def skip(self, reason: str = "") -> None:
        self.status = "skipped"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if reason:
            self.error = reason

    def duration_seconds(self) -> float | None:
        """Elapsed time for the stage in seconds. Returns None if start/end info is missing."""
        if not self.started_at or not self.completed_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.completed_at)
        return (end - start).total_seconds()


@dataclass
class PipelineRun:
    """Record of an entire pipeline run."""

    run_id: str
    date: str
    status: str = "running"  # running | completed | failed | partial
    started_at: str = ""
    completed_at: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, date: str, config: dict[str, Any] | None = None) -> PipelineRun:
        """Create a new pipeline run record."""
        run_id = (
            datetime.now(timezone.utc).strftime("%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )
        return cls(
            run_id=run_id,
            date=date,
            started_at=datetime.now(timezone.utc).isoformat(),
            stages=[StageRecord(name=name) for name in ALL_STAGES],
            config=config or {},
        )

    def get_stage(self, name: str) -> StageRecord | None:
        """Look up a stage record by name."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def finish(self) -> None:
        """Finalize the pipeline run and determine its terminal status."""
        self.completed_at = datetime.now(timezone.utc).isoformat()
        has_failed = any(s.status == "failed" for s in self.stages)
        all_terminal = all(
            s.status in ("completed", "skipped") for s in self.stages
        )
        if has_failed:
            self.status = "failed"
        elif all_terminal:
            self.status = "completed"
        else:
            self.status = "partial"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_line(self) -> str:
        """Return a human-readable one-line summary."""
        marks = {"completed": "O", "failed": "X", "skipped": "-", "pending": "?", "running": "~"}
        parts = []
        for s in self.stages:
            mark = marks.get(s.status, "?")
            parts.append(f"{s.name}[{mark}]")
        return f"[{self.run_id}] {self.date} {self.status} | {' -> '.join(parts)}"

    def duration_seconds(self) -> float | None:
        """Total elapsed time for the pipeline run in seconds."""
        if not self.started_at or not self.completed_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.completed_at)
        return (end - start).total_seconds()


def _runs_dir(data_dir: Path) -> Path:
    return data_dir / "pipeline_runs"


def save_run(run: PipelineRun, data_dir: Path) -> Path:
    """Save a pipeline run record to disk."""
    runs = _runs_dir(data_dir)
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{run.date}_{run.run_id}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(run.to_dict(), f, ensure_ascii=False, indent=2, default=str)
    logger.info("Pipeline run saved: %s", path)
    return path


def load_runs(data_dir: Path, date: str | None = None) -> list[PipelineRun]:
    """Load pipeline run records from disk.

    Args:
        data_dir: Root data directory.
        date: Filter to a specific date (YYYY-MM-DD). None loads all.

    Returns:
        List of PipelineRun objects sorted by time.
    """
    runs_path = _runs_dir(data_dir)
    if not runs_path.exists():
        return []

    pattern = f"{date}_*.json" if date else "*.json"
    records: list[PipelineRun] = []
    for p in sorted(runs_path.glob(pattern)):
        try:
            with p.open(encoding="utf-8") as f:
                data = json.load(f)
            stages = [StageRecord(**s) for s in data.pop("stages", [])]
            records.append(PipelineRun(**data, stages=stages))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("Failed to load pipeline run %s: %s", p.name, e)
    return records
