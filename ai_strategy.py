#!/usr/bin/env python3
"""
ai_strategy.py
==============

Optional AI decision layer for the Kalshi hourly BTC bot.

Design principle: **the AI proposes, deterministic code disposes.** This module
never places orders and never loosens a risk limit. It looks at a market
snapshot and returns a *proposed* decision; the bot's existing deterministic
guardrails (entry band, $/entry cap, stop-loss floor, DEMO/DRY_RUN gate) then
validate it. The AI can only make the bot MORE conservative:

  * veto a marginal entry the rules would otherwise take,
  * (in ``decider`` authority) size an entry DOWN, never up,
  * trigger an EARLIER exit than the deterministic stop/take-profit.

The deterministic stop-loss and take-profit always fire regardless of the AI.

Everything degrades gracefully: no ANTHROPIC_API_KEY, the ``anthropic`` package
missing, an API error, or a malformed response all resolve to "no AI opinion",
and the bot falls back to its rule-based strategy. Uses Claude Opus 4.8 with
adaptive thinking and a validated structured-output schema.

Env vars (documented in the README):
  KALSHI_AI_ENABLED        turn the layer on (default false)
  KALSHI_AI_AUTHORITY      "advisory" (veto only) | "decider" (veto + size down)
  KALSHI_AI_MODEL          model id (default claude-opus-4-8)
  KALSHI_AI_MIN_INTERVAL_SEC   min seconds between AI exit checks (default 30)
  ANTHROPIC_API_KEY        Anthropic API key
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

try:
    import anthropic
    from pydantic import BaseModel, Field
    _HAVE_AI = True
except Exception:  # anthropic/pydantic not installed -> layer stays disabled
    _HAVE_AI = False


if _HAVE_AI:
    class AIDecision(BaseModel):
        """Validated structured output Claude must return."""
        action: str = Field(description="one of: enter, hold, exit, scale_out")
        confidence: float = Field(description="0.0-1.0 confidence in the action")
        max_contracts: int = Field(
            description="the largest position you'd allow here; the bot caps this "
                        "to its own $/entry limit and never exceeds it")
        reason: str = Field(description="one concise sentence explaining the call")


SYSTEM_PROMPT = """\
You are the risk-aware decision layer of an automated trading bot on Kalshi's
hourly BTC above/below markets (binary contracts settling 0 or 100 cents).

The bot's baseline rule: buy YES when the YES ask is 85-90 cents (a strike that
is likely but not certain to be in the money), sizing ~$1000 per entry; sell on
a stop-loss (bid <= entry-1) or take-profit (99). Kalshi charges a taker fee of
ceil(0.07 * contracts * P * (1-P)) cents, which is a real drag near 50c and
smaller near the extremes.

You do NOT predict Bitcoin's price. Your edge is discipline: judge whether THIS
specific entry is worth the fee and risk given the spread, time left in the
hour, book, and current position; and time exits well. Be skeptical of thin
spreads, illiquid books, and entries with little time to settle.

You cannot loosen any risk limit. Returning a large max_contracts does not force
a large position; the bot clamps it. Prefer "hold" when the setup is marginal.

Return ONLY the structured decision:
- action: "enter" to open/keep sizing, "hold" to do nothing, "exit" to close
  now, "scale_out" to trim.
- confidence: 0..1.
- max_contracts: your ceiling (bot caps to its $/entry limit).
- reason: one concise sentence.
"""


class AIStrategy:
    """Wraps a single structured Claude call into a trade decision."""

    def __init__(self, cfg, logger: logging.Logger, client=None):
        self.cfg = cfg
        self.log = logger
        self.authority = getattr(cfg, "ai_authority", "advisory")
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        self.enabled = bool(_HAVE_AI and getattr(cfg, "ai_enabled", False) and has_key)
        self._client = client
        if getattr(cfg, "ai_enabled", False) and not self.enabled:
            why = ("anthropic/pydantic not installed" if not _HAVE_AI
                   else "ANTHROPIC_API_KEY not set")
            logger.warning("AI layer requested but disabled: %s", why)
        elif self.enabled and self._client is None:
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
            logger.info("AI decision layer enabled (model=%s, authority=%s)",
                        getattr(cfg, "ai_model", "claude-opus-4-8"), self.authority)

    def decide(self, snapshot: dict) -> Optional["AIDecision"]:
        """Return the AI's proposed decision, or None on any failure."""
        if not self.enabled or self._client is None:
            return None
        try:
            resp = self._client.messages.parse(
                model=getattr(self.cfg, "ai_model", "claude-opus-4-8"),
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(snapshot)}],
                output_format=AIDecision,
            )
            return resp.parsed_output
        except Exception as exc:  # never let an AI hiccup interrupt trading
            self.log.warning("AI decide failed; falling back to rules: %s", exc)
            return None


# --------------------------------------------------------------------------- #
# Pure guardrail functions (unit-testable without the API)
# --------------------------------------------------------------------------- #


def gate_entry(ai_decision, rule_qty: int, authority: str = "advisory"):
    """Combine the deterministic entry (rule_qty>0 means rules want in) with the
    AI opinion. Returns (proceed: bool, qty: int, reason: str).

    The AI can only VETO or (in 'decider' mode) SIZE DOWN -- never enlarge or
    create an entry outside the rules' envelope.
    """
    if ai_decision is None:
        return rule_qty > 0, rule_qty, "ai:unavailable->rules"
    action = getattr(ai_decision, "action", "hold")
    if action != "enter":
        return False, 0, f"ai:veto({action}) {getattr(ai_decision, 'reason', '')}"
    qty = rule_qty
    if authority == "decider":
        cap = int(getattr(ai_decision, "max_contracts", 0) or 0)
        if cap > 0:
            qty = min(rule_qty, cap)  # size down only
    return qty > 0, qty, f"ai:enter {getattr(ai_decision, 'reason', '')}"


def ai_early_exit(ai_decision) -> Optional[str]:
    """Return 'exit' / 'scale_out' if the AI wants to close early, else None.

    This is purely risk-reducing -- the deterministic stop/take-profit still
    fire independently; the AI can only bring an exit *forward*.
    """
    if ai_decision is None:
        return None
    action = getattr(ai_decision, "action", None)
    return action if action in ("exit", "scale_out") else None
