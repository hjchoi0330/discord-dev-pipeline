"""Tests for new Discord commands: !topic, !conversations, !runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from collector.management import (
    ManagementCog,
    _parse_topic_response,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.display_name = "TestUser"
    ctx.invoked_subcommand = None
    return ctx


# ── _parse_topic_response tests ──────────────────────────────────


class TestParseTopicResponse:
    def test_direct_json(self):
        response = '{"title": "Add Auth", "category": "feature", "priority": "high"}'
        result = _parse_topic_response(response)
        assert result["title"] == "Add Auth"

    def test_json_in_code_fence(self):
        response = 'Here is the result:\n```json\n{"title": "Fix Bug", "priority": "low"}\n```'
        result = _parse_topic_response(response)
        assert result["title"] == "Fix Bug"

    def test_json_with_preamble(self):
        response = 'Analyzing your request...\n\n{"title": "Refactor DB", "category": "refactor"}'
        result = _parse_topic_response(response)
        assert result["title"] == "Refactor DB"

    def test_invalid_json(self):
        result = _parse_topic_response("This is not JSON at all")
        assert result is None

    def test_json_without_title_still_parsed(self):
        # Direct JSON parse succeeds even without "title" key
        response = '{"key": "value", "other": 123}'
        result = _parse_topic_response(response)
        assert result == {"key": "value", "other": 123}


# ── !topic command tests ─────────────────────────────────────────


class TestTopicCommand:
    def test_empty_description_shows_usage(self, tmp_path: Path):
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.topic_command.callback(cog, ctx, description=""))
        msg = ctx.send.call_args[0][0]
        assert "Usage" in msg

    @patch("shared.claude_cli.call_claude")
    def test_successful_topic_injection(self, mock_claude, tmp_path: Path):
        mock_claude.return_value = json.dumps({
            "title": "Add Rate Limiting",
            "category": "feature",
            "priority": "high",
            "summary": "Implement rate limiting for the API gateway.",
            "keywords": ["rate-limit", "api", "gateway"],
            "actionable": True,
            "estimated_complexity": "medium",
        })

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.topic_command.callback(cog, ctx, description="API 게이트웨이에 rate limiting 추가"))

        # Should have called Claude
        mock_claude.assert_called_once()

        # Should have sent an embed
        calls = ctx.send.call_args_list
        # First call: "Structuring topic..."
        # Second call: embed with topic
        embed_call = next(c for c in calls if "embed" in c.kwargs)
        embed = embed_call.kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert "Add Rate Limiting" in embed.title

        # Should have saved to analysis file
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        analysis_path = tmp_path / "analysis" / f"{date}_analysis.json"
        assert analysis_path.exists()
        data = json.loads(analysis_path.read_text())
        assert len(data["dev_topics"]) == 1
        assert data["dev_topics"][0]["title"] == "Add Rate Limiting"
        assert data["dev_topics"][0]["source"] == "manual"

    @patch("shared.claude_cli.call_claude")
    def test_topic_appends_to_existing_analysis(self, mock_claude, tmp_path: Path):
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Pre-existing analysis
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir(parents=True)
        existing = {
            "date": date,
            "analyzed_at": "2026-03-01T00:00:00",
            "source_files": ["file1.json"],
            "dev_topics": [
                {"id": "topic_001", "title": "Existing Topic", "actionable": True}
            ],
            "total_messages_analyzed": 10,
            "dev_topics_found": 1,
        }
        (analysis_dir / f"{date}_analysis.json").write_text(json.dumps(existing))

        mock_claude.return_value = json.dumps({
            "title": "New Topic",
            "category": "bug",
            "priority": "medium",
            "summary": "Fix something.",
            "keywords": ["fix"],
            "actionable": True,
            "estimated_complexity": "small",
        })

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.topic_command.callback(cog, ctx, description="Fix the thing"))

        data = json.loads((analysis_dir / f"{date}_analysis.json").read_text())
        assert len(data["dev_topics"]) == 2
        assert data["dev_topics"][1]["title"] == "New Topic"
        assert data["dev_topics_found"] == 2
        assert "manual" in data["source_files"]

    @patch("shared.claude_cli.call_claude")
    def test_topic_claude_failure(self, mock_claude, tmp_path: Path):
        mock_claude.side_effect = RuntimeError("Claude is down")

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.topic_command.callback(cog, ctx, description="Some topic"))

        # Should report the error
        calls = ctx.send.call_args_list
        error_msg = next(c for c in calls if c.args and "\u274C" in str(c.args[0]))
        assert "Failed" in error_msg.args[0]

    @patch("shared.claude_cli.call_claude")
    def test_topic_bad_response(self, mock_claude, tmp_path: Path):
        mock_claude.return_value = "This is not valid JSON and has no title"

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.topic_command.callback(cog, ctx, description="Some topic"))

        calls = ctx.send.call_args_list
        error_msg = next(c for c in calls if c.args and "parse" in str(c.args[0]).lower())
        assert error_msg is not None


# ── !conversations command tests ─────────────────────────────────


class TestConversationsCommand:
    def test_no_conversations_dir(self, tmp_path: Path):
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.conversations_command.callback(cog, ctx, date=""))
        msg = ctx.send.call_args[0][0]
        assert "No conversations" in msg

    def test_no_files_for_date(self, tmp_path: Path):
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.conversations_command.callback(cog, ctx, date="2099-01-01"))
        msg = ctx.send.call_args[0][0]
        assert "No conversation" in msg

    def test_invalid_date_format(self, tmp_path: Path):
        (tmp_path / "conversations").mkdir()
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.conversations_command.callback(cog, ctx, date="bad-date"))
        msg = ctx.send.call_args[0][0]
        assert "Invalid date" in msg

    def test_lists_conversations_as_embeds(self, tmp_path: Path):
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()
        data = {
            "date": "2026-03-01",
            "channel": "dev-talk",
            "guild": "TestServer",
            "type": "voice_transcription",
            "messages": [
                {"author": "alice", "content": "hello", "timestamp": "2026-03-01T10:00:00"},
            ],
        }
        (conv_dir / "2026-03-01_TestServer_dev-talk_voice.json").write_text(json.dumps(data))

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.conversations_command.callback(cog, ctx, date="2026-03-01"))

        embed_call = next(c for c in ctx.send.call_args_list if "embed" in c.kwargs)
        embed = embed_call.kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert "TestServer" in embed.title

    def test_lists_all_when_no_date(self, tmp_path: Path):
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()
        for d in ("2026-03-01", "2026-03-02"):
            data = {
                "date": d, "channel": "ch", "guild": "g",
                "type": "voice", "messages": [],
            }
            (conv_dir / f"{d}_g_ch_voice.json").write_text(json.dumps(data))

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.conversations_command.callback(cog, ctx, date=""))

        embed_calls = [c for c in ctx.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_calls) == 2


# ── !runs command tests ──────────────────────────────────────────


class TestRunsCommand:
    def test_no_runs_dir(self, tmp_path: Path):
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.runs_command.callback(cog, ctx, date=""))
        msg = ctx.send.call_args[0][0]
        assert "No pipeline" in msg

    def test_no_runs_for_date(self, tmp_path: Path):
        runs_dir = tmp_path / "pipeline_runs"
        runs_dir.mkdir()
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.runs_command.callback(cog, ctx, date="2099-01-01"))
        msg = ctx.send.call_args[0][0]
        assert "No pipeline" in msg

    def test_invalid_date_format(self, tmp_path: Path):
        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.runs_command.callback(cog, ctx, date="bad"))
        msg = ctx.send.call_args[0][0]
        assert "Invalid date" in msg

    def test_shows_runs_as_embeds(self, tmp_path: Path):
        runs_dir = tmp_path / "pipeline_runs"
        runs_dir.mkdir()
        run_data = {
            "run_id": "120000-abc",
            "date": "2026-03-01",
            "status": "completed",
            "started_at": "2026-03-01T12:00:00+00:00",
            "completed_at": "2026-03-01T12:01:00+00:00",
            "stages": [
                {
                    "name": "collect",
                    "status": "completed",
                    "started_at": "2026-03-01T12:00:00+00:00",
                    "completed_at": "2026-03-01T12:00:05+00:00",
                    "output_summary": {"files_found": 2},
                },
            ],
        }
        (runs_dir / "2026-03-01_120000-abc.json").write_text(json.dumps(run_data))

        cog = ManagementCog(MagicMock(), tmp_path)
        ctx = _mock_ctx()
        _run(cog.runs_command.callback(cog, ctx, date="2026-03-01"))

        embed_call = next(c for c in ctx.send.call_args_list if "embed" in c.kwargs)
        embed = embed_call.kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert "2026-03-01" in embed.title
        assert "COMPLETED" in embed.title
