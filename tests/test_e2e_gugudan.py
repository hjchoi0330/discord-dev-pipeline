"""E2E test: Full pipeline validation for the Golang multiplication-table scenario.

Tests the complete flow: transcript -> analysis -> plan -> executor (mock).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import pipeline
from run_demo import (
    _mock_call_claude_analyzer,
    _mock_call_claude_planner,
    _mock_executor_for_gugudan,
)


@pytest.fixture
def gugudan_env(tmp_path, monkeypatch):
    """Set up the environment for the multiplication-table E2E test.

    Directory structure:
      tmp_path/
        data/
          conversations/
            2026-03-01_devteam_general.json
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(pipeline, "_DATA_DIR", data_dir)
    monkeypatch.setattr(pipeline, "_CONFIG_PATH", tmp_path / "no.yaml")
    monkeypatch.chdir(tmp_path)

    # Create the conversations directory and transcript
    conv_dir = data_dir / "conversations"
    conv_dir.mkdir()
    transcript = conv_dir / "2026-03-01_devteam_general.json"
    transcript.write_text(json.dumps({
        "date": "2026-03-01",
        "channel": "general",
        "guild": "devteam",
        "type": "voice_transcription",
        "session_start": "2026-03-01T10:00:00Z",
        "session_end": "2026-03-01T10:15:00Z",
        "messages": [
            {
                "author": "김민수",
                "content": "오늘 간단한 거 하나 만들어보자. Go로 구구단 프로그램 만들어서 신입분들 온보딩 예제로 쓰면 좋겠어.",
                "timestamp": "2026-03-01T10:01:00Z"
            },
            {
                "author": "이지영",
                "content": "구구단? 너무 간단하지 않아? 그래도 Go 기초 연습용으로는 괜찮겠다. CLI로 단수 입력받아서 출력하는 거지?",
                "timestamp": "2026-03-01T10:02:30Z"
            },
            {
                "author": "박준혁",
                "content": "맞아. main.go 하나에 fmt.Scanf로 입력받고, for 루프로 1부터 9까지 곱해서 출력하면 돼. 간단하게 가자.",
                "timestamp": "2026-03-01T10:03:45Z"
            },
            {
                "author": "김민수",
                "content": "좋아. 에러 처리도 넣자. 숫자가 아닌 값 입력하면 안내 메시지 출력하고, 1~9 범위 밖이면 경고해주는 정도.",
                "timestamp": "2026-03-01T10:05:10Z"
            },
            {
                "author": "이지영",
                "content": "그럼 정리하면, Go로 구구단 CLI 프로그램 하나 만드는 거. data/result 디렉토리에 프로젝트 폴더 만들어서 진행하자.",
                "timestamp": "2026-03-01T10:06:20Z"
            }
        ]
    }), encoding="utf-8")

    return tmp_path


