"""Shared configuration helpers for the Discord Dev Pipeline."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config.yaml")


def resolve_data_dir(config_path: Path | None = None) -> Path:
    """Resolve the pipeline data directory from environment or config.

    Resolution order:
    1. ``DATA_DIR`` environment variable.
    2. ``pipeline.data_dir`` key in *config_path* (defaults to ``config.yaml``).
    3. ``Path("data")`` as a final fallback.

    Args:
        config_path: Path to the YAML config file.  Defaults to ``config.yaml``
                     in the current working directory.

    Returns:
        Resolved data directory as a :class:`~pathlib.Path`.
    """
    env_val = os.environ.get("DATA_DIR")
    if env_val:
        return Path(env_val)

    cfg_file = config_path if config_path is not None else _DEFAULT_CONFIG_PATH
    if cfg_file.exists():
        try:
            import yaml  # type: ignore[import-untyped]

            with cfg_file.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            pipeline_section = data.get("pipeline", {})
            data_dir_val = pipeline_section.get("data_dir")
            if data_dir_val:
                return Path(data_dir_val)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read data_dir from %s: %s", cfg_file, exc)

    return Path("data")
