"""Tests for shared.embeds — Discord embed builder functions."""

from __future__ import annotations

import json
from pathlib import Path

import discord
import pytest

from shared.embeds import (
    COLOR_FAILURE,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    conversation_embed,
    pipeline_result_embed,
    plan_embed,
    run_history_embed,
    topic_embed,
)


class TestPipelineResultEmbed:
    def test_completed_pipeline(self):
        run_data = {"status": "completed", "date": "2026-03-01", "duration": 42.5}
        stages = [
            {"name": "collect", "status": "completed", "duration": 1.0, "detail": "3 files"},
            {"name": "analyze", "status": "completed", "duration": 10.0},
            {"name": "plan", "status": "completed", "duration": 30.0},
            {"name": "execute", "status": "skipped"},
        ]
        embed = pipeline_result_embed(run_data, stages)
        assert isinstance(embed, discord.Embed)
        assert embed.color.value == COLOR_SUCCESS
        assert "2026-03-01" in embed.title
        assert any("collect" in f.value for f in embed.fields)

    def test_failed_pipeline(self):
        run_data = {"status": "failed", "date": "2026-03-01"}
        stages = [
            {"name": "collect", "status": "failed"},
        ]
        embed = pipeline_result_embed(run_data, stages)
        assert embed.color.value == COLOR_FAILURE
        assert "Failed" in embed.title

    def test_partial_pipeline(self):
        run_data = {"status": "partial", "date": "2026-03-01"}
        stages = []
        embed = pipeline_result_embed(run_data, stages)
        assert embed.color.value == COLOR_WARNING


class TestTopicEmbed:
    def test_basic_topic(self):
        topic = {
            "title": "Add Docker Health Check",
            "priority": "high",
            "category": "feature",
            "actionable": True,
            "summary": "Need health checks for containers.",
            "keywords": ["docker", "health-check", "monitoring"],
            "estimated_complexity": "medium",
        }
        embed = topic_embed(topic)
        assert isinstance(embed, discord.Embed)
        assert "Add Docker Health Check" in embed.title
        assert embed.color.value == COLOR_FAILURE  # high priority = red

    def test_topic_with_index(self):
        topic = {"title": "Fix Bug", "priority": "low", "actionable": False}
        embed = topic_embed(topic, index=3)
        assert "#3" in embed.title
        assert embed.color.value == COLOR_SUCCESS  # low priority = green

    def test_topic_minimal_fields(self):
        topic = {"title": "Test", "priority": "medium"}
        embed = topic_embed(topic)
        assert "Test" in embed.title


class TestPlanEmbed:
    def test_pending_plan(self, tmp_path: Path):
        plan = tmp_path / "2026-03-01_docker.md"
        plan.write_text(
            "# Plan: Docker Health Check\n"
            "**Category:** feature\n"
            "**Priority:** high\n"
            "**Complexity:** medium\n\n"
            "## Objective\nAdd health checks to Docker containers.\n\n"
            "## Context\nContainers crash without detection.\n"
        )
        embed = plan_embed(plan, executed=False)
        assert isinstance(embed, discord.Embed)
        assert "Docker Health Check" in embed.title
        assert embed.color.value == COLOR_INFO
        # Check fields
        field_names = [f.name for f in embed.fields]
        assert "Category" in field_names
        assert "Objective" in field_names

    def test_executed_plan(self, tmp_path: Path):
        plan = tmp_path / "2026-03-01_feat.md"
        plan.write_text("# Plan: Feature X\n## Objective\nDo something.\n")
        embed = plan_embed(plan, executed=True, exec_date="2026-03-01T12:00:00")
        assert embed.color.value == COLOR_SUCCESS
        status_field = next(f for f in embed.fields if f.name == "Status")
        assert "Executed" in status_field.value

    def test_missing_plan_file(self, tmp_path: Path):
        plan = tmp_path / "nonexistent.md"
        embed = plan_embed(plan)
        assert "Failed to read" in embed.description


class TestConversationEmbed:
    def test_basic_conversation(self):
        data = {
            "date": "2026-03-01",
            "channel": "dev-talk",
            "guild": "MyServer",
            "type": "voice_transcription",
            "messages": [
                {"author": "alice", "content": "hello", "timestamp": "2026-03-01T10:00:00"},
                {"author": "bob", "content": "hi", "timestamp": "2026-03-01T10:05:00"},
            ],
        }
        embed = conversation_embed(data, "2026-03-01_MyServer_dev-talk_voice.json")
        assert isinstance(embed, discord.Embed)
        assert "MyServer" in embed.title
        assert "dev-talk" in embed.title
        field_map = {f.name: f.value for f in embed.fields}
        assert field_map["Messages"] == "2"
        assert field_map["Speakers"] == "2"

    def test_empty_conversation(self):
        data = {
            "date": "2026-03-01",
            "channel": "empty",
            "guild": "Server",
            "type": "voice_transcription",
            "messages": [],
        }
        embed = conversation_embed(data, "file.json")
        field_map = {f.name: f.value for f in embed.fields}
        assert field_map["Messages"] == "0"


class TestRunHistoryEmbed:
    def test_completed_run(self):
        run_data = {
            "run_id": "120000-abc123",
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
                    "output_summary": {"files_found": 3},
                },
                {
                    "name": "analyze",
                    "status": "completed",
                    "started_at": "2026-03-01T12:00:05+00:00",
                    "completed_at": "2026-03-01T12:00:30+00:00",
                    "output_summary": {"topics_found": 2},
                },
            ],
        }
        embed = run_history_embed(run_data)
        assert isinstance(embed, discord.Embed)
        assert embed.color.value == COLOR_SUCCESS
        assert "2026-03-01" in embed.title
        field_value = embed.fields[0].value
        assert "collect" in field_value
        assert "analyze" in field_value
        assert "topics_found=2" in field_value

    def test_failed_run(self):
        run_data = {
            "run_id": "test-run",
            "date": "2026-03-01",
            "status": "failed",
            "stages": [
                {
                    "name": "collect",
                    "status": "failed",
                    "error": "No files found",
                    "output_summary": {},
                },
            ],
        }
        embed = run_history_embed(run_data)
        assert embed.color.value == COLOR_FAILURE
        assert "No files found" in embed.fields[0].value
