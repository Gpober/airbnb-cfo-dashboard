#!/usr/bin/env python3
"""
kalshi_btc_hourly_bot.py
========================

A small, self-contained trading bot for Kalshi's hourly BTC "above/below"
contracts (series ``KXBTCD``).

Baseline strategy
-----------------
* Resolve the *live* market ticker from ``GET /markets`` (do not trust a
  hard-coded ticker; series and event tickers rotate every hour).
* Enter: buy YES when the YES ask is 85-90c, sizing each entry to ~$1000.
* Stop-loss: sell when the YES bid drops to ``entry - STOP_LOSS_CENTS``.
* Take-profit: sell at ``TAKE_PROFIT_CENTS`` (99c).
* Optional scale-out: sell a fraction of the position at an intermediate price.

Extensions (built on top of the baseline, see the CLI ``--help``)
-----------------------------------------------------------------
1. ``--paper``  : paper-trade backtest harness. Uses the *live* demo order
                  book but simulates fills locally, logs every cycle to SQLite,
                  and prints a summary (trade count, win rate, avg P&L/trade,
                  total fees, net expectancy per $1000 deployed).
2. ``--manage`` : management loop driven by the Kalshi WebSocket
                  (``ticker`` + ``orderbook_delta`` channels) with an automatic
                  REST fallback when the socket drops.
3. Robustness   : real position is reconciled from ``GET /portfolio/fills``
                  (never assumed from local intent), all REST calls carry
                  429 backoff with jitter, and everything is written to a
                  structured log file.

Safety
------
The bot ships in DEMO + DRY_RUN. A *real* order is only ever placed when BOTH
``KALSHI_DEMO`` and ``KALSHI_DRY_RUN`` are explicitly false. See the README
"Go-live checklist" before flipping either.

Auth
----
RSA-PSS (SHA-256, salt length = digest length = 32 bytes) over the string
``timestamp_ms + METHOD + path``. Headers: ``KALSHI-ACCESS-KEY``,
``KALSHI-ACCESS-TIMESTAMP``, ``KALSHI-ACCESS-SIGNATURE``.

Environment variables are documented in the README and in ``Config`` below.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import logging.handlers
import math
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

try:  # cryptography is required for real auth; keep import errors friendly.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: F401
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - only hit if cryptography is absent
    _HAVE_CRYPTO = False


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Kalshi endpoints. Production is api.elections.kalshi.com (the "elections"
# subdomain serves ALL markets, not just elections); demo is demo-api.kalshi.co.
# Both can be overridden via KALSHI_API_BASE / KALSHI_WS_BASE if Kalshi moves
# hosts (e.g. external-api.kalshi.com for public market data).
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass
class Config:
    """All tunables come from the environment so nothing is hard-coded."""

    # --- Safety switches (defaults are the *safe* values) ------------------ #
    demo: bool = field(default_factory=lambda: _env_bool("KALSHI_DEMO", True))
    dry_run: bool = field(default_factory=lambda: _env_bool("KALSHI_DRY_RUN", True))

    # --- Credentials ------------------------------------------------------- #
    api_key_id: Optional[str] = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID"))
    private_key_path: Optional[str] = field(
        default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH")
    )
    private_key_pem: Optional[str] = field(
        default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PEM")
    )
    # base64 of the PEM file -- a single line with no special characters, so a
    # hosting UI cannot mangle it. Preferred on Railway/Render/Fly.
    private_key_b64: Optional[str] = field(
        default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_B64")
    )

    # --- Market selection -------------------------------------------------- #
    series_ticker: str = field(
        default_factory=lambda: os.getenv("KALSHI_SERIES_TICKER", "KXBTCD")
    )

    # --- Strategy knobs (cents) ------------------------------------------- #
    entry_min_cents: int = field(default_factory=lambda: _env_int("KALSHI_ENTRY_MIN_CENTS", 85))
    entry_max_cents: int = field(default_factory=lambda: _env_int("KALSHI_ENTRY_MAX_CENTS", 90))
    target_notional_usd: float = field(
        default_factory=lambda: _env_float("KALSHI_TARGET_NOTIONAL_USD", 1000.0)
    )
    stop_loss_cents: int = field(default_factory=lambda: _env_int("KALSHI_STOP_LOSS_CENTS", 1))
    take_profit_cents: int = field(
        default_factory=lambda: _env_int("KALSHI_TAKE_PROFIT_CENTS", 99)
    )
    # Optional scale-out: sell SCALE_OUT_FRACTION of the position the first time
    # the bid reaches SCALE_OUT_CENTS. 0 disables it.
    scale_out_cents: int = field(default_factory=lambda: _env_int("KALSHI_SCALE_OUT_CENTS", 0))
    scale_out_fraction: float = field(
        default_factory=lambda: _env_float("KALSHI_SCALE_OUT_FRACTION", 0.5)
    )

    # --- Fees -------------------------------------------------------------- #
    # See kalshi_fee_cents() for the formula and citation.
    fee_multiplier: float = field(
        default_factory=lambda: _env_float("KALSHI_FEE_MULTIPLIER", 0.07)
    )
    fee_maker_fraction: float = field(
        default_factory=lambda: _env_float("KALSHI_FEE_MAKER_FRACTION", 0.25)
    )
    assume_maker: bool = field(default_factory=lambda: _env_bool("KALSHI_ASSUME_MAKER", False))

    # --- Runtime ----------------------------------------------------------- #
    poll_interval_sec: float = field(
        default_factory=lambda: _env_float("KALSHI_POLL_INTERVAL_SEC", 5.0)
    )
    max_retries: int = field(default_factory=lambda: _env_int("KALSHI_MAX_RETRIES", 6))
    db_path: str = field(default_factory=lambda: os.getenv("KALSHI_DB_PATH", "kalshi_bot.db"))
    log_path: str = field(default_factory=lambda: os.getenv("KALSHI_LOG_PATH", "kalshi_bot.log"))
    request_timeout_sec: float = field(
        default_factory=lambda: _env_float("KALSHI_REQUEST_TIMEOUT_SEC", 15.0)
    )
    # How often the always-on manager re-checks for the active hourly ticker
    # (markets roll every hour, so a 24/7 bot must follow the rollover).
    rollover_check_sec: float = field(
        default_factory=lambda: _env_float("KALSHI_ROLLOVER_CHECK_SEC", 60.0)
    )

    # --- Supabase sink (optional) ----------------------------------------- #
    # If both are set, runs/trades/fills/orders are mirrored to Supabase.
    # Use the SERVICE-ROLE key (server-side only) so writes bypass RLS.
    supabase_url: Optional[str] = field(
        default_factory=lambda: os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    )
    supabase_service_key: Optional[str] = field(
        default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

    @property
    def base_url(self) -> str:
        override = os.getenv("KALSHI_API_BASE")
        if override:
            return override.rstrip("/")
        return DEMO_BASE if self.demo else PROD_BASE

    @property
    def ws_url(self) -> str:
        override = os.getenv("KALSHI_WS_BASE")
        if override:
            return override.rstrip("/")
        return DEMO_WS if self.demo else PROD_WS

    @property
    def live_orders_enabled(self) -> bool:
        """Real money is only ever touched when BOTH switches are off."""
        return (not self.demo) and (not self.dry_run)


# --------------------------------------------------------------------------- #
# Logging (structured, to file + console)
# --------------------------------------------------------------------------- #


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to grep, tail, or ship to a collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any structured extras passed via `logger.info(..., extra={"kv": {...}})`.
        kv = getattr(record, "kv", None)
        if isinstance(kv, dict):
            payload.update(kv)
        return json.dumps(payload, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable console line that also appends the structured kv fields.

    Without this, `log_kv(..., ticker=..., yes_bid=...)` would print only the
    bare message on the console (and hosts like Railway only show the console),
    hiding the very fields we log for diagnosis. Append them as k=v pairs.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        kv = getattr(record, "kv", None)
        if isinstance(kv, dict) and kv:
            base += " " + " ".join(f"{k}={v}" for k, v in kv.items())
        return base


def setup_logging(cfg: Config, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("kalshi_btc")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    # Rotating structured file log.
    fh = logging.handlers.RotatingFileHandler(
        cfg.log_path, maxBytes=5_000_000, backupCount=5
    )
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    # Human-readable console log (includes kv fields so hosted logs show them).
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(_ConsoleFormatter("%(asctime)s %(levelname)-5s %(message)s"))
    logger.addHandler(ch)
    logger.propagate = False
    return logger


def log_kv(logger: logging.Logger, level: int, msg: str, **kv) -> None:
    """Helper so callers can emit structured key/values in one line."""
    logger.log(level, msg, extra={"kv": kv})


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #


def kalshi_fee_cents(contracts: int, price_cents: int, cfg: Config) -> int:
    """Return the Kalshi trading fee, in **whole cents**, for a fill.

    Kalshi fee formula (taker), per the fee schedule and help center:

        fee = round_up_to_next_cent( multiplier * C * P * (1 - P) )

    where ``C`` = number of contracts and ``P`` = fill price in *dollars*
    (0.01 .. 0.99). The standard multiplier is 0.07; Kalshi rounds the result
    UP to the next cent. Maker orders are charged a fraction (currently 25%)
    of the taker fee.

    Worked example (from the schedule): at P = $0.50 the per-contract fee is
    0.07 * 0.50 * 0.50 = $0.0175, the maximum, which rounds up to $0.02.

    Sources (verified 2026-07):
      * Kalshi Fee Schedule (July 2026): https://kalshi.com/docs/kalshi-fee-schedule.pdf
      * Kalshi Help Center - Fees:        https://help.kalshi.com/en/articles/13823805-fees

    NOTE: some market *categories* carry a multiplier higher than 0.07. The
    BTC hourly series can differ from the base rate, so ``multiplier`` is
    exposed via ``KALSHI_FEE_MULTIPLIER`` -- confirm the current value for
    KXBTCD against the schedule above before trading live.
    """
    if contracts <= 0:
        return 0
    p = price_cents / 100.0
    mult = cfg.fee_multiplier
    if cfg.assume_maker:
        mult *= cfg.fee_maker_fraction
    raw_cents = mult * contracts * p * (1.0 - p) * 100.0
    # Round UP to the next whole cent, but subtract a tiny epsilon first so that
    # values that are mathematically exact (e.g. 1.75 -> 175c) are not pushed to
    # the next cent by binary floating-point error (0.07*0.5*0.5 == 0.0175000..2).
    return int(math.ceil(raw_cents - 1e-9))


def contracts_for_notional(price_cents: int, notional_usd: float) -> int:
    """How many contracts approximate ``notional_usd`` at ``price_cents``."""
    if price_cents <= 0:
        return 0
    cost_per_contract = price_cents / 100.0
    return int(notional_usd // cost_per_contract)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class KalshiSigner:
    """Loads the RSA private key and signs requests with RSA-PSS/SHA-256."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._key = None
        self.load_error: Optional[str] = None
        if cfg.api_key_id and (cfg.private_key_path or cfg.private_key_pem
                               or cfg.private_key_b64):
            # A bad/mangled key must not crash the whole worker at startup --
            # record the error, stay "not ready", and let the caller log it.
            try:
                self._key = self._load_key()
            except Exception as exc:
                self.load_error = str(exc)

    def _load_key(self):
        if not _HAVE_CRYPTO:
            raise RuntimeError("cryptography is required for signed requests")
        if self.cfg.private_key_b64:
            # base64 of the PEM file -- decode and parse. Unmanglable on hosts.
            pem = base64.b64decode(self.cfg.private_key_b64)
        elif self.cfg.private_key_pem:
            pem = self._normalize_pem(self.cfg.private_key_pem).encode()
        else:
            with open(self.cfg.private_key_path, "rb") as fh:  # type: ignore[arg-type]
                pem = fh.read()
        return serialization.load_pem_private_key(pem, password=None)

    @staticmethod
    def _normalize_pem(s: str) -> str:
        """Repair a PEM that a hosting UI mangled.

        Env-var editors frequently strip real newlines, replace them with
        spaces, store them as the literal two characters ``\\n``, or jam the
        BEGIN/END markers onto the key body. A PEM without proper line breaks is
        invalid, so we rebuild it from the base64 body before parsing.
        """
        import re
        s = s.strip().strip('"').strip("'")
        if "\\n" in s:                       # literal backslash-n -> newline
            s = s.replace("\\n", "\n")
        # If the markers are present but the body is not correctly wrapped
        # (no interior newlines, or the header/footer are jammed on), rebuild.
        m = re.search(r"-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----", s, re.DOTALL)
        if m:
            label = m.group(1).strip()
            body = "".join(m.group(2).split())  # strip ALL whitespace from body
            wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
            s = f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"
        return s

    @property
    def ready(self) -> bool:
        return self._key is not None and bool(self.cfg.api_key_id)

    def sign(self, method: str, path: str) -> dict:
        """Return the three auth headers for ``method`` + ``path``.

        The signed message is ``timestamp_ms + METHOD + path`` where ``path``
        is the request path *including* the ``/trade-api/v2`` prefix and any
        query string, exactly as sent.
        """
        if not self.ready:
            raise RuntimeError("signer not configured (missing key id or private key)")
        ts = str(int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self._key.sign(  # type: ignore[union-attr]
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=32,  # salt length == digest length == 32 bytes
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,  # type: ignore[dict-item]
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


# --------------------------------------------------------------------------- #
# Order book helper
# --------------------------------------------------------------------------- #


@dataclass
class TopOfBook:
    """Top-of-book for a Kalshi binary market, expressed from the YES side.

    Kalshi order books are two-sided (YES bids and NO bids). To BUY YES you
    lift the best NO bid, so ``yes_ask = 100 - best_no_bid``. To SELL YES you
    hit the best YES bid.
    """

    yes_bid: Optional[int] = None  # highest price someone will pay for YES
    yes_ask: Optional[int] = None  # lowest price at which you can buy YES
    ts: float = 0.0

    @property
    def valid(self) -> bool:
        return self.yes_bid is not None and self.yes_ask is not None


def top_from_orderbook(ob: dict) -> TopOfBook:
    """Convert a REST/WS ``orderbook`` payload into a :class:`TopOfBook`."""
    yes_levels = ob.get("yes") or []
    no_levels = ob.get("no") or []
    yes_bid = max((int(l[0]) for l in yes_levels), default=None)
    best_no = max((int(l[0]) for l in no_levels), default=None)
    yes_ask = (100 - best_no) if best_no is not None else None
    return TopOfBook(yes_bid=yes_bid, yes_ask=yes_ask, ts=time.time())


# --------------------------------------------------------------------------- #
# REST client (429 backoff + jitter, structured logging)
# --------------------------------------------------------------------------- #


class KalshiClient:
    def __init__(self, cfg: Config, signer: KalshiSigner, logger: logging.Logger):
        self.cfg = cfg
        self.signer = signer
        self.log = logger
        self.session = requests.Session()

    # -- low level -------------------------------------------------------- #

    def _request(self, method: str, path: str, *, signed: bool, body: Optional[dict] = None):
        """Perform one REST call with retry/backoff.

        429 (rate limit) and 5xx are retried with exponential backoff + full
        jitter, honouring ``Retry-After`` when present. 4xx (other than 429)
        fail fast -- they will not fix themselves on retry.
        """
        url = f"{self.cfg.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if signed:
                # Kalshi signs the path WITHOUT the query string: strip everything
                # from the first '?'. Signing the query string yields a bad
                # signature and a 401 on any request with query params.
                sign_path = f"/trade-api/v2{path.split('?', 1)[0]}"
                headers.update(self.signer.sign(method, sign_path))
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=body,
                    timeout=self.cfg.request_timeout_sec,
                )
            except requests.RequestException as exc:
                if attempt > self.cfg.max_retries:
                    raise
                delay = self._backoff(attempt)
                log_kv(self.log, logging.WARNING, "request error, retrying",
                       path=path, attempt=attempt, delay=round(delay, 2), error=str(exc))
                time.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.cfg.max_retries:
                    resp.raise_for_status()
                delay = self._retry_after(resp) or self._backoff(attempt)
                log_kv(self.log, logging.WARNING, "throttled/5xx, backing off",
                       path=path, status=resp.status_code, attempt=attempt,
                       delay=round(delay, 2))
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                log_kv(self.log, logging.ERROR, "request failed",
                       path=path, status=resp.status_code, body=resp.text[:500])
                resp.raise_for_status()

            if not resp.content:
                return {}
            return resp.json()

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 30s."""
        base = min(30.0, 0.5 * (2 ** (attempt - 1)))
        return random.uniform(0.0, base)

    @staticmethod
    def _retry_after(resp: requests.Response) -> Optional[float]:
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        try:
            return float(ra)
        except ValueError:
            return None

    # -- public endpoints ------------------------------------------------- #

    def get_markets(self, **params) -> dict:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        path = "/markets" + (f"?{query}" if query else "")
        # Sign when we have a key: Kalshi populates live quotes/order books only
        # for authenticated requests. Falls back to unsigned when no key is set.
        return self._request("GET", path, signed=self.signer.ready)

    def get_all_markets(self, max_pages: int = 10, **params) -> list:
        """Fetch markets across pages (KXBTCD is a large strike ladder).

        Follows the cursor so we see every strike, not just the first page --
        otherwise market selection can miss the liquid, in-band contract.
        """
        out: list = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page = dict(params)
            page["limit"] = 1000
            if cursor:
                page["cursor"] = cursor
            data = self.get_markets(**page)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_orderbook(self, ticker: str, depth: int = 1) -> dict:
        path = f"/markets/{ticker}/orderbook?depth={depth}"
        data = self._request("GET", path, signed=self.signer.ready)
        return data.get("orderbook", {})

    def get_market(self, ticker: str) -> dict:
        data = self._request("GET", f"/markets/{ticker}", signed=self.signer.ready)
        return data.get("market", {})

    def get_top(self, ticker: str) -> TopOfBook:
        """Top-of-book for ``ticker``.

        Prefer the market object's own ``yes_bid``/``yes_ask`` quote (public and
        reliably populated). The dedicated order-book endpoint can require auth
        and come back empty, so it's only a fallback.
        """
        try:
            m = self.get_market(ticker)
            yb = int(m.get("yes_bid") or 0)
            ya = int(m.get("yes_ask") or 0)
            top = TopOfBook(yes_bid=yb or None, yes_ask=ya or None, ts=time.time())
            if top.valid:
                return top
        except Exception:
            pass  # fall through to the order-book endpoint
        return top_from_orderbook(self.get_orderbook(ticker, depth=1))

    # -- authenticated endpoints ----------------------------------------- #

    def get_fills(self, ticker: Optional[str] = None, limit: int = 200,
                  max_pages: int = 20) -> list:
        """Return all fills (optionally for one ticker), following the cursor.

        Reconciliation must not miss fills, so we page through the whole result
        set rather than trusting a single response.
        """
        fills: list = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params = {"limit": limit}
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            query = "&".join(f"{k}={v}" for k, v in params.items())
            data = self._request("GET", f"/portfolio/fills?{query}", signed=True)
            fills.extend(data.get("fills", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return fills

    def get_positions(self, ticker: Optional[str] = None) -> list:
        path = "/portfolio/positions"
        if ticker:
            path += f"?ticker={ticker}"
        data = self._request("GET", path, signed=True)
        return data.get("market_positions", [])

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    price_cents: int, order_type: str = "limit",
                    client_order_id: Optional[str] = None) -> dict:
        """Place an order. NO-OP unless live orders are explicitly enabled.

        ``side`` is 'yes'/'no', ``action`` is 'buy'/'sell'. The returned dict
        always carries ``client_order_id`` so callers can correlate the order
        with its fills.
        """
        coid = client_order_id or f"kbtc-{int(time.time()*1000)}-{random.randint(0, 9999)}"
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            # client_order_id makes retries idempotent on Kalshi's side.
            "client_order_id": coid,
        }
        if order_type == "limit":
            # Kalshi expects the limit price on the side being traded.
            body["yes_price" if side == "yes" else "no_price"] = price_cents

        if not self.cfg.live_orders_enabled:
            log_kv(self.log, logging.INFO, "DRY-RUN order (not sent)",
                   ticker=ticker, action=action, side=side, count=count,
                   price_cents=price_cents, demo=self.cfg.demo, dry_run=self.cfg.dry_run)
            return {"dry_run": True, "order": body}

        log_kv(self.log, logging.WARNING, "PLACING LIVE ORDER",
               ticker=ticker, action=action, side=side, count=count, price_cents=price_cents)
        return self._request("POST", "/portfolio/orders", signed=True, body={"order": body})


# --------------------------------------------------------------------------- #
# Market / ticker resolution
# --------------------------------------------------------------------------- #


def resolve_active_ticker(client: KalshiClient, series_ticker: str,
                          logger: logging.Logger,
                          cfg: Optional[Config] = None) -> Optional[str]:
    """Select the tradeable market in ``series_ticker`` for this hour.

    KXBTCD is a *strike ladder*: each hour has many strike markets, and most
    are illiquid with empty books. Picking the first by close-time (the old
    behaviour) lands on a dead strike. Instead, within the soonest-closing
    hour, use the quotes Kalshi returns in ``GET /markets`` to pick a market
    that is actually trading -- preferring one whose YES ask is inside the
    entry band, else the most liquid quoted strike. Returns None on error so
    callers degrade gracefully.
    """
    try:
        markets = client.get_all_markets(series_ticker=series_ticker, status="open")
    except Exception as exc:
        log_kv(logger, logging.ERROR, "market lookup failed",
               series=series_ticker, error=str(exc))
        return None
    if not markets:
        log_kv(logger, logging.WARNING, "no open markets", series=series_ticker)
        return None

    # Restrict to the currently-active hour (soonest close time).
    soonest = min((m.get("close_time", "") for m in markets if m.get("close_time")),
                  default="")
    hour = [m for m in markets if m.get("close_time", "") == soonest] or markets

    # Markets with a real two-sided quote (yes_bid and yes_ask are cents; 0/None
    # means no quote). These are the only ones worth watching or trading.
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _band(m):
        return cfg is not None and cfg.entry_min_cents <= _int(m.get("yes_ask")) <= cfg.entry_max_cents

    quoted = [m for m in hour if _int(m.get("yes_bid")) and _int(m.get("yes_ask"))]

    # The /markets LIST often omits live quotes even when authenticated. If so,
    # probe the most active strikes directly -- the single-market endpoint
    # returns the live quote -- and stop as soon as we find an in-band one.
    if not quoted:
        cands = sorted(hour, key=lambda m: _int(m.get("open_interest")) + _int(m.get("volume")),
                       reverse=True)[:15]
        for m in cands:
            try:
                mk = client.get_market(m["ticker"])
            except Exception:
                continue
            yb, ya = _int(mk.get("yes_bid")), _int(mk.get("yes_ask"))
            if yb and ya:
                m = {**m, "yes_bid": yb, "yes_ask": ya}
                quoted.append(m)
                if _band(m):
                    break

    in_band = [m for m in quoted if _band(m)]
    pool = in_band or quoted
    if not pool:
        # Diagnostic: show what a top market actually looks like so we can see
        # which fields Kalshi populates for this series.
        s = max(hour, key=lambda m: _int(m.get("volume")))
        log_kv(logger, logging.WARNING, "no quoted market; sample",
               ticker=s.get("ticker"), yes_bid=s.get("yes_bid"), yes_ask=s.get("yes_ask"),
               last_price=s.get("last_price"), volume=s.get("volume"),
               open_interest=s.get("open_interest"), status=s.get("status"),
               hour_markets=len(hour))
        pool = hour  # nothing quoted -- fall back so we still watch a real market

    pick = max(pool, key=lambda m: _int(m.get("volume")))
    log_kv(logger, logging.INFO, "selected market", ticker=pick["ticker"],
           yes_bid=pick.get("yes_bid"), yes_ask=pick.get("yes_ask"),
           in_band=bool(in_band), quoted=len(quoted), open_markets=len(markets))
    return pick["ticker"]


# --------------------------------------------------------------------------- #
# Position & strategy
# --------------------------------------------------------------------------- #


@dataclass
class Position:
    ticker: str
    contracts: int
    entry_price_cents: int
    entry_fee_cents: int
    opened_ts: float
    remaining: int
    scaled_out: bool = False


def decide_entry(top: TopOfBook, cfg: Config) -> bool:
    """True if the current YES ask is inside the entry band."""
    return bool(top.valid and cfg.entry_min_cents <= top.yes_ask <= cfg.entry_max_cents)


def decide_exit(pos: Position, top: TopOfBook, cfg: Config) -> Optional[tuple]:
    """Return ``(reason, sell_price_cents, contracts)`` or None.

    Priority: take-profit, then stop-loss, then optional scale-out.
    """
    if not top.valid:
        return None
    bid = top.yes_bid
    # Take-profit: bid has reached the target -> exit everything.
    if bid >= cfg.take_profit_cents:
        return ("take_profit", min(bid, cfg.take_profit_cents), pos.remaining)
    # Stop-loss: bid fell to entry - stop -> exit everything.
    if bid <= pos.entry_price_cents - cfg.stop_loss_cents:
        return ("stop_loss", bid, pos.remaining)
    # Optional scale-out: trim a fraction once, at an intermediate price.
    if (cfg.scale_out_cents > 0 and not pos.scaled_out
            and bid >= cfg.scale_out_cents and pos.remaining > 1):
        qty = max(1, int(pos.remaining * cfg.scale_out_fraction))
        return ("scale_out", bid, qty)
    return None


# --------------------------------------------------------------------------- #
# SQLite trade log
# --------------------------------------------------------------------------- #


SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    reason          TEXT NOT NULL,     -- take_profit | stop_loss | scale_out
    contracts       INTEGER NOT NULL,
    entry_price     INTEGER NOT NULL,  -- cents
    exit_price      INTEGER NOT NULL,  -- cents
    entry_fee       INTEGER NOT NULL,  -- cents (allocated to this parcel)
    exit_fee        INTEGER NOT NULL,  -- cents
    gross_pnl       INTEGER NOT NULL,  -- cents, before fees
    net_pnl         INTEGER NOT NULL,  -- cents, after fees
    notional        INTEGER NOT NULL,  -- cents deployed at entry for this parcel
    hold_secs       REAL NOT NULL,
    opened_ts       REAL NOT NULL,
    closed_ts       REAL NOT NULL
);
"""