class TestE2EGugudanPipeline:
    """Validates the full pipeline for the multiplication-table scenario."""

    def test_analysis_extracts_gugudan_topic(self, gugudan_env):
        """Step 1: The multiplication-table topic should be extracted from the transcript."""
        from analyzer.analyzer import analyze_conversations

        conv_files = list((gugudan_env / "data" / "conversations").glob("2026-03-01_*.json"))
        assert len(conv_files) == 1

        with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer):
            result = analyze_conversations(conv_files, "2026-03-01")

        assert result.dev_topics_found == 1
        topic = result.dev_topics[0]
        assert "구구단" in topic.title
        assert topic.category == "feature"
        assert topic.actionable is True
        assert topic.estimated_complexity == "small"

    def test_plan_generation_for_gugudan(self, gugudan_env):
        """Step 2: A multiplication-table plan should be generated from the analysis result."""
        from analyzer.analyzer import analyze_conversations
        from planner.planner import generate_plans

        conv_files = list((gugudan_env / "data" / "conversations").glob("2026-03-01_*.json"))

        with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer):
            analysis = analyze_conversations(conv_files, "2026-03-01")

        plan_dir = gugudan_env / "data" / "plans"
        with patch("planner.planner._call_claude", side_effect=_mock_call_claude_planner):
            plan_files = generate_plans(analysis, plan_dir)

        assert len(plan_files) == 1
        plan_content = plan_files[0].read_text(encoding="utf-8")
        assert "Go 구구단" in plan_content
        assert "## Claude Code Prompt" in plan_content
        assert "main.go" in plan_content

    def test_executor_creates_go_files(self, gugudan_env):
        """Step 3: The executor should create Go files under data/result/go-gugudan."""
        result = _mock_executor_for_gugudan("")

        assert "Successfully" in result

        # chdir is gugudan_env (tmp_path), so data/result/go-gugudan is under tmp_path
        result_dir = gugudan_env / "data" / "result" / "go-gugudan"
        assert result_dir.exists()

        # Verify main.go
        main_go = result_dir / "main.go"
        assert main_go.exists()
        content = main_go.read_text(encoding="utf-8")
        assert "package main" in content
        assert "fmt.Scan" in content
        assert "1~9" in content
        assert "for i := 1; i <= 9; i++" in content

        # Verify go.mod
        go_mod = result_dir / "go.mod"
        assert go_mod.exists()
        mod_content = go_mod.read_text(encoding="utf-8")
        assert "module go-gugudan" in mod_content
        assert "go 1.21" in mod_content

    def test_full_pipeline_end_to_end(self, gugudan_env):
        """Full pipeline E2E: transcript -> analysis -> plan generation."""
        with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer), \
             patch("planner.planner._call_claude", side_effect=_mock_call_claude_planner):
            pipeline.run_pipeline(date="2026-03-01", force=True)

        # Verify analysis result (analyzer saves to Path("data/analysis"), chdir=tmp_path)
        analysis_path = gugudan_env / "data" / "analysis" / "2026-03-01_analysis.json"
        assert analysis_path.exists()
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        assert analysis["dev_topics_found"] == 1
        assert "구구단" in analysis["dev_topics"][0]["title"]

        # Verify plan files (pipeline._DATA_DIR / "plans")
        plan_dir = gugudan_env / "data" / "plans"
        plans = list(plan_dir.glob("2026-03-01_*.md"))
        assert len(plans) == 1
        plan_content = plans[0].read_text(encoding="utf-8")
        assert "## Claude Code Prompt" in plan_content

    def test_already_analyzed_files_skipped(self, gugudan_env):
        """Already-analyzed files should not be re-analyzed."""
        # First run
        with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer), \
             patch("planner.planner._call_claude", side_effect=_mock_call_claude_planner):
            pipeline.run_pipeline(date="2026-03-01", force=True)

        # Second run (force=False) - files already analyzed, should be skipped
        mock_analyze = MagicMock()
        with patch("analyzer.analyzer.analyze_conversations", mock_analyze):
            pipeline.run_pipeline(date="2026-03-01", force=False)

        # analyze_conversations should not be called (all files already analyzed)
        mock_analyze.assert_not_called()

    def test_new_file_analyzed_with_existing(self, gugudan_env):
        """When a new file is added despite an existing analysis, only the new file should be analyzed."""
        # First run
        with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer), \
             patch("planner.planner._call_claude", side_effect=_mock_call_claude_planner):
            pipeline.run_pipeline(date="2026-03-01", force=True)

        # Add a new conversation file
        conv_dir = gugudan_env / "data" / "conversations"
        new_file = conv_dir / "2026-03-01_devteam_dev.json"
        new_file.write_text(json.dumps({
            "messages": [
                {"author": "alice", "timestamp": "2026-03-01T14:00:00Z",
                 "content": "API 엔드포인트 추가해야 해"}
            ]
        }), encoding="utf-8")

        # Second run (force=False) - only the new file should be analyzed
        mock_analyze = MagicMock()
        mock_analyze.return_value = MagicMock(dev_topics=[], dev_topics_found=0)
        with patch("analyzer.analyzer.analyze_conversations", mock_analyze):
            pipeline.run_pipeline(date="2026-03-01", force=False)

        # analyze_conversations should be called with only the new file
        assert mock_analyze.called
        called_files = mock_analyze.call_args[0][0]
        called_names = [f.name for f in called_files]
        assert "2026-03-01_devteam_dev.json" in called_names
        assert "2026-03-01_devteam_general.json" not in called_names
