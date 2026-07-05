"""Configuration loading.

Failure modes: see ``arb.config.loader`` — every load error is fatal at boot
by design; this system never starts on a partially-valid config.
"""

from arb.config.loader import load_settings

__all__ = ["load_settings"]