class TradeLog:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def record(self, run_id: str, ticker: str, reason: str, contracts: int,
               entry_price: int, exit_price: int, entry_fee: int, exit_fee: int,
               opened_ts: float, closed_ts: float) -> None:
        gross = (exit_price - entry_price) * contracts
        net = gross - entry_fee - exit_fee
        notional = entry_price * contracts
        self.conn.execute(
            """INSERT INTO paper_trades
               (run_id, ticker, reason, contracts, entry_price, exit_price,
                entry_fee, exit_fee, gross_pnl, net_pnl, notional, hold_secs,
                opened_ts, closed_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, ticker, reason, contracts, entry_price, exit_price,
             entry_fee, exit_fee, gross, net, notional, closed_ts - opened_ts,
             opened_ts, closed_ts),
        )
        self.conn.commit()

    def summary(self, run_id: Optional[str] = None) -> dict:
        cur = self.conn.cursor()
        where = "WHERE run_id = ?" if run_id else ""
        args = (run_id,) if run_id else ()
        rows = cur.execute(
            f"SELECT net_pnl, gross_pnl, entry_fee, exit_fee, notional, hold_secs "
            f"FROM paper_trades {where}", args
        ).fetchall()
        n = len(rows)
        if n == 0:
            return {"trades": 0}
        net = sum(r[0] for r in rows)
        fees = sum(r[2] + r[3] for r in rows)
        wins = sum(1 for r in rows if r[0] > 0)
        notional = sum(r[4] for r in rows)
        avg_hold = sum(r[5] for r in rows) / n
        # Net expectancy per $1000 deployed = (net P&L / notional deployed) * $1000.
        exp_per_1000 = (net / notional) * 100_000 if notional else 0.0  # in cents
        return {
            "trades": n,
            "win_rate": wins / n,
            "avg_net_pnl_cents": net / n,
            "total_fees_cents": fees,
            "total_net_pnl_cents": net,
            "total_notional_cents": notional,
            "net_expectancy_per_1000_cents": exp_per_1000,
            "avg_hold_secs": avg_hold,
        }

    def close(self):
        self.conn.close()


def print_summary(summary: dict) -> None:
    if summary.get("trades", 0) == 0:
        print("\nNo trades recorded.")
        return
    c = 100.0  # cents -> dollars divisor
    print("\n" + "=" * 52)
    print(" PAPER-TRADE SUMMARY")
    print("=" * 52)
    print(f" Trades                 : {summary['trades']}")
    print(f" Win rate               : {summary['win_rate']*100:.1f}%")
    print(f" Avg P&L / trade        : ${summary['avg_net_pnl_cents']/c:,.2f}")
    print(f" Total fees             : ${summary['total_fees_cents']/c:,.2f}")
    print(f" Total net P&L          : ${summary['total_net_pnl_cents']/c:,.2f}")
    print(f" Capital deployed       : ${summary['total_notional_cents']/c:,.2f}")
    print(f" Net expectancy /$1000  : ${summary['net_expectancy_per_1000_cents']/c:,.2f}")
    print(f" Avg hold               : {summary['avg_hold_secs']:.1f}s")
    print("=" * 52)


# --------------------------------------------------------------------------- #
# Supabase sink (optional) -- mirrors runs/trades/fills/orders to Postgres
# --------------------------------------------------------------------------- #


class SupabaseSink:
    """Best-effort writer to Supabase via PostgREST.

    Enabled only when SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set. Every
    call is wrapped so a Supabase hiccup can never crash the trading loop -- the
    local SQLite log remains the durable record; Supabase is the shared mirror
    the dashboard reads.
    """

    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self.enabled = bool(cfg.supabase_url and cfg.supabase_service_key)
        self._base = (cfg.supabase_url or "").rstrip("/") + "/rest/v1"
        self._session = requests.Session()
        if self.enabled:
            self._session.headers.update({
                "apikey": cfg.supabase_service_key,
                "Authorization": f"Bearer {cfg.supabase_service_key}",
                "Content-Type": "application/json",
            })
            log_kv(logger, logging.INFO, "supabase sink enabled", url=cfg.supabase_url)

    def _post(self, table: str, row: dict, on_conflict: Optional[str] = None) -> None:
        if not self.enabled:
            return
        params = {}
        headers = {"Prefer": "return=minimal"}
        if on_conflict:
            params["on_conflict"] = on_conflict
            headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        try:
            resp = self._session.post(
                f"{self._base}/{table}", params=params, headers=headers,
                json=row, timeout=self.cfg.request_timeout_sec,
            )
            if resp.status_code >= 300:
                log_kv(self.log, logging.WARNING, "supabase write failed",
                       table=table, status=resp.status_code, body=resp.text[:300])
        except requests.RequestException as exc:
            log_kv(self.log, logging.WARNING, "supabase unreachable", table=table, error=str(exc))

    # -- typed helpers ---------------------------------------------------- #

    def start_run(self, run_id: str, mode: str) -> None:
        self._post("kalshi_runs", {
            "run_id": run_id, "mode": mode, "series_ticker": self.cfg.series_ticker,
            "config": self._config_snapshot(),
        }, on_conflict="run_id")

    def end_run(self, run_id: str) -> None:
        # PATCH the run's ended_at; use POST upsert to keep it single-path.
        if not self.enabled:
            return
        try:
            self._session.patch(
                f"{self._base}/kalshi_runs", params={"run_id": f"eq.{run_id}"},
                headers={"Prefer": "return=minimal"},
                json={"ended_at": "now()"}, timeout=self.cfg.request_timeout_sec,
            )
        except requests.RequestException as exc:
            log_kv(self.log, logging.WARNING, "supabase end_run failed", error=str(exc))

    def record_trade(self, run_id: str, ticker: str, reason: str, contracts: int,
                     entry_price: int, exit_price: int, entry_fee: int, exit_fee: int,
                     opened_ts: float, closed_ts: float) -> None:
        gross = (exit_price - entry_price) * contracts
        self._post("kalshi_trades", {
            "run_id": run_id, "ticker": ticker, "reason": reason, "contracts": contracts,
            "entry_price": entry_price, "exit_price": exit_price,
            "entry_fee": entry_fee, "exit_fee": exit_fee,
            "gross_pnl": gross, "net_pnl": gross - entry_fee - exit_fee,
            "notional": entry_price * contracts, "hold_secs": closed_ts - opened_ts,
            "opened_at": _iso(opened_ts), "closed_at": _iso(closed_ts),
        })

    def record_order(self, run_id: str, ticker: str, side: str, action: str,
                     count: int, price: Optional[int], client_order_id: str,
                     dry_run: bool, status: str = "submitted", raw: Optional[dict] = None) -> None:
        self._post("kalshi_orders", {
            "client_order_id": client_order_id, "run_id": run_id, "ticker": ticker,
            "side": side, "action": action, "count": count, "price": price,
            "dry_run": dry_run, "status": status, "raw": raw,
        }, on_conflict="client_order_id")

    def record_position(self, run_id: str, ticker: str, net: int, avg_entry: int) -> None:
        self._post("kalshi_positions", {
            "run_id": run_id, "ticker": ticker, "net_contracts": net, "avg_entry": avg_entry,
        })

    def record_tick(self, ticker: str, top: "TopOfBook", source: str) -> None:
        self._post("kalshi_market_ticks", {
            "ticker": ticker, "yes_bid": top.yes_bid, "yes_ask": top.yes_ask, "source": source,
        })

    def _config_snapshot(self) -> dict:
        c = self.cfg
        return {
            "entry_min_cents": c.entry_min_cents, "entry_max_cents": c.entry_max_cents,
            "target_notional_usd": c.target_notional_usd, "stop_loss_cents": c.stop_loss_cents,
            "take_profit_cents": c.take_profit_cents, "scale_out_cents": c.scale_out_cents,
            "fee_multiplier": c.fee_multiplier, "demo": c.demo, "dry_run": c.dry_run,
        }


def _iso(ts: float) -> str:
    """UTC ISO-8601 for a unix timestamp (Postgres timestamptz-friendly)."""
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"


# --------------------------------------------------------------------------- #
# Paper-trade harness
# --------------------------------------------------------------------------- #


def simulate_fill_exit(pos: Position, reason: str, sell_price: int, qty: int,
                       cfg: Config, tradelog: TradeLog, run_id: str,
                       closed_ts: float, logger: logging.Logger,
                       sink: Optional["SupabaseSink"] = None) -> None:
    """Book a simulated (partial or full) exit of ``pos`` into the trade log."""
    # Allocate the entry fee to this parcel proportionally.
    parcel_entry_fee = int(round(pos.entry_fee_cents * qty / pos.contracts))
    exit_fee = kalshi_fee_cents(qty, sell_price, cfg)
    tradelog.record(
        run_id=run_id, ticker=pos.ticker, reason=reason, contracts=qty,
        entry_price=pos.entry_price_cents, exit_price=sell_price,
        entry_fee=parcel_entry_fee, exit_fee=exit_fee,
        opened_ts=pos.opened_ts, closed_ts=closed_ts,
    )
    if sink:
        sink.record_trade(run_id, pos.ticker, reason, qty, pos.entry_price_cents,
                          sell_price, parcel_entry_fee, exit_fee, pos.opened_ts, closed_ts)
    log_kv(logger, logging.INFO, "paper exit",
           reason=reason, qty=qty, entry=pos.entry_price_cents, exit=sell_price,
           exit_fee=exit_fee)


def run_paper(cfg: Config, client: KalshiClient, logger: logging.Logger,
              cycles: int, interval: float,
              book_source: Optional[Callable[[str], TopOfBook]] = None,
              ticker: Optional[str] = None,
              sink: Optional["SupabaseSink"] = None) -> dict:
    """Paper-trade against the *live* order book, simulating fills locally.

    ``book_source(ticker) -> TopOfBook`` is injectable so the logic can be
    exercised offline; by default it reads the live REST order book.
    """
    run_id = f"paper-{int(time.time())}"
    tradelog = TradeLog(cfg.db_path)
    get_top = book_source or client.get_top

    if ticker is None:
        ticker = resolve_active_ticker(client, cfg.series_ticker, logger, cfg)
        if ticker is None:
            log_kv(logger, logging.ERROR, "no active ticker; aborting paper run")
            return tradelog.summary(run_id)

    if sink:
        sink.start_run(run_id, "paper")
    pos: Optional[Position] = None
    log_kv(logger, logging.INFO, "paper run start",
           run_id=run_id, ticker=ticker, cycles=cycles, interval=interval)

    for i in range(cycles):
        try:
            top = get_top(ticker)
        except Exception as exc:  # keep the harness alive across transient errors
            log_kv(logger, logging.WARNING, "book fetch failed", cycle=i, error=str(exc))
            time.sleep(interval)
            continue

        if not top.valid:
            time.sleep(interval)
            continue

        now = time.time()
        if pos is None:
            if decide_entry(top, cfg):
                qty = contracts_for_notional(top.yes_ask, cfg.target_notional_usd)
                if qty > 0:
                    entry_fee = kalshi_fee_cents(qty, top.yes_ask, cfg)
                    pos = Position(
                        ticker=ticker, contracts=qty, entry_price_cents=top.yes_ask,
                        entry_fee_cents=entry_fee, opened_ts=now, remaining=qty,
                    )
                    log_kv(logger, logging.INFO, "paper entry",
                           ticker=ticker, qty=qty, price=top.yes_ask, entry_fee=entry_fee)
        else:
            decision = decide_exit(pos, top, cfg)
            if decision:
                reason, sell_price, qty = decision
                simulate_fill_exit(pos, reason, sell_price, qty, cfg,
                                   tradelog, run_id, now, logger, sink=sink)
                pos.remaining -= qty
                if reason == "scale_out":
                    pos.scaled_out = True
                if pos.remaining <= 0:
                    pos = None

        if i < cycles - 1:
            time.sleep(interval)

    summary = tradelog.summary(run_id)
    tradelog.close()
    if sink:
        sink.end_run(run_id)
    return summary


# --------------------------------------------------------------------------- #
# WebSocket feed (ticker + orderbook_delta) with REST fallback
# --------------------------------------------------------------------------- #


class WebSocketFeed:
    """Maintains a live :class:`TopOfBook` from the Kalshi WebSocket.

    Subscribes to the ``ticker`` and ``orderbook_delta`` channels for one
    market. The handshake carries the same signed headers as REST (the signed
    path is the WS route ``/trade-api/ws/v2``). If the socket is unavailable or
    drops, callers should fall back to REST via :meth:`get_top`'s ``stale``
    signal.
    """

    def __init__(self, cfg: Config, signer: KalshiSigner, ticker: str,
                 logger: logging.Logger):
        self.cfg = cfg
        self.signer = signer
        self.ticker = ticker
        self.log = logger
        self.top = TopOfBook()
        self._yes: dict[int, int] = {}
        self._no: dict[int, int] = {}
        self.connected = False
        self._ws = None
        self._thread = None
        self._stop = False
        self._last_msg_ts = 0.0
        # orderbook_delta carries a monotonic sequence number per market. A gap
        # means we missed a delta and the local book is unreliable until the
        # next snapshot -- we flag it and let callers fall back to REST.
        self._seq: Optional[int] = None
        self._desynced = False

    # -- message handling (pure; unit-testable without a socket) ---------- #

    def _apply_snapshot(self, msg: dict) -> None:
        ob = msg.get("msg", {})
        self._yes = {int(p): int(s) for p, s in (ob.get("yes") or [])}
        self._no = {int(p): int(s) for p, s in (ob.get("no") or [])}
        # A fresh snapshot resets the sequence and clears any prior desync.
        self._seq = msg.get("seq")
        self._desynced = False
        self._recompute_top()

    def _apply_delta(self, msg: dict) -> None:
        seq = msg.get("seq")
        if self._seq is not None and seq is not None and seq != self._seq + 1:
            self._desynced = True
            log_kv(self.log, logging.WARNING, "ws sequence gap; REST fallback",
                   expected=self._seq + 1, got=seq)
        if seq is not None:
            self._seq = seq
        d = msg.get("msg", {})
        side = d.get("side")
        price = d.get("price")
        delta = d.get("delta")
        if side is None or price is None or delta is None:
            return
        book = self._yes if side == "yes" else self._no
        new = book.get(int(price), 0) + int(delta)
        if new <= 0:
            book.pop(int(price), None)
        else:
            book[int(price)] = new
        self._recompute_top()

    def _apply_ticker(self, msg: dict) -> None:
        t = msg.get("msg", {})
        yb, ya = t.get("yes_bid"), t.get("yes_ask")
        if yb is not None:
            self.top.yes_bid = int(yb)
        if ya is not None:
            self.top.yes_ask = int(ya)
        self.top.ts = time.time()

    def _recompute_top(self) -> None:
        # The maintained order-book dicts are authoritative for top-of-book:
        # if a side empties, that side of the top is genuinely gone (None).
        self.top.yes_bid = max(self._yes) if self._yes else None
        self.top.yes_ask = (100 - max(self._no)) if self._no else None
        self.top.ts = time.time()

    def handle_message(self, raw: str) -> None:
        """Dispatch a raw WS text frame. Public so tests can drive it."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._last_msg_ts = time.time()
        mtype = msg.get("type")
        if mtype == "orderbook_snapshot":
            self._apply_snapshot(msg)
        elif mtype == "orderbook_delta":
            self._apply_delta(msg)
        elif mtype == "ticker":
            self._apply_ticker(msg)
        # other frame types (subscribed/error/ok) are ignored here

    # -- connection lifecycle -------------------------------------------- #

    def start(self) -> bool:
        """Open the socket in a background thread. Returns False if unavailable.

        WebSocket support is an optional dependency (``websocket-client``); if
        it is not installed we return False and the caller uses REST.
        """
        try:
            import threading
            import websocket  # type: ignore
        except Exception as exc:
            log_kv(self.log, logging.WARNING, "websocket unavailable; REST fallback",
                   error=str(exc))
            return False

        headers = self.signer.sign("GET", "/trade-api/ws/v2")
        header_list = [f"{k}: {v}" for k, v in headers.items()]

        def _on_open(ws):
            self.connected = True
            sub = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["ticker", "orderbook_delta"],
                    "market_tickers": [self.ticker],  # Kalshi WS v2 expects a list
                },
            }
            ws.send(json.dumps(sub))
            log_kv(self.log, logging.INFO, "ws connected", ticker=self.ticker)

        def _on_message(ws, message):
            self.handle_message(message)

        def _on_error(ws, error):
            log_kv(self.log, logging.WARNING, "ws error", error=str(error))

        def _on_close(ws, code, reason):
            self.connected = False
            log_kv(self.log, logging.WARNING, "ws closed", code=code, reason=str(reason))

        self._ws = websocket.WebSocketApp(
            self.cfg.ws_url, header=header_list,
            on_open=_on_open, on_message=_on_message,
            on_error=_on_error, on_close=_on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 10, "ping_timeout": 5},
            daemon=True,
        )
        self._thread.start()
        # Give the handshake a moment.
        deadline = time.time() + 5
        while time.time() < deadline and not self.connected:
            time.sleep(0.1)
        return self.connected

    def is_fresh(self, max_age: float = 10.0) -> bool:
        return (self.connected and self.top.valid and not self._desynced
                and (time.time() - self.top.ts) < max_age)

    def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Position reconciliation (never assume fills)
