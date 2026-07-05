"""YAML -> validated ``Settings``.

Failure modes:
- Missing file, unreadable file, invalid YAML, or schema violations all raise
  ``ConfigError`` with full context. Nothing is defaulted-around: a broken
  config must stop the process at boot, never mid-session.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from arb.models import Settings


class ConfigError(RuntimeError):
    """Raised for any configuration problem; always fatal at boot."""


def load_settings(path: Path) -> Settings:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config root in {path} must be a mapping")

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config schema errors in {path}:\n{exc}") from exc
