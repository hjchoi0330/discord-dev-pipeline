"""Unit tests for the analyzer module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from analyzer.analyzer import (
    AnalysisResult,
    DevTopic,
    Message,
    _chunk_messages,
    _deduplicate_topics,
    _format_messages_for_prompt,
    _load_conversation_file,
    _parse_claude_response,
    analyze_conversations,
)


# ── _format_messages_for_prompt ──────────────────────────────────────


class TestFormatMessagesForPrompt:
    def test_basic_formatting(self):
        messages = [
            {"author": "alice", "timestamp": "2026-01-01T10:00:00Z", "content": "hello"},
            {"author": "bob", "timestamp": "2026-01-01T10:01:00Z", "content": "hi there"},
        ]
        result = _format_messages_for_prompt(messages)
        assert "[0] alice (2026-01-01 10:00:0" in result
        assert "[1] bob" in result
        assert "hello" in result
        assert "hi there" in result

    def test_missing_timestamp(self):
        messages = [{"author": "alice", "content": "test"}]
        result = _format_messages_for_prompt(messages)
        assert "[0] alice" in result
        assert "test" in result

    def test_empty_list(self):
        assert _format_messages_for_prompt([]) == ""


# ── _chunk_messages ──────────────────────────────────────────────────


class TestChunkMessages:
    def test_single_chunk(self):
        messages = [{"content": "short"} for _ in range(5)]
        chunks = _chunk_messages(messages)
        assert len(chunks) == 1
        assert len(chunks[0]) == 5

    def test_split_by_count(self):
        messages = [{"content": "x"} for _ in range(250)]
        chunks = _chunk_messages(messages)
        assert len(chunks) >= 3  # 250 / 100 = 2.5 → 3 chunks

    def test_split_by_char_limit(self):
        # Each message is 5000 chars -> 2 messages = 10000 chars > 8000 limit
        messages = [{"content": "a" * 5000} for _ in range(3)]
        chunks = _chunk_messages(messages)
        assert len(chunks) >= 2

    def test_empty_messages(self):
        assert _chunk_messages([]) == []

    def test_single_message(self):
        chunks = _chunk_messages([{"content": "hello"}])
        assert len(chunks) == 1
        assert len(chunks[0]) == 1


# ── _parse_claude_response ───────────────────────────────────────────


class TestParseClaudeResponse:
    def test_valid_json(self):
        response = '{"dev_topics": [{"title": "Add auth", "category": "feature"}]}'
        topics = _parse_claude_response(response)
        assert len(topics) == 1
        assert topics[0]["title"] == "Add auth"

    def test_json_in_markdown_fence(self):
        response = """Here is the analysis:
