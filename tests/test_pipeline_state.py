"""shared/pipeline_state 모듈 단위 테스트."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from shared.pipeline_state import (
    ALL_STAGES,
    STAGE_ANALYZE,
    STAGE_COLLECT,
    STAGE_EXECUTE,
    STAGE_PLAN,
    PipelineRun,
    StageRecord,
    load_runs,
    save_run,
)


# ── StageRecord ─────────────────────────────────────────────────────


class TestStageRecord:
    def test_default_status_is_pending(self):
        stage = StageRecord(name="analyze")
        assert stage.status == "pending"
        assert stage.started_at is None
        assert stage.completed_at is None

    def test_start(self):
        stage = StageRecord(name="analyze")
        stage.start({"input_files": 3})
        assert stage.status == "running"
        assert stage.started_at is not None
        assert stage.input_summary == {"input_files": 3}

    def test_complete(self):
        stage = StageRecord(name="analyze")
        stage.start()
        stage.complete({"topics_found": 5})
        assert stage.status == "completed"
        assert stage.completed_at is not None
        assert stage.output_summary == {"topics_found": 5}

    def test_fail(self):
        stage = StageRecord(name="analyze")
        stage.start()
        stage.fail("Connection error")
        assert stage.status == "failed"
        assert stage.completed_at is not None
        assert stage.error == "Connection error"

    def test_skip(self):
        stage = StageRecord(name="execute")
        stage.skip("Dry run mode")
        assert stage.status == "skipped"
        assert stage.completed_at is not None
        assert stage.error == "Dry run mode"

    def test_skip_without_reason(self):
        stage = StageRecord(name="execute")
        stage.skip()
        assert stage.status == "skipped"
        assert stage.error is None

    def test_duration_seconds(self):
        stage = StageRecord(name="plan")
        stage.start()
        time.sleep(0.05)
        stage.complete()
        duration = stage.duration_seconds()
        assert duration is not None
        assert duration >= 0.04

    def test_duration_returns_none_without_timestamps(self):
        stage = StageRecord(name="plan")
        assert stage.duration_seconds() is None

    def test_duration_returns_none_without_completion(self):
        stage = StageRecord(name="plan")
        stage.start()
        assert stage.duration_seconds() is None


# ── PipelineRun ─────────────────────────────────────────────────────


class TestPipelineRun:
    def test_create(self):
        run = PipelineRun.create("2026-03-01", config={"dry_run": True})
        assert run.date == "2026-03-01"
        assert run.status == "running"
        assert run.started_at != ""
        assert len(run.stages) == len(ALL_STAGES)
        assert run.config == {"dry_run": True}
        assert run.run_id  # non-empty

    def test_create_default_config(self):
        run = PipelineRun.create("2026-03-01")
        assert run.config == {}

    def test_get_stage(self):
        run = PipelineRun.create("2026-03-01")
        collect = run.get_stage(STAGE_COLLECT)
        assert collect is not None
        assert collect.name == "collect"

    def test_get_stage_returns_none_for_unknown(self):
        run = PipelineRun.create("2026-03-01")
        assert run.get_stage("nonexistent") is None

    def test_all_stages_present(self):
        run = PipelineRun.create("2026-03-01")
        for name in ALL_STAGES:
            stage = run.get_stage(name)
            assert stage is not None
            assert stage.status == "pending"

    def test_finish_completed(self):
        run = PipelineRun.create("2026-03-01")
        for stage in run.stages:
            stage.start()
            stage.complete()
        run.finish()
        assert run.status == "completed"
        assert run.completed_at is not None

    def test_finish_failed(self):
        run = PipelineRun.create("2026-03-01")
        run.get_stage(STAGE_COLLECT).start()
        run.get_stage(STAGE_COLLECT).complete()
        run.get_stage(STAGE_ANALYZE).start()
        run.get_stage(STAGE_ANALYZE).fail("Error")
        run.get_stage(STAGE_PLAN).skip("No input")
        run.get_stage(STAGE_EXECUTE).skip("No input")
        run.finish()
        assert run.status == "failed"

    def test_finish_partial(self):
        run = PipelineRun.create("2026-03-01")
        run.get_stage(STAGE_COLLECT).start()
        run.get_stage(STAGE_COLLECT).complete()
        # Other stages still pending
        run.finish()
        assert run.status == "partial"

    def test_finish_with_skips(self):
        run = PipelineRun.create("2026-03-01")
        run.get_stage(STAGE_COLLECT).start()
        run.get_stage(STAGE_COLLECT).complete()
        run.get_stage(STAGE_ANALYZE).start()
        run.get_stage(STAGE_ANALYZE).complete()
        run.get_stage(STAGE_PLAN).start()
        run.get_stage(STAGE_PLAN).complete()
        run.get_stage(STAGE_EXECUTE).skip("Dry run")
        run.finish()
        assert run.status == "completed"

    def test_to_dict(self):
        run = PipelineRun.create("2026-03-01")
        d = run.to_dict()
        assert isinstance(d, dict)
        assert d["date"] == "2026-03-01"
        assert "stages" in d
        assert len(d["stages"]) == len(ALL_STAGES)

    def test_summary_line(self):
        run = PipelineRun.create("2026-03-01")
        run.get_stage(STAGE_COLLECT).start()
        run.get_stage(STAGE_COLLECT).complete()
        run.get_stage(STAGE_ANALYZE).start()
        run.get_stage(STAGE_ANALYZE).fail("Error")
        run.get_stage(STAGE_PLAN).skip()
        run.finish()
        line = run.summary_line()
        assert run.run_id in line
        assert "2026-03-01" in line
        assert "collect[O]" in line
        assert "analyze[X]" in line
        assert "plan[-]" in line

    def test_duration_seconds(self):
        run = PipelineRun.create("2026-03-01")
        time.sleep(0.05)
        run.finish()
        duration = run.duration_seconds()
        assert duration is not None
        assert duration >= 0.04


# ── save_run / load_runs ────────────────────────────────────────────


class TestSaveAndLoadRuns:
    def test_round_trip(self, tmp_path):
        run = PipelineRun.create("2026-03-01", config={"test": True})
        run.get_stage(STAGE_COLLECT).start()
        run.get_stage(STAGE_COLLECT).complete({"files_found": 3})
        run.get_stage(STAGE_ANALYZE).start({"input_files": 3})
        run.get_stage(STAGE_ANALYZE).complete({"topics_found": 2})
        run.get_stage(STAGE_PLAN).start()
        run.get_stage(STAGE_PLAN).complete({"plans_generated": 1})
        run.get_stage(STAGE_EXECUTE).skip("Dry run")
        run.finish()

        path = save_run(run, tmp_path)
        assert path.exists()
        assert run.date in path.name
        assert run.run_id in path.name

        loaded = load_runs(tmp_path, date="2026-03-01")
        assert len(loaded) == 1
        loaded_run = loaded[0]
        assert loaded_run.run_id == run.run_id
        assert loaded_run.date == "2026-03-01"
        assert loaded_run.status == "completed"
        assert len(loaded_run.stages) == len(ALL_STAGES)

        collect = loaded_run.get_stage(STAGE_COLLECT)
        assert collect.status == "completed"
        assert collect.output_summary == {"files_found": 3}

        execute = loaded_run.get_stage(STAGE_EXECUTE)
        assert execute.status == "skipped"

    def test_load_empty_directory(self, tmp_path):
        runs = load_runs(tmp_path)
        assert runs == []

    def test_load_nonexistent_directory(self, tmp_path):
        runs = load_runs(tmp_path / "nonexistent")
        assert runs == []

    def test_date_filtering(self, tmp_path):
        run1 = PipelineRun.create("2026-03-01")
        run1.finish()
        save_run(run1, tmp_path)

        run2 = PipelineRun.create("2026-03-02")
        run2.finish()
        save_run(run2, tmp_path)

        all_runs = load_runs(tmp_path)
        assert len(all_runs) == 2

        mar01 = load_runs(tmp_path, date="2026-03-01")
        assert len(mar01) == 1
        assert mar01[0].date == "2026-03-01"

        mar03 = load_runs(tmp_path, date="2026-03-03")
        assert len(mar03) == 0

    def test_corrupt_file_skipped(self, tmp_path):
        runs_dir = tmp_path / "pipeline_runs"
        runs_dir.mkdir()
        (runs_dir / "2026-03-01_bad.json").write_text("not valid json")

        run = PipelineRun.create("2026-03-01")
        run.finish()
        save_run(run, tmp_path)

        loaded = load_runs(tmp_path, date="2026-03-01")
        assert len(loaded) == 1  # corrupt file skipped, valid file loaded

    def test_multiple_runs_same_date(self, tmp_path):
        for _ in range(3):
            run = PipelineRun.create("2026-03-01")
            run.finish()
            save_run(run, tmp_path)

        loaded = load_runs(tmp_path, date="2026-03-01")
        assert len(loaded) == 3
        run_ids = {r.run_id for r in loaded}
        assert len(run_ids) == 3  # all unique IDs
