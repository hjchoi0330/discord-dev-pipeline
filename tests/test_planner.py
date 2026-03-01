"""planner 모듈 단위 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from analyzer.analyzer import AnalysisResult, DevTopic, Message
from planner.planner import _format_messages, _sanitize_filename, generate_plans


# ── _sanitize_filename ───────────────────────────────────────────────


class TestSanitizeFilename:
    def test_basic(self):
        assert _sanitize_filename("Add OAuth login") == "add-oauth-login"

    def test_special_chars_removed(self):
        result = _sanitize_filename("Fix bug #123: crash!")
        assert "#" not in result
        assert "!" not in result
        assert ":" not in result

    def test_multiple_spaces_collapsed(self):
        result = _sanitize_filename("too   many   spaces")
        assert "--" not in result  # 연속 하이픈 없음

    def test_truncation(self):
        long_title = "a" * 100
        result = _sanitize_filename(long_title)
        assert len(result) <= 60

    def test_empty_string(self):
        result = _sanitize_filename("")
        assert isinstance(result, str)

    def test_unicode_korean(self):
        result = _sanitize_filename("로그인 기능 추가")
        assert isinstance(result, str)
        assert len(result) > 0


# ── _format_messages ─────────────────────────────────────────────────


class TestFormatMessages:
    def test_with_messages(self):
        topic = DevTopic(
            id="t1",
            title="Test",
            category="feature",
            priority="high",
            messages=[
                Message(author="alice", content="Let's add auth", timestamp="2026-01-01T10:00:00Z"),
                Message(author="bob", content="Good idea", timestamp="2026-01-01T10:01:00Z"),
            ],
            summary="Auth discussion",
            keywords=["auth"],
            actionable=True,
            estimated_complexity="medium",
        )
        result = _format_messages(topic)
        assert "alice" in result
        assert "Let's add auth" in result
        assert "bob" in result

    def test_no_messages(self):
        topic = DevTopic(
            id="t1", title="Test", category="feature", priority="high",
            messages=[], summary="", keywords=[], actionable=True,
            estimated_complexity="small",
        )
        result = _format_messages(topic)
        assert "no specific messages" in result

    def test_max_20_messages(self):
        messages = [
            Message(author=f"user{i}", content=f"msg{i}", timestamp="")
            for i in range(30)
        ]
        topic = DevTopic(
            id="t1", title="Test", category="feature", priority="high",
            messages=messages, summary="", keywords=[], actionable=True,
            estimated_complexity="small",
        )
        result = _format_messages(topic)
        # 21번째 이후 메시지는 포함되지 않아야 함
        assert "user20" not in result
        assert "user19" in result


# ── generate_plans (통합, mock) ──────────────────────────────────────


class TestGeneratePlans:
    def _make_analysis(self, topics: list[DevTopic]) -> AnalysisResult:
        return AnalysisResult(
            date="2026-01-01",
            analyzed_at="2026-01-01T12:00:00Z",
            source_files=["conv.json"],
            dev_topics=topics,
            total_messages_analyzed=10,
            dev_topics_found=len(topics),
        )

    def _make_topic(self, title: str, actionable: bool = True, priority: str = "high") -> DevTopic:
        return DevTopic(
            id="t1", title=title, category="feature", priority=priority,
            messages=[Message(author="alice", content="do it", timestamp="2026-01-01T10:00:00Z")],
            summary="Test summary", keywords=["test"], actionable=actionable,
            estimated_complexity="medium",
        )

    @patch("planner.planner._call_claude")
    def test_generates_plan_files(self, mock_claude, tmp_path):
        mock_claude.return_value = "# Plan: Add Auth\n\nSome plan content here."
        analysis = self._make_analysis([self._make_topic("Add Auth")])

        result = generate_plans(analysis, tmp_path / "plans")

        assert len(result) == 1
        assert result[0].exists()
        assert result[0].suffix == ".md"
        content = result[0].read_text()
        assert "Plan" in content

    @patch("planner.planner._call_claude")
    def test_skips_non_actionable(self, mock_claude, tmp_path):
        analysis = self._make_analysis([
            self._make_topic("Discussion only", actionable=False),
        ])

        result = generate_plans(analysis, tmp_path / "plans")
        assert len(result) == 0
        mock_claude.assert_not_called()

    @patch("planner.planner._call_claude")
    def test_priority_sorting(self, mock_claude, tmp_path):
        mock_claude.return_value = "# Plan content"
        analysis = self._make_analysis([
            self._make_topic("Low task", priority="low"),
            self._make_topic("High task", priority="high"),
            self._make_topic("Medium task", priority="medium"),
        ])

        result = generate_plans(analysis, tmp_path / "plans")
        assert len(result) == 3
        # Claude가 high → medium → low 순으로 호출되었는지 확인
        calls = mock_claude.call_args_list
        assert "High task" in calls[0][0][0]
        assert "Medium task" in calls[1][0][0]
        assert "Low task" in calls[2][0][0]

    @patch("planner.planner._call_claude")
    def test_empty_topics(self, mock_claude, tmp_path):
        analysis = self._make_analysis([])
        result = generate_plans(analysis, tmp_path / "plans")
        assert result == []

    @patch("planner.planner._call_claude")
    def test_skips_already_planned_topic(self, mock_claude, tmp_path):
        """A topic already in planned manifest should be skipped."""
        import json
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "planned.json").write_text(json.dumps({
            "t1": {
                "title": "Rock Paper Scissors Game",
                "plan_file": "2026-01-01_rock-paper-scissors-game.md",
                "planned_at": "2026-01-01T12:00:00Z",
            },
        }))

        mock_claude.return_value = "# Plan: Countdown\n\nNew plan."
        topic_rps = self._make_topic("Develop Rock Paper Scissors Game")
        topic_countdown = DevTopic(
            id="t2", title="Build Countdown Timer", category="feature",
            priority="high",
            messages=[Message(author="alice", content="do it", timestamp="2026-01-01T10:00:00Z")],
            summary="Timer", keywords=["timer"], actionable=True,
            estimated_complexity="small",
        )
        analysis = self._make_analysis([topic_rps, topic_countdown])

        result = generate_plans(analysis, plans_dir, data_dir=tmp_path)

        assert len(result) == 1
        assert "countdown" in result[0].name
        assert mock_claude.call_count == 1

    @patch("planner.planner._call_claude")
    def test_planned_manifest_written_on_success(self, mock_claude, tmp_path):
        """Successful plan generation should write to meta/planned.json."""
        import json
        plans_dir = tmp_path / "plans"
        mock_claude.return_value = "# Plan: Auth\n\nContent."
        analysis = self._make_analysis([self._make_topic("Add Auth")])

        generate_plans(analysis, plans_dir, data_dir=tmp_path)

        manifest_path = tmp_path / "meta" / "planned.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert "t1" in manifest
        assert manifest["t1"]["title"] == "Add Auth"


# ── Planned manifest helpers ─────────────────────────────────────────


class TestPlannedManifest:
    def test_load_empty(self, tmp_path):
        from planner.planner import _load_planned_manifest
        assert _load_planned_manifest(tmp_path) == {}

    def test_load_corrupt(self, tmp_path):
        from planner.planner import _load_planned_manifest
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "planned.json").write_text("not json")
        assert _load_planned_manifest(tmp_path) == {}

    def test_mark_and_load(self, tmp_path):
        from planner.planner import _load_planned_manifest, _mark_topic_planned
        topic = DevTopic(
            id="t42", title="Test Topic", category="feature", priority="high",
            messages=[], summary="s", keywords=[], actionable=True,
            estimated_complexity="small",
        )
        plan_file = tmp_path / "2026-01-01_test-topic.md"
        _mark_topic_planned(topic, plan_file, tmp_path)

        manifest = _load_planned_manifest(tmp_path)
        assert "t42" in manifest
        assert manifest["t42"]["plan_file"] == plan_file.name

    def test_preserves_previous_entries(self, tmp_path):
        import json
        from planner.planner import _load_planned_manifest, _mark_topic_planned
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        (meta_dir / "planned.json").write_text(json.dumps({
            "old_id": {"title": "Old", "plan_file": "old.md", "planned_at": "2026-01-01T00:00:00Z"},
        }))
        topic = DevTopic(
            id="new_id", title="New", category="feature", priority="high",
            messages=[], summary="s", keywords=[], actionable=True,
            estimated_complexity="small",
        )
        _mark_topic_planned(topic, tmp_path / "new.md", tmp_path)

        manifest = _load_planned_manifest(tmp_path)
        assert "old_id" in manifest
        assert "new_id" in manifest


# ── generate_plans: Claude CLI 실패 시 graceful skip ─────────────────


class TestGeneratePlansCLIFailure:
    def _make_analysis(self, topics: list[DevTopic]) -> AnalysisResult:
        return AnalysisResult(
            date="2026-01-01",
            analyzed_at="2026-01-01T12:00:00Z",
            source_files=["conv.json"],
            dev_topics=topics,
            total_messages_analyzed=10,
            dev_topics_found=len(topics),
        )

    def _make_topic(self, title: str, priority: str = "high") -> DevTopic:
        return DevTopic(
            id="t1", title=title, category="feature", priority=priority,
            messages=[Message(author="alice", content="do it", timestamp="2026-01-01T10:00:00Z")],
            summary="Test summary", keywords=["test"], actionable=True,
            estimated_complexity="medium",
        )

    @patch("planner.planner._call_claude")
    def test_claude_failure_skips_topic(self, mock_claude, tmp_path):
        """Claude CLI 실패 시 해당 토픽을 건너뛰고 나머지를 계속 처리해야 한다."""
        mock_claude.side_effect = [
            RuntimeError("claude CLI 오류"),
            "# Plan: Second Topic\n\nContent here.",
        ]
        analysis = self._make_analysis([
            self._make_topic("First Topic"),
            self._make_topic("Second Topic"),
        ])

        result = generate_plans(analysis, tmp_path / "plans")

        assert len(result) == 1
        assert "second-topic" in result[0].name

    @patch("planner.planner._call_claude")
    def test_all_claude_failures_returns_empty(self, mock_claude, tmp_path):
        """모든 Claude CLI 호출이 실패하면 빈 목록을 반환해야 한다."""
        mock_claude.side_effect = RuntimeError("claude CLI 오류")
        analysis = self._make_analysis([
            self._make_topic("Topic A"),
            self._make_topic("Topic B"),
        ])

        result = generate_plans(analysis, tmp_path / "plans")

        assert len(result) == 0
