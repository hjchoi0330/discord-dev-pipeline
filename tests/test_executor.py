"""executor 모듈 단위 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from executor.executor import (
    ExecutionResult,
    _extract_prompt,
    _find_claude_binary,
    _load_executed_manifest,
    _mark_plan_executed,
    execute_plan,
    execute_plans,
)


# ── _extract_prompt ──────────────────────────────────────────────────


class TestExtractPrompt:
    def test_extracts_prompt_section(self):
        plan = """# Plan: Add Auth

## Context
Some context here.

## Claude Code Prompt
Implement the following feature:

Add OAuth2 login support with Google provider.

## Another Section
This should not be included.
"""
        result = _extract_prompt(plan)
        assert "Implement the following feature" in result
        assert "OAuth2" in result
        assert "Another Section" not in result

    def test_prompt_at_end_of_file(self):
        plan = """# Plan

## Claude Code Prompt
Do something cool.
"""
        result = _extract_prompt(plan)
        assert "Do something cool" in result

    def test_no_prompt_section(self):
        plan = """# Plan
## Context
No prompt here.
"""
        assert _extract_prompt(plan) == ""

    def test_empty_input(self):
        assert _extract_prompt("") == ""


# ── _find_claude_binary ──────────────────────────────────────────────


class TestFindClaudeBinary:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_found_in_path(self, mock_which):
        assert _find_claude_binary() == "/usr/local/bin/claude"

    @patch("shutil.which", return_value=None)
    @patch("pathlib.Path.exists", return_value=False)
    def test_not_found(self, mock_exists, mock_which):
        assert _find_claude_binary("nonexistent") is None


# ── ExecutionResult ──────────────────────────────────────────────────


class TestExecutionResult:
    def test_to_dict(self):
        r = ExecutionResult(
            plan_file="plan.md",
            prompt_used="do something",
            claude_output="done",
            success=True,
            error="",
        )
        d = r.to_dict()
        assert d["plan_file"] == "plan.md"
        assert d["success"] is True
        assert "timestamp" in d

    def test_default_timestamp(self):
        r = ExecutionResult(
            plan_file="p.md", prompt_used="", claude_output="",
            success=False, error="err",
        )
        assert r.timestamp  # 기본값이 자동 생성됨


# ── execute_plan ─────────────────────────────────────────────────────


class TestExecutePlan:
    def test_missing_file(self, tmp_path):
        result = execute_plan(
            tmp_path / "nonexistent.md",
            data_dir=tmp_path / "data",
        )
        assert not result.success
        assert "not found" in result.error

    def test_no_prompt_section(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n## Context\nNo prompt here.\n")
        result = execute_plan(plan, data_dir=tmp_path / "data")
        assert not result.success
        assert "No '## Claude Code Prompt'" in result.error

    def test_dry_run(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nImplement auth.\n")
        result = execute_plan(plan, dry_run=True, data_dir=tmp_path / "data")
        assert result.success
        assert "dry run" in result.claude_output

    @patch("executor.executor._find_claude_binary", return_value=None)
    def test_claude_not_found_saves_pending(self, mock_find, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nImplement auth.\n")
        result = execute_plan(plan, data_dir=tmp_path / "data")
        assert not result.success
        assert "not found" in result.error
        # pending 파일 확인
        pending_dir = tmp_path / "data" / "pending_executions"
        assert pending_dir.exists()
        pending_files = list(pending_dir.glob("*.txt"))
        assert len(pending_files) == 1

    @patch("executor.executor._find_claude_binary", return_value="/usr/bin/claude")
    @patch("subprocess.run")
    def test_successful_execution(self, mock_run, mock_find, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Implementation done.", stderr=""
        )
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nImplement auth.\n")

        result = execute_plan(plan, data_dir=tmp_path / "data")
        assert result.success
        assert "Implementation done" in result.claude_output

    @patch("executor.executor._find_claude_binary", return_value="/usr/bin/claude")
    @patch("subprocess.run")
    def test_failed_execution(self, mock_run, mock_find, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error occurred"
        )
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nBad prompt.\n")

        result = execute_plan(plan, data_dir=tmp_path / "data")
        assert not result.success
        assert "Error occurred" in result.error

    @patch("executor.executor._find_claude_binary", return_value="/usr/bin/claude")
    @patch("subprocess.run", side_effect=TimeoutError)
    def test_timeout(self, mock_run, mock_find, tmp_path):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)

        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nSlow task.\n")

        result = execute_plan(plan, data_dir=tmp_path / "data")
        assert not result.success
        assert "timed out" in result.error

    def test_execution_log_saved(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Claude Code Prompt\nDo it.\n")
        execute_plan(plan, dry_run=True, data_dir=tmp_path / "data")

        log_dir = tmp_path / "data" / "executions"
        assert log_dir.exists()
        log_files = list(log_dir.glob("*_execution_log.json"))
        assert len(log_files) == 1
        records = json.loads(log_files[0].read_text())
        assert len(records) == 1
        assert records[0]["success"] is True


# ── execute_plans ────────────────────────────────────────────────────


class TestExecutePlans:
    def test_empty_list(self):
        assert execute_plans([]) == []

    def test_multiple_plans_dry_run(self, tmp_path):
        plans = []
        for i in range(3):
            p = tmp_path / f"plan{i}.md"
            p.write_text(f"# Plan {i}\n\n## Claude Code Prompt\nTask {i}.\n")
            plans.append(p)

        results = execute_plans(plans, dry_run=True, data_dir=tmp_path / "data")
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_skips_already_executed(self, tmp_path):
        """Plans recorded in the manifest should be skipped."""
        data_dir = tmp_path / "data"
        meta_dir = data_dir / "meta"
        meta_dir.mkdir(parents=True)

        p1 = tmp_path / "plan_a.md"
        p2 = tmp_path / "plan_b.md"
        p1.write_text("# A\n\n## Claude Code Prompt\nTask A.\n")
        p2.write_text("# B\n\n## Claude Code Prompt\nTask B.\n")

        # Mark plan_a as already executed
        manifest = {"plan_a.md": {"executed_at": "2026-01-01T00:00:00Z", "plan_path": str(p1)}}
        (meta_dir / "executed.json").write_text(json.dumps(manifest))

        results = execute_plans([p1, p2], dry_run=True, data_dir=data_dir)
        # Only plan_b should be executed
        assert len(results) == 1
        assert "plan_b.md" in results[0].plan_file

    def test_force_re_executes(self, tmp_path):
        """With force=True, already-executed plans should run again."""
        data_dir = tmp_path / "data"
        meta_dir = data_dir / "meta"
        meta_dir.mkdir(parents=True)

        p1 = tmp_path / "plan_a.md"
        p1.write_text("# A\n\n## Claude Code Prompt\nTask A.\n")

        # Mark plan_a as already executed
        manifest = {"plan_a.md": {"executed_at": "2026-01-01T00:00:00Z", "plan_path": str(p1)}}
        (meta_dir / "executed.json").write_text(json.dumps(manifest))

        results = execute_plans([p1], dry_run=True, data_dir=data_dir, force=True)
        assert len(results) == 1
        assert results[0].success

    def test_all_executed_returns_empty(self, tmp_path):
        """When all plans are already executed, return empty list."""
        data_dir = tmp_path / "data"
        meta_dir = data_dir / "meta"
        meta_dir.mkdir(parents=True)

        p1 = tmp_path / "plan_a.md"
        p1.write_text("# A\n\n## Claude Code Prompt\nTask A.\n")

        manifest = {"plan_a.md": {"executed_at": "2026-01-01T00:00:00Z", "plan_path": str(p1)}}
        (meta_dir / "executed.json").write_text(json.dumps(manifest))

        results = execute_plans([p1], dry_run=True, data_dir=data_dir)
        assert results == []


# ── Execution manifest ──────────────────────────────────────────────


class TestExecutionManifest:
    def test_load_empty_manifest(self, tmp_path):
        assert _load_executed_manifest(tmp_path) == {}

    def test_mark_and_load(self, tmp_path):
        data_dir = tmp_path / "data"
        plan_file = tmp_path / "my-plan.md"
        plan_file.write_text("# Plan")

        _mark_plan_executed(plan_file, data_dir)

        manifest = _load_executed_manifest(data_dir)
        assert "my-plan.md" in manifest
        assert "executed_at" in manifest["my-plan.md"]

    def test_manifest_preserves_previous_entries(self, tmp_path):
        data_dir = tmp_path / "data"
        p1 = tmp_path / "plan1.md"
        p2 = tmp_path / "plan2.md"
        p1.write_text("# 1")
        p2.write_text("# 2")

        _mark_plan_executed(p1, data_dir)
        _mark_plan_executed(p2, data_dir)

        manifest = _load_executed_manifest(data_dir)
        assert "plan1.md" in manifest
        assert "plan2.md" in manifest

    def test_corrupt_manifest_returns_empty(self, tmp_path):
        data_dir = tmp_path
        meta_dir = data_dir / "meta"
        meta_dir.mkdir(parents=True)
        (meta_dir / "executed.json").write_text("not valid json")

        assert _load_executed_manifest(data_dir) == {}

    @patch("executor.executor._find_claude_binary", return_value="/usr/bin/claude")
    @patch("subprocess.run")
    def test_successful_execution_marks_manifest(self, mock_run, mock_find, tmp_path):
        """A successful execute_plan should record in the manifest."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Done.", stderr=""
        )
        data_dir = tmp_path / "data"
        plan = tmp_path / "plan_x.md"
        plan.write_text("# X\n\n## Claude Code Prompt\nDo X.\n")

        result = execute_plan(plan, data_dir=data_dir)
        assert result.success

        manifest = _load_executed_manifest(data_dir)
        assert "plan_x.md" in manifest

    def test_failed_execution_not_in_manifest(self, tmp_path):
        """A failed execute_plan should NOT record in the manifest."""
        data_dir = tmp_path / "data"
        plan = tmp_path / "bad.md"
        plan.write_text("# Bad\n## Context\nNo prompt section.\n")

        execute_plan(plan, data_dir=data_dir)

        manifest = _load_executed_manifest(data_dir)
        assert "bad.md" not in manifest
