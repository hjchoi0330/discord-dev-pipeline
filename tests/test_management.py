"""Tests for collector.management — ManagementCog helpers and Discord commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from collector.management import (
    ManagementCog,
    _extract_plan_title,
    _load_json,
    _parse_topic_response,
    _save_json,
    _truncate,
    _validate_date,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ── Helper function tests ─────────────────────────────────────────


class TestLoadJson:
    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        assert _load_json(p) == {"key": "value"}

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _load_json(tmp_path / "nope.json") is None

    def test_corrupt_file(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{broken json")
        assert _load_json(p) is None


class TestSaveJson:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "data.json"
        _save_json(p, {"x": 1})
        assert json.loads(p.read_text()) == {"x": 1}


class TestTruncate:
    def test_short_text(self) -> None:
        assert _truncate("hello", 100) == "hello"

    def test_long_text(self) -> None:
        result = _truncate("a" * 2000, 100)
        assert len(result) <= 105  # 100 + len("\n...")
        assert result.endswith("...")


class TestExtractPlanTitle:
    def test_with_title(self, tmp_path: Path) -> None:
        p = tmp_path / "plan.md"
        p.write_text("# Plan: My Cool Feature\n\nSome content")
        assert _extract_plan_title(p) == "My Cool Feature"

    def test_without_title(self, tmp_path: Path) -> None:
        p = tmp_path / "plan.md"
        p.write_text("No heading here")
        assert _extract_plan_title(p) == "plan"

    def test_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nope.md"
        assert _extract_plan_title(p) == "nope"


# ── ManagementCog._resolve_plan_file tests ─────────────────────────


class TestResolvePlanFile:
    def _make_cog(self, data_dir: Path) -> ManagementCog:
        bot = MagicMock()
        return ManagementCog(bot, data_dir)

    def test_exact_match_with_md(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat.md").write_text("# Plan: Feat")

        cog = self._make_cog(tmp_path)
        result = cog._resolve_plan_file("2026-03-01_feat.md")
        assert result is not None
        assert result.name == "2026-03-01_feat.md"

    def test_exact_match_without_md(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat.md").write_text("# Plan: Feat")

        cog = self._make_cog(tmp_path)
        result = cog._resolve_plan_file("2026-03-01_feat")
        assert result is not None
        assert result.name == "2026-03-01_feat.md"

    def test_partial_match_unique(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_docker-setup.md").write_text("")
        (plans / "2026-03-01_redis-cache.md").write_text("")

        cog = self._make_cog(tmp_path)
        result = cog._resolve_plan_file("docker")
        assert result is not None
        assert "docker" in result.name

    def test_partial_match_ambiguous(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat-a.md").write_text("")
        (plans / "2026-03-01_feat-b.md").write_text("")

        cog = self._make_cog(tmp_path)
        assert cog._resolve_plan_file("feat") is None

    def test_no_plans_dir(self, tmp_path: Path) -> None:
        cog = self._make_cog(tmp_path)
        assert cog._resolve_plan_file("anything") is None


# ── Command integration tests (using mock ctx) ────────────────────


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = "TestUser#1234"
    ctx.invoked_subcommand = None
    return ctx


class TestAnalysisListCommand:
    def test_no_data(self, tmp_path: Path) -> None:
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_list.callback(cog, ctx))
        ctx.send.assert_called_once()
        assert "No analysis" in ctx.send.call_args[0][0]

    def test_with_data(self, tmp_path: Path) -> None:
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        data = {
            "date": "2026-03-01",
            "dev_topics_found": 3,
            "total_messages_analyzed": 42,
            "dev_topics": [
                {"actionable": True},
                {"actionable": True},
                {"actionable": False},
            ],
        }
        (analysis_dir / "2026-03-01_analysis.json").write_text(json.dumps(data))

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_list.callback(cog, ctx))

        # Now uses embed
        embed = ctx.send.call_args[1]["embed"]
        assert isinstance(embed, discord.Embed)
        # Check the embed contains the date info
        field_values = " ".join(f.value for f in embed.fields)
        field_names = " ".join(f.name for f in embed.fields)
        assert "2026-03-01" in field_names
        assert "3 topic(s)" in field_values
        assert "2 actionable" in field_values


class TestAnalysisResetCommand:
    def test_reset_deletes_file(self, tmp_path: Path) -> None:
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        target = analysis_dir / "2026-03-01_analysis.json"
        target.write_text("{}")

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_reset.callback(cog, ctx, date="2026-03-01"))

        assert not target.exists()
        assert "deleted" in ctx.send.call_args[0][0].lower()

    def test_reset_missing_file(self, tmp_path: Path) -> None:
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_reset.callback(cog, ctx, date="2099-01-01"))
        assert "No analysis" in ctx.send.call_args[0][0]


class TestPlanListCommand:
    def test_with_plans(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat.md").write_text("# Plan: Feature A")
        (plans / "2026-03-01_bug.md").write_text("# Plan: Bug Fix B")

        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "executed.json").write_text(
            json.dumps({"2026-03-01_feat.md": {"executed_at": "2026-03-01T12:00:00"}})
        )

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.plan_list.callback(cog, ctx))

        # Now uses embed
        embed = ctx.send.call_args[1]["embed"]
        assert isinstance(embed, discord.Embed)
        field_names = " ".join(f.name for f in embed.fields)
        assert "Feature A" in field_names
        assert "Bug Fix B" in field_names
        assert "\u2705" in field_names  # executed
        assert "\u23f3" in field_names  # pending


class TestPlanResetCommand:
    def test_reset_removes_from_manifest(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat.md").write_text("# Plan: Feat")

        meta = tmp_path / "meta"
        meta.mkdir()
        manifest = {"2026-03-01_feat.md": {"executed_at": "2026-03-01T12:00:00"}}
        (meta / "executed.json").write_text(json.dumps(manifest))

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.plan_reset.callback(cog, ctx, name="2026-03-01_feat"))

        updated = json.loads((meta / "executed.json").read_text())
        assert "2026-03-01_feat.md" not in updated
        assert "reset" in ctx.send.call_args[0][0].lower()


class TestValidateDate:
    def test_valid_date(self) -> None:
        assert _validate_date("2026-03-20") is True

    def test_invalid_traversal(self) -> None:
        assert _validate_date("../../etc/passwd") is False

    def test_invalid_format(self) -> None:
        assert _validate_date("20260320") is False

    def test_empty_string(self) -> None:
        assert _validate_date("") is False


class TestAnalysisShowInvalidDate:
    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_show.callback(cog, ctx, date="../../etc/passwd"))
        msg = ctx.send.call_args[0][0]
        assert "Invalid date format" in msg

    def test_path_traversal_reset_rejected(self, tmp_path: Path) -> None:
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.analysis_reset.callback(cog, ctx, date="../../etc/passwd"))
        msg = ctx.send.call_args[0][0]
        assert "Invalid date format" in msg


class TestResolvePlanFileTraversal:
    def _make_cog(self, data_dir: Path) -> ManagementCog:
        bot = MagicMock()
        return ManagementCog(bot, data_dir)

    def test_path_traversal_returns_none(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        # Create a file outside plans dir that traversal might reach
        outside = tmp_path / "secret.md"
        outside.write_text("# Plan: Secret")

        cog = self._make_cog(tmp_path)
        # Attempt traversal: "../secret" would resolve to tmp_path/secret.md
        result = cog._resolve_plan_file("../secret")
        assert result is None


class TestPlanDeleteCommand:
    def test_delete_cleans_all_manifests(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "2026-03-01_feat.md").write_text("# Plan: Feat")

        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "executed.json").write_text(
            json.dumps({"2026-03-01_feat.md": {"executed_at": "2026-03-01T12:00:00"}})
        )
        (meta / "planned.json").write_text(
            json.dumps({"topic_001": {"plan_file": "2026-03-01_feat.md", "title": "Feat"}})
        )

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.plan_delete.callback(cog, ctx, name="2026-03-01_feat"))

        assert not (plans / "2026-03-01_feat.md").exists()
        assert "2026-03-01_feat.md" not in json.loads((meta / "executed.json").read_text())
        assert "topic_001" not in json.loads((meta / "planned.json").read_text())