```json
{"dev_topics": [{"title": "Fix bug"}]}
```
"""
        topics = _parse_claude_response(response)
        assert len(topics) == 1
        assert topics[0]["title"] == "Fix bug"

    def test_no_dev_topics(self):
        response = '{"dev_topics": []}'
        assert _parse_claude_response(response) == []

    def test_invalid_json(self):
        assert _parse_claude_response("not json at all") == []

    def test_empty_response(self):
        assert _parse_claude_response("") == []

    def test_json_without_dev_topics_key(self):
        response = '{"other_key": "value"}'
        assert _parse_claude_response(response) == []

    def test_multiple_json_blocks_uses_first_valid(self):
        """When response contains multiple JSON-like blocks, the first parseable one with dev_topics wins."""
        response = (
            'Some preamble {"broken": } garbage '
            '{"dev_topics": [{"title": "Real topic"}]} '
            '{"dev_topics": [{"title": "Second block"}]}'
        )
        topics = _parse_claude_response(response)
        assert len(topics) == 1
        assert topics[0]["title"] == "Real topic"


# ── _deduplicate_topics ──────────────────────────────────────────────


class TestDeduplicateTopics:
    def _make_topic(self, title: str) -> DevTopic:
        return DevTopic(
            id="t1",
            title=title,
            category="feature",
            priority="medium",
            messages=[],
            summary="",
            keywords=[],
            actionable=True,
            estimated_complexity="small",
        )

    def test_no_duplicates(self):
        topics = [self._make_topic("A"), self._make_topic("B")]
        assert len(_deduplicate_topics(topics)) == 2

    def test_exact_duplicate(self):
        topics = [self._make_topic("Auth"), self._make_topic("Auth")]
        assert len(_deduplicate_topics(topics)) == 1

    def test_case_insensitive(self):
        topics = [self._make_topic("Add Auth"), self._make_topic("add auth")]
        assert len(_deduplicate_topics(topics)) == 1

    def test_whitespace_normalization(self):
        topics = [self._make_topic("  Auth  "), self._make_topic("auth")]
        assert len(_deduplicate_topics(topics)) == 1

    def test_empty_list(self):
        assert _deduplicate_topics([]) == []


# ── analyze_conversations (integration, mock) ────────────────────────


class TestAnalyzeConversations:
    def _make_conv_file(self, tmp_path: Path, name: str, messages: list[dict]) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
        return path

    @patch("analyzer.analyzer._call_claude")
    def test_basic_analysis(self, mock_claude, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        conv = self._make_conv_file(tmp_path, "conv.json", [
            {"author": "alice", "timestamp": "2026-01-01T10:00:00Z", "content": "We need a login page"},
            {"author": "bob", "timestamp": "2026-01-01T10:01:00Z", "content": "Yeah, OAuth would be good"},
        ])
        mock_claude.return_value = json.dumps({
            "dev_topics": [{
                "title": "Add OAuth login",
                "category": "feature",
                "priority": "high",
                "summary": "Team wants OAuth",
                "keywords": ["oauth", "login"],
                "actionable": True,
                "estimated_complexity": "medium",
                "relevant_message_indices": [0, 1],
            }]
        })

        result = analyze_conversations([conv], "2026-01-01")

        assert isinstance(result, AnalysisResult)
        assert result.dev_topics_found == 1
        assert result.dev_topics[0].title == "Add OAuth login"
        assert result.total_messages_analyzed == 2
        mock_claude.assert_called_once()

    @patch("analyzer.analyzer._call_claude")
    def test_empty_messages_filtered(self, mock_claude, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        conv = self._make_conv_file(tmp_path, "conv.json", [
            {"author": "bot", "timestamp": "2026-01-01T10:00:00Z", "content": ""},
            {"author": "alice", "timestamp": "2026-01-01T10:01:00Z", "content": "  "},
            {"author": "bob", "timestamp": "2026-01-01T10:02:00Z", "content": "real message"},
        ])
        mock_claude.return_value = '{"dev_topics": []}'

        result = analyze_conversations([conv], "2026-01-01")
        assert result.total_messages_analyzed == 3
        assert result.dev_topics_found == 0

    @patch("analyzer.analyzer._call_claude")
    def test_no_files(self, mock_claude, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = analyze_conversations([], "2026-01-01")
        assert result.dev_topics_found == 0
        mock_claude.assert_not_called()

    @patch("analyzer.analyzer._call_claude")
    def test_non_dev_conversation_returns_no_topics(self, mock_claude, tmp_path, monkeypatch):
        """Conversations unrelated to development (lunch, team dinner, hiking, etc.) should return 0 topics."""
        monkeypatch.chdir(tmp_path)
        conv = self._make_conv_file(tmp_path, "random.json", [
            {"author": "김민수", "timestamp": "2026-02-28T12:01:00Z", "content": "다들 점심 뭐 먹었어? 라멘집 갔는데 맛있더라."},
            {"author": "이지영", "timestamp": "2026-02-28T12:02:00Z", "content": "오 어디? 나도 라멘 좋아하는데."},
            {"author": "박준혁", "timestamp": "2026-02-28T12:03:00Z", "content": "금요일에 팀 회식 있잖아. 다들 참석 가능해?"},
            {"author": "이지영", "timestamp": "2026-02-28T12:04:00Z", "content": "삼겹살집 어때? 가격도 괜찮고 단체석도 있었잖아."},
            {"author": "김민수", "timestamp": "2026-02-28T12:05:00Z", "content": "주말에 등산 갈까? 북한산 봄꽃 피기 시작했대."},
        ])
        mock_claude.return_value = '{"dev_topics": []}'

        result = analyze_conversations([conv], "2026-02-28")

        assert result.dev_topics_found == 0
        assert result.dev_topics == []
        assert result.total_messages_analyzed == 5

    @patch("analyzer.analyzer._call_claude")
    def test_mixed_dev_and_non_dev_files(self, mock_claude, tmp_path, monkeypatch):
        """When dev and non-dev conversations are mixed, only dev topics should be extracted."""
        monkeypatch.chdir(tmp_path)
        dev_conv = self._make_conv_file(tmp_path, "general.json", [
            {"author": "alice", "timestamp": "2026-01-01T10:00:00Z", "content": "Docker 컨테이너가 OOM으로 죽어"},
            {"author": "bob", "timestamp": "2026-01-01T10:01:00Z", "content": "메모리 limit을 올리자"},
        ])
        non_dev_conv = self._make_conv_file(tmp_path, "random.json", [
            {"author": "alice", "timestamp": "2026-01-01T12:00:00Z", "content": "점심 뭐 먹을까?"},
            {"author": "bob", "timestamp": "2026-01-01T12:01:00Z", "content": "근처 카페 가자"},
        ])

        def mock_response(prompt, timeout=120):
            if "OOM" in prompt or "Docker" in prompt:
                return json.dumps({"dev_topics": [{
                    "title": "Fix Docker OOM",
                    "category": "infrastructure",
                    "priority": "high",
                    "summary": "Docker OOM issue",
                    "keywords": ["docker", "OOM"],
                    "actionable": True,
                    "estimated_complexity": "small",
                    "relevant_message_indices": [0, 1],
                }]})
            return '{"dev_topics": []}'

        mock_claude.side_effect = mock_response

        result = analyze_conversations([dev_conv, non_dev_conv], "2026-01-01")

        assert result.dev_topics_found == 1
        assert result.dev_topics[0].title == "Fix Docker OOM"
        assert result.total_messages_analyzed == 4
        assert len(result.source_files) == 2
        assert mock_claude.call_count == 2

    @patch("analyzer.analyzer._call_claude")
    def test_dev_talk_in_random_channel_detected(self, mock_claude, tmp_path, monkeypatch):
        """Even in a random channel, dev-related conversation should produce extracted topics."""
        monkeypatch.chdir(tmp_path)
        # Filename indicates a random channel, but content contains dev discussion
        conv = self._make_conv_file(tmp_path, "2026-01-01_server_random.json", [
            {"author": "김민수", "timestamp": "2026-01-01T14:00:00Z", "content": "점심 맛있었어?"},
            {"author": "이지영", "timestamp": "2026-01-01T14:01:00Z", "content": "응 근데 아까 Redis 캐시 만료 이슈 봤어?"},
            {"author": "박준혁", "timestamp": "2026-01-01T14:02:00Z", "content": "TTL 설정이 잘못된 것 같아. 캐시 무효화 로직 수정해야 해."},
        ])
        mock_claude.return_value = json.dumps({
            "dev_topics": [{
                "title": "Redis 캐시 TTL 설정 오류 수정",
                "category": "bug",
                "priority": "high",
                "summary": "Redis 캐시 만료 이슈 발견",
                "keywords": ["redis", "cache", "TTL"],
                "actionable": True,
                "estimated_complexity": "small",
                "relevant_message_indices": [1, 2],
            }]
        })

        result = analyze_conversations([conv], "2026-01-01")

        assert result.dev_topics_found == 1
        assert result.dev_topics[0].title == "Redis 캐시 TTL 설정 오류 수정"
        # Confirm analysis is content-based, not channel-name-based
        assert "random" in result.source_files[0]


# ── _load_conversation_file (error handling) ─────────────────────────


class TestLoadConversationFile:
    def test_valid_json(self, tmp_path):
        path = tmp_path / "conv.json"
        path.write_text('{"messages": [{"author": "alice", "content": "hi"}]}', encoding="utf-8")
        result = _load_conversation_file(path)
        assert result["messages"][0]["author"] == "alice"

    def test_file_not_found(self, tmp_path):
        result = _load_conversation_file(tmp_path / "nonexistent.json")
        assert result == {}

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        result = _load_conversation_file(path)
        assert result == {}

    def test_non_dict_json(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text('[1, 2, 3]', encoding="utf-8")
        result = _load_conversation_file(path)
        assert result == {}

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("", encoding="utf-8")
        result = _load_conversation_file(path)
        assert result == {}


# ── analyze_conversations: graceful skip on Claude CLI failure ────────


class TestAnalyzeConversationsCLIFailure:
    def _make_conv_file(self, tmp_path: Path, name: str, messages: list[dict]) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
        return path

    @patch("analyzer.analyzer._call_claude")
    def test_claude_failure_skips_chunk(self, mock_claude, tmp_path, monkeypatch):
        """A Claude CLI call failure should not abort the entire analysis."""
        monkeypatch.chdir(tmp_path)
        conv = self._make_conv_file(tmp_path, "conv.json", [
            {"author": "alice", "timestamp": "2026-01-01T10:00:00Z", "content": "need auth"},
        ])
        mock_claude.side_effect = RuntimeError("claude CLI 오류")

        result = analyze_conversations([conv], "2026-01-01")

        assert result.dev_topics_found == 0
        assert result.total_messages_analyzed == 1

    @patch("analyzer.analyzer._call_claude")
    def test_claude_timeout_skips_chunk(self, mock_claude, tmp_path, monkeypatch):
        """A Claude CLI timeout should not abort the entire analysis."""
        import subprocess
        monkeypatch.chdir(tmp_path)
        conv = self._make_conv_file(tmp_path, "conv.json", [
            {"author": "alice", "timestamp": "2026-01-01T10:00:00Z", "content": "need auth"},
        ])
        mock_claude.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)

        result = analyze_conversations([conv], "2026-01-01")

        assert result.dev_topics_found == 0

    @patch("analyzer.analyzer._call_claude")
    def test_corrupt_file_skipped_gracefully(self, mock_claude, tmp_path, monkeypatch):
        """Corrupt conversation files should be skipped while remaining files are analyzed normally."""
        monkeypatch.chdir(tmp_path)
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("NOT JSON", encoding="utf-8")
        good_file = self._make_conv_file(tmp_path, "good.json", [
            {"author": "bob", "timestamp": "2026-01-01T11:00:00Z", "content": "fix the bug"},
        ])
        mock_claude.return_value = json.dumps({
            "dev_topics": [{"title": "Fix Bug", "category": "bug", "priority": "high",
                           "summary": "Bug fix", "keywords": ["bug"], "actionable": True,
                           "estimated_complexity": "small", "relevant_message_indices": [0]}]
        })

        result = analyze_conversations([bad_file, good_file], "2026-01-01")

        # bad_file returns empty dict -> 0 messages, only good_file is analyzed
        assert result.dev_topics_found == 1
        assert result.dev_topics[0].title == "Fix Bug"
