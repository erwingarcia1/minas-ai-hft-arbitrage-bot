"""Config loading: the shipped default must be valid; broken configs must be fatal."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arb.config.loader import ConfigError, load_settings
from arb.models import VenueName

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def test_shipped_default_config_is_valid() -> None:
    settings = load_settings(REPO_CONFIG)
    assert settings.symbols == ["BTC/USDT", "ETH/USDT"]
    assert set(settings.enabled_venues) == {
        VenueName.BINANCE,
        VenueName.BYBIT,
        VenueName.OKX,
        VenueName.BITGET,
    }
    assert settings.signals.min_net_edge_bps > 0
    assert settings.risk.staleness_ms == 500


def test_missing_file_is_fatal() -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(Path("/nonexistent/config.yaml"))


def _valid_raw() -> dict:
    return yaml.safe_load(REPO_CONFIG.read_text())


def test_single_enabled_venue_is_fatal(tmp_path: Path) -> None:
    raw = _valid_raw()
    for name in ("bybit", "okx", "bitget"):
        raw["venues"][name]["enabled"] = False
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="at least two venues"):
        load_settings(path)


def test_unknown_key_is_fatal(tmp_path: Path) -> None:
    # A typo like `min_net_edge_bp` must not silently disable a threshold.
    raw = _valid_raw()
    raw["signals"]["min_net_edge_bp"] = raw["signals"].pop("min_net_edge_bps")
    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_bad_symbol_format_is_fatal(tmp_path: Path) -> None:
    raw = _valid_raw()
    raw["symbols"] = ["BTCUSDT"]
    path = tmp_path / "sym.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="BTC/USDT"):
        load_settings(path)


def test_invalid_yaml_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("symbols: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_settings(path)