# --------------------------------------------------------------------------- #


def reconcile_position_from_fills(client: KalshiClient, ticker: str,
                                  logger: logging.Logger) -> Optional[Position]:
    """Reconstruct the real net YES position for ``ticker`` from actual fills.

    We do NOT assume our orders filled; we read ``GET /portfolio/fills`` and
    net buys against sells. Returns a :class:`Position` (with a size-weighted
    average entry) or None if flat.
    """
    fills = client.get_fills(ticker=ticker)
    net = 0
    cost = 0  # cents, signed, of open YES exposure
    fee_total = 0
    opened_ts = time.time()
    for f in fills:
        side = f.get("side", "yes")
        action = f.get("action")  # buy | sell
        count = int(f.get("count", 0))
        price = int(f.get("yes_price") if side == "yes" else 100 - int(f.get("no_price", 0)))
        fee_total += int(f.get("fee", 0) or 0)
        signed = count if action == "buy" else -count
        # Only track net YES exposure for this simple single-market strategy.
        if side == "no":
            signed = -signed  # a NO buy is economically short YES
        net += signed
        cost += signed * price

    if net == 0:
        log_kv(logger, logging.INFO, "reconciled flat", ticker=ticker, fills=len(fills))
        return None

    avg_entry = int(round(cost / net)) if net else 0
    log_kv(logger, logging.INFO, "reconciled position",
           ticker=ticker, net=net, avg_entry=avg_entry, fills=len(fills))
    return Position(
        ticker=ticker, contracts=abs(net), entry_price_cents=avg_entry,
        entry_fee_cents=fee_total, opened_ts=opened_ts, remaining=abs(net),
    )


