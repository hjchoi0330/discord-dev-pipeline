"""Configuration for the Discord collector bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CollectorConfig:
    """Discord voice recording bot configuration.

    Loaded from config.yaml; individual values can be overridden via environment variables.
    """

    token: str
    data_dir: str = "data/conversations"
    # Voice channel settings
    whisper_model: str = "small"       # tiny/base/small/medium/large-v3
    whisper_language: str = "ko"       # Transcription language code (ko, en, ja, etc.)
    whisper_device: str = "cpu"        # cpu / cuda
    whisper_compute_type: str = "int8" # int8 / float16 / float32
    # Channel filters
    monitored_voice_channels: list[str] = field(default_factory=list)  # empty list = all channels
    ignored_voice_channels: list[str] = field(default_factory=list)
    # Auto-run pipeline after recording
    auto_pipeline: bool = False
    # Bot pipeline execution mode: "full" (plan+execute) | "plan" (plan only)
    pipeline_mode: str = "full"


def load_config(config_path: str = "config.yaml") -> CollectorConfig:
    """Loads configuration from a YAML file and environment variables.

    DISCORD_BOT_TOKEN is required (env var or config.yaml).
    All other values fall back to the dataclass defaults.
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
            "Bot token is required. Set the DISCORD_BOT_TOKEN environment variable or discord.token in config.yaml."
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
