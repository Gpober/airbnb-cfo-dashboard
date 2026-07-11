#!/usr/bin/env python3
"""
remote_settings.py
==================

Runtime-adjustable bot controls, polled from Supabase (``kalshi_settings``).

This lets the dashboard tweak safe strategy knobs -- trade size, entry band,
stop/take, and a new-entry pause switch -- without a redeploy. The worker polls
the table every ``KALSHI_SETTINGS_REFRESH_SEC`` seconds and applies changes to
the live Config.

SAFETY: only a fixed WHITELIST of parameters is adjustable this way. The
real-money gate (``KALSHI_DEMO`` / ``KALSHI_DRY_RUN``), API keys, and the series
are NEVER read from here -- they stay environment-only, so no web form can move
the bot from demo/dry-run to real money. Every value is validated and clamped
before it touches Config, so a malformed row can't break trading.

When enabled (default on wherever Supabase is configured), a stored value
OVERRIDES the corresponding env default for that parameter -- the table is the
control surface. Turn the whole mechanism off with
``KALSHI_REMOTE_SETTINGS_ENABLED=false``.

Env vars (documented in the README):
  KALSHI_REMOTE_SETTINGS_ENABLED   master switch (default true)
  KALSHI_SETTINGS_REFRESH_SEC      seconds between polls (default 30)
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple


def _as_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_bool(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return None


def coerce_settings(settings: dict) -> dict:
    """Return only whitelisted, validated, clamped settings.

    Anything not in the whitelist (notably demo/dry_run/api keys) is dropped.
    """
    out: dict = {}

    if "target_notional_usd" in settings:
        f = _as_float(settings["target_notional_usd"])
        if f is not None and f > 0:
            out["target_notional_usd"] = min(f, 100_000.0)

    if "stop_loss_cents" in settings:
        i = _as_int(settings["stop_loss_cents"])
        if i is not None:
            out["stop_loss_cents"] = max(0, min(99, i))

    if "take_profit_cents" in settings:
        i = _as_int(settings["take_profit_cents"])
        if i is not None:
            out["take_profit_cents"] = max(1, min(100, i))

    if "entry_min_cents" in settings:
        i = _as_int(settings["entry_min_cents"])
        if i is not None:
            out["entry_min_cents"] = max(1, min(99, i))

    if "entry_max_cents" in settings:
        i = _as_int(settings["entry_max_cents"])
        if i is not None:
            out["entry_max_cents"] = max(1, min(99, i))

    if "entries_paused" in settings:
        b = _as_bool(settings["entries_paused"])
        if b is not None:
            out["entries_paused"] = b

    return out


def apply_settings(cfg, settings: dict,
                   logger: Optional[logging.Logger] = None) -> Tuple[List[str], Optional[bool]]:
    """Apply whitelisted settings to ``cfg`` in place.

    Returns (changes, paused) where ``changes`` is a human-readable list of what
    changed and ``paused`` is the new-entry pause flag (None if unset).
    """
    coerced = coerce_settings(settings)

    # Entry band must stay ordered; drop the band change if it would invert.
    p_min = coerced.get("entry_min_cents", getattr(cfg, "entry_min_cents", 0))
    p_max = coerced.get("entry_max_cents", getattr(cfg, "entry_max_cents", 100))
    if p_min > p_max:
        coerced.pop("entry_min_cents", None)
        coerced.pop("entry_max_cents", None)
        if logger:
            logger.warning("remote settings: entry band min>max ignored (%s>%s)", p_min, p_max)

    paused = coerced.pop("entries_paused", None)

    changes: List[str] = []
    for key, val in coerced.items():
        cur = getattr(cfg, key, None)
        if cur != val:
            setattr(cfg, key, val)
            changes.append(f"{key}: {cur} -> {val}")
    return changes, paused


class RemoteSettings:
    """Polls ``kalshi_settings`` and applies safe overrides to the live Config."""

    def __init__(self, cfg, logger: logging.Logger, sink=None):
        self.cfg = cfg
        self.log = logger
        self.sink = sink
        self.enabled = bool(getattr(cfg, "remote_settings_enabled", True)
                            and sink is not None and getattr(sink, "enabled", False))
        self.paused = False
        self._last = 0.0
        self.refresh_sec = float(getattr(cfg, "settings_refresh_sec", 30.0))
        if self.enabled:
            logger.info("remote settings enabled (poll every %.0fs)", self.refresh_sec)

    def _fetch(self) -> dict:
        base = getattr(self.sink, "_base", "")
        resp = self.sink._session.get(
            f"{base}/kalshi_settings", params={"select": "key,value"},
            timeout=getattr(self.cfg, "request_timeout_sec", 15.0),
        )
        if resp.status_code >= 300:
            self.log.warning("remote settings fetch failed status=%s body=%s",
                             resp.status_code, resp.text[:200])
            return {}
        return {r["key"]: r["value"] for r in resp.json()}

    def refresh(self, now: float) -> None:
        if not self.enabled:
            return
        try:
            settings = self._fetch()
        except Exception as exc:  # never let a settings read crash trading
            self.log.warning("remote settings refresh error: %s", exc)
            self._last = now
            return
        changes, paused = apply_settings(self.cfg, settings, self.log)
        if paused is not None:
            if paused != self.paused:
                changes.append(f"entries_paused: {self.paused} -> {paused}")
            self.paused = paused
        self._last = now
        if changes:
            self.log.info("remote settings applied: %s", "; ".join(changes))

    def maybe_refresh(self, now: float) -> None:
        if self.enabled and (now - self._last) >= self.refresh_sec:
            self.refresh(now)