# --------------------------------------------------------------------------- #
# Management loop (WebSocket-driven, REST fallback)
# --------------------------------------------------------------------------- #


def run_manage(cfg: Config, client: KalshiClient, signer: KalshiSigner,
               logger: logging.Logger, cycles: Optional[int] = None,
               sink: Optional["SupabaseSink"] = None, rollover: bool = False) -> None:
    """Manage a position from the WebSocket feed, falling back to REST.

    In LIVE mode the real position is the source of truth: it is reconciled
    from ``GET /portfolio/fills`` every cycle, and after any order we suppress
    further order placement for a short grace window so a resting limit is not
    duplicated before it fills.

    In DRY_RUN / demo mode no order actually reaches the exchange, so the loop
    simulates the fill locally -- this lets you watch a full entry -> exit
    lifecycle without touching the API's order endpoint.

    When ``rollover`` is true (the always-on worker), the active hourly ticker
    is re-checked every ``cfg.rollover_check_sec`` and, when the hour rolls, the
    loop reconnects the socket to the new market and resets local state.
    """
    live = cfg.live_orders_enabled
    run_id = f"manage-{int(time.time())}"
    mode = "live" if live else ("demo" if cfg.demo else "prod")
    if sink:
        sink.start_run(run_id, mode)

    state = {"feed": None}  # mutable holder so the reconnect helper can swap it

    def connect(tkr: str) -> bool:
        old = state["feed"]
        if old is not None:
            old.stop()
        f = WebSocketFeed(cfg, signer, tkr, logger)
        ok = f.start() if signer.ready else False
        if not ok:
            log_kv(logger, logging.INFO, "using REST polling", ticker=tkr)
        state["feed"] = f
        return ok

    ticker = resolve_active_ticker(client, cfg.series_ticker, logger, cfg)
    pos: Optional[Position] = None
    ws_ok = False
    if ticker:
        if signer.ready:
            try:
                pos = reconcile_position_from_fills(client, ticker, logger)
            except Exception as exc:
                log_kv(logger, logging.WARNING, "startup reconcile failed", error=str(exc))
        ws_ok = connect(ticker)
    elif not rollover:
        return  # one-shot manage with no market: nothing to do

    grace = max(2 * cfg.poll_interval_sec, 5.0)
    pending_until = 0.0        # do not place another order before this (live only)
    last_roll_check = time.time()
    i = 0
    try:
        while cycles is None or i < cycles:
            i += 1

            # --- hourly rollover: follow the active market ----------------- #
            if rollover and (time.time() - last_roll_check) >= cfg.rollover_check_sec:
                last_roll_check = time.time()
                new_ticker = resolve_active_ticker(client, cfg.series_ticker, logger, cfg)
                if new_ticker and new_ticker != ticker:
                    log_kv(logger, logging.INFO, "ticker rollover",
                           old=ticker, new=new_ticker)
                    ticker, pos, pending_until = new_ticker, None, 0.0
                    ws_ok = connect(ticker)

            if ticker is None:  # forever mode, waiting for a market to open
                time.sleep(cfg.poll_interval_sec)
                continue

            feed = state["feed"]
            # Roughly one heartbeat / stale-notice per rollover window.
            hb_every = max(1, int(cfg.rollover_check_sec / max(cfg.poll_interval_sec, 1)))

            # --- market data: prefer a fresh socket, else REST ------------- #
            if ws_ok and feed.is_fresh():
                top, source = feed.top, "ws"
            else:
                # Throttle so a persistently-unusable socket doesn't log every cycle.
                if ws_ok and not feed.is_fresh() and i % hb_every == 1:
                    log_kv(logger, logging.WARNING, "ws not fresh; using REST", ticker=ticker)
                try:
                    top = client.get_top(ticker)
                    source = "rest"
                except Exception as exc:
                    log_kv(logger, logging.WARNING, "REST top failed", error=str(exc))
                    time.sleep(cfg.poll_interval_sec)
                    continue

            # Heartbeat: log exactly what the bot sees ~once per minute so a
            # quiet loop is diagnosable (empty book vs. no signal vs. all good).
            if i % hb_every == 0:
                log_kv(logger, logging.INFO, "heartbeat", ticker=ticker,
                       yes_bid=top.yes_bid, yes_ask=top.yes_ask, valid=top.valid,
                       source=source, has_position=pos is not None)
            if not top.valid and i % hb_every == 1:
                log_kv(logger, logging.WARNING, "order book empty/one-sided; skipping",
                       ticker=ticker, yes_bid=top.yes_bid, yes_ask=top.yes_ask)

            if sink and top.valid:
                sink.record_tick(ticker, top, source)

            # --- position truth: live reads fills every cycle -------------- #
            if live and signer.ready:
                try:
                    pos = reconcile_position_from_fills(client, ticker, logger)
                except Exception as exc:
                    log_kv(logger, logging.WARNING, "reconcile failed", error=str(exc))
            if sink and pos is not None:
                sink.record_position(run_id, ticker, pos.contracts, pos.entry_price_cents)

            now = time.time()
            placed = False

            if pos is None:
                if now >= pending_until and top.valid and decide_entry(top, cfg):
                    qty = contracts_for_notional(top.yes_ask, cfg.target_notional_usd)
                    if qty > 0:
                        coid = f"kbtc-{int(now*1000)}-{random.randint(0, 9999)}"
                        log_kv(logger, logging.INFO, "entry signal",
                               price=top.yes_ask, qty=qty, source=source)
                        resp = client.place_order(ticker, "yes", "buy", qty,
                                                  top.yes_ask, client_order_id=coid)
                        placed = True
                        if sink:
                            sink.record_order(run_id, ticker, "yes", "buy", qty,
                                              top.yes_ask, coid, dry_run=not live, raw=resp)
                        if not live:
                            # Simulate the fill so the lifecycle is observable.
                            fee = kalshi_fee_cents(qty, top.yes_ask, cfg)
                            pos = Position(ticker=ticker, contracts=qty,
                                           entry_price_cents=top.yes_ask,
                                           entry_fee_cents=fee, opened_ts=now, remaining=qty)
            else:
                decision = decide_exit(pos, top, cfg)
                if decision and now >= pending_until:
                    reason, sell_price, qty = decision
                    coid = f"kbtc-{int(now*1000)}-{random.randint(0, 9999)}"
                    log_kv(logger, logging.INFO, "exit signal",
                           reason=reason, price=sell_price, qty=qty, source=source)
                    resp = client.place_order(ticker, "yes", "sell", qty,
                                              sell_price, client_order_id=coid)
                    placed = True
                    if sink:
                        sink.record_order(run_id, ticker, "yes", "sell", qty,
                                          sell_price, coid, dry_run=not live, raw=resp)
                    if not live:
                        pos.remaining -= qty
                        if reason == "scale_out":
                            pos.scaled_out = True
                        if pos.remaining <= 0:
                            pos = None

            # After a live order, hold off until the grace window passes so a
            # resting limit is not resubmitted before the next fills read.
            if placed and live:
                pending_until = now + grace

            time.sleep(cfg.poll_interval_sec)
    finally:
        if state["feed"] is not None:
            state["feed"].stop()
        if sink:
            sink.end_run(run_id)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _banner(cfg: Config, logger: logging.Logger) -> None:
    mode = "LIVE-REAL-MONEY" if cfg.live_orders_enabled else (
        "DEMO" if cfg.demo else "PROD") + ("+DRY_RUN" if cfg.dry_run else "")
    log_kv(logger, logging.INFO, "startup",
           mode=mode, demo=cfg.demo, dry_run=cfg.dry_run,
           base_url=cfg.base_url, series=cfg.series_ticker,
           live_orders_enabled=cfg.live_orders_enabled)
    if cfg.live_orders_enabled:
        log_kv(logger, logging.WARNING,
               "LIVE ORDERS ENABLED - both DEMO and DRY_RUN are false")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi hourly BTC bot (KXBTCD)")
    parser.add_argument("--paper", action="store_true",
                        help="paper-trade against the live book, simulate fills")
    parser.add_argument("--manage", action="store_true",
                        help="run the WebSocket-driven management loop once")
    parser.add_argument("--forever", action="store_true",
                        help="always-on worker: manage + follow the hourly rollover")
    parser.add_argument("--report", action="store_true",
                        help="print the SQLite paper-trade summary and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run offline self-tests (no network)")
    parser.add_argument("--cycles", type=int, default=120,
                        help="paper/manage cycles to run (default 120)")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between cycles (default: poll interval)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config()
    logger = setup_logging(cfg, verbose=args.verbose)

    if args.selftest:
        from kalshi_selftest import run_selftests  # local, offline
        return run_selftests(cfg, logger)

    _banner(cfg, logger)
    signer = KalshiSigner(cfg)
    if signer.load_error:
        log_kv(logger, logging.ERROR,
               "private key failed to load - running unauthenticated "
               "(check KALSHI_PRIVATE_KEY_PEM formatting)", error=signer.load_error)
    client = KalshiClient(cfg, signer, logger)
    sink = SupabaseSink(cfg, logger)
    interval = args.interval if args.interval is not None else cfg.poll_interval_sec

    if args.report:
        log = TradeLog(cfg.db_path)
        print_summary(log.summary())
        log.close()
        return 0

    if args.paper:
        summary = run_paper(cfg, client, logger, cycles=args.cycles,
                            interval=interval, sink=sink)
        print_summary(summary)
        return 0

    if args.forever:
        # Always-on worker: run indefinitely, following the hourly rollover.
        log_kv(logger, logging.INFO, "starting always-on worker (--forever)")
        run_manage(cfg, client, signer, logger, cycles=None, sink=sink, rollover=True)
        return 0

    if args.manage:
        run_manage(cfg, client, signer, logger, cycles=args.cycles, sink=sink)
        return 0

    # Default: resolve and print the active ticker + top of book.
    ticker = resolve_active_ticker(client, cfg.series_ticker, logger, cfg)
    if ticker:
        try:
            top = client.get_top(ticker)
            log_kv(logger, logging.INFO, "top of book",
                   ticker=ticker, yes_bid=top.yes_bid, yes_ask=top.yes_ask)
        except Exception as exc:
            log_kv(logger, logging.WARNING, "could not fetch book", error=str(exc))
    print("\nRun with --paper, --manage, --report, or --selftest. See --help.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
