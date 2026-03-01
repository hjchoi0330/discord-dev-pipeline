"""pipeline 및 config 모듈 단위 테스트."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# pipeline 모듈은 import 시 load_dotenv()를 호출하므로 주의
import pipeline
from collector.config import CollectorConfig, load_config


# ── _load_config ─────────────────────────────────────────────────────


class TestLoadConfig:
    def test_with_config_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "pipeline": {"auto_execute": True},
            "analyzer": {"model": "claude-sonnet-4-6"},
        }))
        monkeypatch.setattr(pipeline, "_CONFIG_PATH", config_file)

        cfg = pipeline._load_config()
        assert cfg["pipeline"]["auto_execute"] is True
        assert cfg["analyzer"]["model"] == "claude-sonnet-4-6"

    def test_without_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_CONFIG_PATH", tmp_path / "nonexistent.yaml")
        cfg = pipeline._load_config()
        assert cfg == {}

    def test_empty_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        monkeypatch.setattr(pipeline, "_CONFIG_PATH", config_file)
        cfg = pipeline._load_config()
        assert cfg == {}


# ── _find_conversation_files ─────────────────────────────────────────


class TestFindConversationFiles:
    def test_finds_matching_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()

        (conv_dir / "2026-01-01_server_general.json").write_text("{}")
        (conv_dir / "2026-01-01_server_dev.json").write_text("{}")
        (conv_dir / "2026-01-03_server_general.json").write_text("{}")

        files = pipeline._find_conversation_files("2026-01-01")
        # 당일 2개 + 전날(2025-12-31) 없음 = 2개
        assert len(files) == 2

    def test_includes_previous_day_files(self, tmp_path, monkeypatch):
        """날짜 경계를 넘는 대화를 위해 전날 파일도 포함해야 한다."""
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()

        (conv_dir / "2026-01-01_server_general.json").write_text("{}")
        (conv_dir / "2026-01-02_server_general.json").write_text("{}")
        (conv_dir / "2026-01-03_server_other.json").write_text("{}")

        # 2026-01-02로 검색 → 당일(01-02) + 전날(01-01) = 2개
        files = pipeline._find_conversation_files("2026-01-02")
        assert len(files) == 2
        filenames = [f.name for f in files]
        assert "2026-01-01_server_general.json" in filenames
        assert "2026-01-02_server_general.json" in filenames

    def test_no_matching_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()

        files = pipeline._find_conversation_files("2026-01-01")
        assert files == []

    def test_no_conversations_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        files = pipeline._find_conversation_files("2026-01-01")
        assert files == []


# ── _get_already_analyzed_files ──────────────────────────────────────


class TestGetAlreadyAnalyzedFiles:
    def test_returns_source_files_from_analysis(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        analysis_file = analysis_dir / "2026-01-01_analysis.json"
        analysis_file.write_text(json.dumps({
            "source_files": ["data/conversations/a.json", "data/conversations/b.json"],
            "dev_topics_found": 1,
        }))

        files, mtime = pipeline._get_already_analyzed_files("2026-01-01")
        assert files == {"data/conversations/a.json", "data/conversations/b.json"}
        assert mtime > 0

    def test_returns_empty_when_no_analysis(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        files, mtime = pipeline._get_already_analyzed_files("2026-01-01")
        assert files == set()
        assert mtime == 0.0

    def test_returns_empty_on_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        (analysis_dir / "2026-01-01_analysis.json").write_text("not json")

        files, mtime = pipeline._get_already_analyzed_files("2026-01-01")
        assert files == set()
        assert mtime == 0.0

    def test_skips_already_analyzed_in_pipeline(self, tmp_path, monkeypatch):
        """Unmodified already-analyzed files should be skipped."""
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_CONFIG_PATH", tmp_path / "no.yaml")

        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()
        file_a = conv_dir / "2026-01-01_server_general.json"
        file_a.write_text(json.dumps({"messages": [
            {"author": "a", "timestamp": "2026-01-01T10:00:00Z", "content": "hello"},
        ]}))
        file_b = conv_dir / "2026-01-01_server_dev.json"
        file_b.write_text(json.dumps({"messages": [
            {"author": "b", "timestamp": "2026-01-01T11:00:00Z", "content": "new feature needed"},
        ]}))

        # Mark file_a as already analyzed (analysis created AFTER file_a)
        import time
        time.sleep(0.05)  # ensure analysis mtime > file_a mtime
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        (analysis_dir / "2026-01-01_analysis.json").write_text(json.dumps({
            "source_files": [str(file_a)],
            "dev_topics_found": 0,
        }))

        from unittest.mock import MagicMock
        mock_analyze = MagicMock()
        mock_analyze.return_value = MagicMock(
            dev_topics=[], dev_topics_found=0,
        )
        with patch("analyzer.analyzer.analyze_conversations", mock_analyze):
            pipeline.run_pipeline(date="2026-01-01")

        assert mock_analyze.called
        called_files = mock_analyze.call_args[0][0]
        called_names = [f.name for f in called_files]
        assert "2026-01-01_server_dev.json" in called_names
        assert "2026-01-01_server_general.json" not in called_names

    def test_modified_file_triggers_reanalysis(self, tmp_path, monkeypatch):
        """A conversation file modified after analysis should be re-analyzed."""
        monkeypatch.setattr(pipeline, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_CONFIG_PATH", tmp_path / "no.yaml")

        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir()
        file_a = conv_dir / "2026-01-01_server_general.json"
        file_a.write_text(json.dumps({"messages": [
            {"author": "a", "timestamp": "2026-01-01T10:00:00Z", "content": "hello"},
        ]}))

        # Create analysis first
        import time
        analysis_dir = tmp_path / "analysis"
        analysis_dir.mkdir()
        (analysis_dir / "2026-01-01_analysis.json").write_text(json.dumps({
            "source_files": [str(file_a)],
            "dev_topics_found": 0,
        }))

        # Now modify the conversation file AFTER analysis
        time.sleep(0.05)
        file_a.write_text(json.dumps({"messages": [
            {"author": "a", "timestamp": "2026-01-01T10:00:00Z", "content": "hello"},
            {"author": "b", "timestamp": "2026-01-01T12:00:00Z", "content": "build a new API"},
        ]}))

        from unittest.mock import MagicMock
        mock_analyze = MagicMock()
        mock_analyze.return_value = MagicMock(
            dev_topics=[], dev_topics_found=0,
        )
        with patch("analyzer.analyzer.analyze_conversations", mock_analyze):
            pipeline.run_pipeline(date="2026-01-01")

        # file_a should be re-analyzed because it was modified after analysis
        assert mock_analyze.called
        called_files = mock_analyze.call_args[0][0]
        called_names = [f.name for f in called_files]
        assert "2026-01-01_server_general.json" in called_names


# ── _print_summary ───────────────────────────────────────────────────


class TestPrintSummary:
    def test_with_results(self):
        from executor.executor import ExecutionResult
        results = [
            ExecutionResult("a.md", "", "", True, ""),
            ExecutionResult("b.md", "", "", False, "Something failed"),
        ]
        # _print_summary는 logger.info만 호출하므로 예외 없이 실행되면 통과
        pipeline._print_summary(results)

    def test_empty_results(self):
        pipeline._print_summary([])


# ── CollectorConfig (load_config) ────────────────────────────────────


class TestCollectorConfig:
    def test_load_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-123")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {"token": ""},
            "collector": {"whisper_model": "tiny"},
        }))

        cfg = load_config(str(config_file))
        assert cfg.token == "test-token-123"
        assert cfg.whisper_model == "tiny"

    def test_load_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {"token": "yaml-token"},
            "collector": {
                "whisper_language": "en",
                "whisper_device": "cuda",
            },
        }))

        cfg = load_config(str(config_file))
        assert cfg.token == "yaml-token"
        assert cfg.whisper_language == "en"
        assert cfg.whisper_device == "cuda"

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"discord": {"token": ""}}))

        with pytest.raises(ValueError, match="봇 토큰이 필요합니다"):
            load_config(str(config_file))

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {"token": "yaml-token"},
            "collector": {"whisper_model": "small"},
        }))

        cfg = load_config(str(config_file))
        assert cfg.token == "env-token"
        assert cfg.whisper_model == "large-v3"

    def test_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        cfg = load_config(str(config_file))
        assert cfg.whisper_model == "small"
        assert cfg.whisper_language == "ko"
        assert cfg.whisper_device == "cpu"
        assert cfg.whisper_compute_type == "int8"
        assert cfg.monitored_voice_channels == []
        assert cfg.ignored_voice_channels == []

    def test_no_config_file(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        cfg = load_config("/nonexistent/config.yaml")
        assert cfg.token == "tok"
        assert isinstance(cfg, CollectorConfig)
