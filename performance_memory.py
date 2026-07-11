#!/usr/bin/env python3
"""
performance_memory.py
=====================

Optional *learning* layer for the Kalshi hourly BTC bot.

The bot logs every closed trade to Supabase (``kalshi_trades``). This module
reads that track record back and turns it into an adaptive signal, so the bot
gets better as it accumulates evidence about what actually pays.

Design principle (same as the AI layer): **history can only make the bot MORE
conservative.** From its own realized results it may

  * shrink position size when recent expectancy is negative or trending down,
  * skip an entry-price bucket that has a proven negative edge,
  * pause new entries after a losing streak (open positions still exit normally).

It NEVER sizes up, widens the band, or loosens a limit -- so a hot streak can
never talk the bot into betting bigger. The deterministic stop-loss and
take-profit always fire regardless.

Everything degrades gracefully: no Supabase, too few trades (cold start), or a
fetch error all resolve to "no opinion" (size multiplier 1.0, no pause) and the
bot runs on its base rules.

The stats are also injected into the AI snapshot, so Claude can reason over the
same realized performance.

Env vars (documented in the README):
  KALSHI_LEARN_ENABLED            turn the layer on (default false)
  KALSHI_LEARN_MIN_TRADES         closed trades required before adapting (20)
  KALSHI_LEARN_LOOKBACK           most-recent trades to consider (200)
  KALSHI_LEARN_REFRESH_SEC        seconds between history refreshes (300)
  KALSHI_LEARN_MIN_SCALAR         floor on the size multiplier (0.5)
  KALSHI_LEARN_PAUSE_LOSS_STREAK  consecutive losers that pause entries (5)
  KALSHI_LEARN_BUCKET_MIN_TRADES  trades in a price bucket before it can veto (8)
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Pure, unit-testable analytics (no network)
# --------------------------------------------------------------------------- #


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def empty_stats() -> dict:
    """The neutral 'no opinion' stats block."""
    return {
        "n": 0, "win_rate": None, "avg_net_pnl_cents": None,
        "net_total_cents": 0, "expectancy_per_1000": None,
        "recent_streak": 0, "trend": "unknown", "by_entry": {},
    }


def summarize_trades(trades: List[dict], lookback: int = 200) -> dict:
    """Reduce a list of closed-trade rows to a performance summary.

    Each row is expected to carry ``net_pnl`` (cents), ``entry_price`` (cents),
    ``notional`` (cents) and ``closed_at`` (already sorted ascending by the
    caller). Missing/garbage fields degrade to zero rather than raising.
    """
    rows = [t for t in trades if t is not None][-lookback:]
    n = len(rows)
    if n == 0:
        return empty_stats()

    nets = [_num(t.get("net_pnl")) for t in rows]
    wins = sum(1 for x in nets if x > 0)
    net_total = sum(nets)

    # Expectancy per $1000 staked: mean per-trade return fraction * $1000.
    fracs = []
    for t in rows:
        notional = _num(t.get("notional"))
        if notional > 0:
            fracs.append(_num(t.get("net_pnl")) / notional)
    expectancy_per_1000 = (sum(fracs) / len(fracs) * 1000.0) if fracs else None

    # Recent streak: signed run length at the tail (+ wins, - losses).
    streak = 0
    for x in reversed(nets):
        if x > 0:
            if streak < 0:
                break
            streak += 1
        elif x < 0:
            if streak > 0:
                break
            streak -= 1
        else:
            break

    # Trend: compare the win rate of the older half vs the newer half.
    trend = "unknown"
    if n >= 16:
        half = n // 2
        old = nets[:half]
        new = nets[half:]
        old_wr = sum(1 for x in old if x > 0) / len(old)
        new_wr = sum(1 for x in new if x > 0) / len(new)
        if new_wr >= old_wr + 0.1:
            trend = "improving"
        elif new_wr <= old_wr - 0.1:
            trend = "declining"
        else:
            trend = "flat"

    # Per-entry-price buckets: does buying at 85c pay better than 90c?
    by_entry: dict = {}
    for t in rows:
        price = int(_num(t.get("entry_price")))
        b = by_entry.setdefault(price, {"n": 0, "wins": 0, "net_total_cents": 0.0,
                                        "frac_sum": 0.0, "frac_n": 0})
        net = _num(t.get("net_pnl"))
        b["n"] += 1
        b["wins"] += 1 if net > 0 else 0
        b["net_total_cents"] += net
        notional = _num(t.get("notional"))
        if notional > 0:
            b["frac_sum"] += net / notional
            b["frac_n"] += 1
    for price, b in by_entry.items():
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else None
        b["expectancy_frac"] = (b["frac_sum"] / b["frac_n"]) if b["frac_n"] else None
        b.pop("frac_sum", None)
        b.pop("frac_n", None)

    return {
        "n": n,
        "win_rate": wins / n,
        "avg_net_pnl_cents": net_total / n,
        "net_total_cents": net_total,
        "expectancy_per_1000": expectancy_per_1000,
        "recent_streak": streak,
        "trend": trend,
        "by_entry": by_entry,
    }


def compute_throttle(stats: dict, min_trades: int, min_scalar: float,
                     pause_loss_streak: int) -> dict:
    """Turn stats into a size multiplier + pause flag. Only ever tightens.

    Returns {"scalar": float in [min_scalar, 1.0], "pause": bool, "notes": str}.
    """
    n = stats.get("n", 0)
    if n < min_trades:
        return {"scalar": 1.0, "pause": False,
                "notes": f"cold-start ({n}/{min_trades} trades) -> rules stand"}

    scalar = 1.0
    notes = []
    exp = stats.get("expectancy_per_1000")
    if exp is not None and exp < 0:
        scalar *= 0.5
        notes.append("negative expectancy -> half size")
    if stats.get("trend") == "declining":
        scalar *= 0.75
        notes.append("declining trend -> trim size")
    scalar = max(min_scalar, min(1.0, scalar))

    streak = stats.get("recent_streak", 0)
    pause = streak <= -abs(pause_loss_streak)
    if pause:
        notes.append(f"{-streak} straight losses -> pause new entries")

    return {"scalar": scalar, "pause": pause,
            "notes": "; ".join(notes) or "within normal range"}


def should_avoid_entry(stats: dict, entry_price: int,
                       bucket_min_trades: int) -> Tuple[bool, str]:
    """True if THIS entry price has a proven negative edge (enough samples)."""
    b = stats.get("by_entry", {}).get(int(entry_price))
    if not b or b.get("n", 0) < bucket_min_trades:
        return False, ""
    exp = b.get("expectancy_frac")
    if exp is not None and exp < 0:
        return True, (f"entry@{entry_price}c has negative edge over "
                      f"{b['n']} trades (win_rate={b.get('win_rate'):.0%})")
    return False, ""


def snapshot_block(stats: dict) -> Optional[dict]:
    """Compact, JSON-friendly performance summary for the AI snapshot."""
    if not stats or stats.get("n", 0) == 0:
        return None
    by_entry = {
        str(p): {"n": b["n"], "win_rate": round(b["win_rate"], 3) if b.get("win_rate") is not None else None,
                 "expectancy_frac": round(b["expectancy_frac"], 4) if b.get("expectancy_frac") is not None else None}
        for p, b in sorted(stats.get("by_entry", {}).items())
    }
    return {
        "trades": stats["n"],
        "win_rate": round(stats["win_rate"], 3) if stats.get("win_rate") is not None else None,
        "expectancy_per_1000_usd": round(stats["expectancy_per_1000"], 2)
        if stats.get("expectancy_per_1000") is not None else None,
        "recent_streak": stats.get("recent_streak", 0),
        "trend": stats.get("trend", "unknown"),
        "by_entry_price": by_entry,
    }


# --------------------------------------------------------------------------- #
# Live memory: reads history from Supabase and holds the current signal
# --------------------------------------------------------------------------- #


class PerformanceMemory:
    """Reads the bot's own trade history from Supabase and adapts from it.

    Reuses the SupabaseSink's authenticated session for reads. When disabled
    (learning off or no sink) every accessor returns the neutral value, so
    callers need no special-casing.
    """

    def __init__(self, cfg, logger: logging.Logger, sink=None):
        self.cfg = cfg
        self.log = logger
        self.sink = sink
        self.enabled = bool(getattr(cfg, "learn_enabled", False)
                            and sink is not None and getattr(sink, "enabled", False))
        self.stats = empty_stats()
        self._throttle = {"scalar": 1.0, "pause": False, "notes": "no data"}
        self._last_refresh = 0.0

        if getattr(cfg, "learn_enabled", False) and not self.enabled:
            logger.warning("performance learning requested but disabled: "
                           "Supabase sink not configured (needs SUPABASE_URL + "
                           "SUPABASE_SERVICE_ROLE_KEY)")
        elif self.enabled:
            logger.info("performance learning enabled (min_trades=%s, lookback=%s)",
                        getattr(cfg, "learn_min_trades", 20),
                        getattr(cfg, "learn_lookback", 200))

    # -- data -------------------------------------------------------------- #

    def _fetch_trades(self) -> List[dict]:
        base = getattr(self.sink, "_base", "")
        params = {
            "select": "net_pnl,entry_price,notional,contracts,hold_secs,closed_at",
            "order": "closed_at.desc",
            "limit": str(int(getattr(self.cfg, "learn_lookback", 200))),
        }
        resp = self.sink._session.get(
            f"{base}/kalshi_trades", params=params,
            timeout=getattr(self.cfg, "request_timeout_sec", 15.0),
        )
        if resp.status_code >= 300:
            self.log.warning("learning: trade history fetch failed status=%s body=%s",
                             resp.status_code, resp.text[:200])
            return []
        rows = resp.json()
        rows.reverse()  # PostgREST gave newest-first; analytics want oldest-first
        return rows

    def refresh(self, now: float) -> None:
        if not self.enabled:
            return
        try:
            trades = self._fetch_trades()
        except Exception as exc:  # never let a read crash trading
            self.log.warning("learning: history refresh error: %s", exc)
            self._last_refresh = now  # back off; don't hammer on every cycle
            return
        self.stats = summarize_trades(trades, int(getattr(self.cfg, "learn_lookback", 200)))
        self._throttle = compute_throttle(
            self.stats,
            int(getattr(self.cfg, "learn_min_trades", 20)),
            float(getattr(self.cfg, "learn_min_scalar", 0.5)),
            int(getattr(self.cfg, "learn_pause_loss_streak", 5)),
        )
        self._last_refresh = now
        self.log.info("learning: refreshed trades=%s win_rate=%s exp/1000=%s "
                      "scalar=%.2f pause=%s (%s)",
                      self.stats["n"],
                      None if self.stats["win_rate"] is None else round(self.stats["win_rate"], 3),
                      None if self.stats["expectancy_per_1000"] is None
                      else round(self.stats["expectancy_per_1000"], 2),
                      self._throttle["scalar"], self._throttle["pause"],
                      self._throttle["notes"])

    def maybe_refresh(self, now: float) -> None:
        if self.enabled and (now - self._last_refresh) >= getattr(self.cfg, "learn_refresh_sec", 300.0):
            self.refresh(now)

    # -- signal accessors -------------------------------------------------- #

    @property
    def scalar(self) -> float:
        return self._throttle["scalar"] if self.enabled else 1.0

    @property
    def paused(self) -> bool:
        return self._throttle["pause"] if self.enabled else False

    @property
    def notes(self) -> str:
        return self._throttle["notes"]

    def avoid_entry(self, entry_price) -> Tuple[bool, str]:
        if not self.enabled or entry_price is None:
            return False, ""
        return should_avoid_entry(
            self.stats, int(entry_price),
            int(getattr(self.cfg, "learn_bucket_min_trades", 8)))

    def size(self, rule_qty: int) -> int:
        """Apply the size multiplier (only shrinks). May return 0 -> skip."""
        if not self.enabled or rule_qty <= 0:
            return rule_qty
        return int(rule_qty * self.scalar)

    def snapshot_block(self) -> Optional[dict]:
        if not self.enabled:
            return None
        return snapshot_block(self.stats)
