"""Configuration for the Discord collector bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CollectorConfig:
    """Discord 음성 녹음 봇 설정.

    config.yaml에서 로드되며, 환경변수로 오버라이드 가능합니다.
    """

    token: str
    data_dir: str = "data/conversations"
    # 음성 채널 설정
    whisper_model: str = "small"       # tiny/base/small/medium/large-v3
    whisper_language: str = "ko"       # 전사 언어 코드 (ko, en, ja 등)
    whisper_device: str = "cpu"        # cpu / cuda
    whisper_compute_type: str = "int8" # int8 / float16 / float32
    # 채널 필터
    monitored_voice_channels: list[str] = field(default_factory=list)  # 빈 목록 = 모두
    ignored_voice_channels: list[str] = field(default_factory=list)
    # 파이프라인 자동 실행
    auto_pipeline: bool = False
    # 봇 파이프라인 실행 모드: "full" (계획+실행) | "plan" (계획까지만)
    pipeline_mode: str = "full"


def load_config(config_path: str = "config.yaml") -> CollectorConfig:
    """YAML 파일과 환경변수로부터 설정을 로드합니다.

    DISCORD_BOT_TOKEN은 필수입니다 (env var 또는 config.yaml).
    나머지는 데이터클래스 기본값으로 폴백합니다.
    """
    base: dict[str, Any] = {}

    path = Path(config_path)
    if path.exists():
        with path.open() as fh:
            loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                base = loaded

    discord_cfg: dict = base.get("discord", {})
    collector_cfg: dict = base.get("collector", {})

    token = os.environ.get("DISCORD_BOT_TOKEN", discord_cfg.get("token", ""))
    if not token:
        raise ValueError(
            "봇 토큰이 필요합니다. DISCORD_BOT_TOKEN 환경변수 또는 config.yaml의 discord.token을 설정하세요."
        )

    return CollectorConfig(
        token=token,
        data_dir=os.environ.get("DATA_DIR", base.get("data_dir", "data/conversations")),
        whisper_model=os.environ.get(
            "WHISPER_MODEL", collector_cfg.get("whisper_model", "small")
        ),
        whisper_language=os.environ.get(
            "WHISPER_LANGUAGE", collector_cfg.get("whisper_language", "ko")
        ),
        whisper_device=os.environ.get(
            "WHISPER_DEVICE", collector_cfg.get("whisper_device", "cpu")
        ),
        whisper_compute_type=os.environ.get(
            "WHISPER_COMPUTE_TYPE", collector_cfg.get("whisper_compute_type", "int8")
        ),
        monitored_voice_channels=collector_cfg.get("monitored_voice_channels", []),
        ignored_voice_channels=collector_cfg.get("ignored_voice_channels", []),
        auto_pipeline=collector_cfg.get("auto_pipeline", False),
        pipeline_mode=collector_cfg.get("pipeline_mode", "full"),
    )
