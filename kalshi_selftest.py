#!/usr/bin/env python3
"""
kalshi_selftest.py
==================

Offline self-tests for kalshi_btc_hourly_bot. These exercise every code path
that does NOT require network access to Kalshi, so the bot's logic can be
validated in an environment where the demo/prod APIs are unreachable.

Run:  python kalshi_btc_hourly_bot.py --selftest
  or: python kalshi_selftest.py

Covered:
  * fee formula (values + round-up behaviour + maker discount)
  * contract sizing for a target notional
  * entry / exit decision logic (take-profit, stop-loss, scale-out)
  * order-book -> top-of-book conversion
  * paper harness end-to-end with a synthetic book (SQLite + summary)
  * WebSocket message parsing (snapshot / delta / ticker)
  * position reconciliation from mock fills
  * RSA-PSS signing (verified against an ephemeral key)
"""

from __future__ import annotations

import logging
import os
import tempfile

import kalshi_btc_hourly_bot as bot


class _Check:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def eq(self, name, got, want):
        if got == want:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}: got {got!r}, want {want!r}")

    def ok(self, name, cond):
        self.eq(name, bool(cond), True)


def _test_fees(chk, cfg):
    # Max per-contract fee is at P=0.50: 0.07*0.5*0.5 = $0.0175 -> rounds up to 2c.
    chk.eq("fee 1@50c = 2c", bot.kalshi_fee_cents(1, 50, cfg), 2)
    # 0.07 * 100 * 0.5 * 0.5 = $1.75 -> 175c exactly.
    chk.eq("fee 100@50c = 175c", bot.kalshi_fee_cents(100, 50, cfg), 175)
    # At 10c: 0.07*0.1*0.9 = 0.0063 -> 1c (rounded up).
    chk.eq("fee 1@10c = 1c (round up)", bot.kalshi_fee_cents(1, 10, cfg), 1)
    # At the strategy's 87c entry, 11 contracts: 0.07*11*0.87*0.13 = 0.0870 -> 9c.
    chk.eq("fee 11@87c = 9c", bot.kalshi_fee_cents(11, 87, cfg), 9)
    chk.eq("fee 0 contracts = 0", bot.kalshi_fee_cents(0, 50, cfg), 0)
    # Maker discount = 25% of taker.
    maker_cfg = bot.Config()
    maker_cfg.assume_maker = True
    # 0.25 * 0.07 * 100 * 0.5 * 0.5 = $0.4375 -> 44c.
    chk.eq("maker fee 100@50c = 44c", bot.kalshi_fee_cents(100, 50, maker_cfg), 44)


def _test_sizing(chk):
    # $1000 at 87c/contract -> floor(1000/0.87) = 1149 contracts.
    chk.eq("sizing $1000@87c", bot.contracts_for_notional(87, 1000.0), 1149)
    chk.eq("sizing @0c = 0", bot.contracts_for_notional(0, 1000.0), 0)


def _test_decisions(chk, cfg):
    inb = bot.TopOfBook(yes_bid=86, yes_ask=87)
    chk.ok("entry in band (87c)", bot.decide_entry(inb, cfg))
    chk.ok("no entry at 92c", not bot.decide_entry(bot.TopOfBook(84, 92), cfg))
    chk.ok("no entry at 84c", not bot.decide_entry(bot.TopOfBook(83, 84), cfg))
    chk.ok("no entry on invalid book", not bot.decide_entry(bot.TopOfBook(None, None), cfg))

    pos = bot.Position("T", contracts=100, entry_price_cents=87,
                       entry_fee_cents=8, opened_ts=0, remaining=100)
    # Take-profit at 99c.
    d = bot.decide_exit(pos, bot.TopOfBook(yes_bid=99, yes_ask=100), cfg)
    chk.eq("take-profit fires", d[0], "take_profit")
    # Stop-loss: entry 87 - 1 = 86 -> bid 86 triggers.
    d = bot.decide_exit(pos, bot.TopOfBook(yes_bid=86, yes_ask=88), cfg)
    chk.eq("stop-loss fires at entry-1", d[0], "stop_loss")
    # No exit in the middle.
    d = bot.decide_exit(pos, bot.TopOfBook(yes_bid=90, yes_ask=92), cfg)
    chk.ok("no exit mid-range", d is None)

    # Scale-out.
    so_cfg = bot.Config()
    so_cfg.scale_out_cents = 93
    so_cfg.scale_out_fraction = 0.5
    d = bot.decide_exit(pos, bot.TopOfBook(yes_bid=93, yes_ask=95), so_cfg)
    chk.eq("scale-out fires", (d[0], d[2]), ("scale_out", 50))


def _test_orderbook(chk):
    ob = {"yes": [[80, 100], [86, 50]], "no": [[8, 30], [11, 40]]}
    top = bot.top_from_orderbook(ob)
    chk.eq("yes_bid = max yes", top.yes_bid, 86)
    chk.eq("yes_ask = 100 - best_no", top.yes_ask, 89)  # 100 - 11


def _test_ws_parsing(chk, cfg):
    feed = bot.WebSocketFeed(cfg, bot.KalshiSigner(cfg), "TICK", logging.getLogger("t"))
    feed.handle_message('{"type":"orderbook_snapshot","msg":{"yes":[[86,10]],"no":[[11,5]]}}')
    chk.eq("ws snapshot yes_bid", feed.top.yes_bid, 86)
    chk.eq("ws snapshot yes_ask", feed.top.yes_ask, 89)
    # Delta adds a better NO bid at 12 -> yes_ask should drop to 88.
    feed.handle_message('{"type":"orderbook_delta","msg":{"side":"no","price":12,"delta":7}}')
    chk.eq("ws delta updates yes_ask", feed.top.yes_ask, 88)
    # Delta removes the 86 yes level -> yes_bid recomputes to next (none here).
    feed.handle_message('{"type":"orderbook_delta","msg":{"side":"yes","price":86,"delta":-10}}')
    chk.ok("ws delta removes level", feed.top.yes_bid is None)
    # Ticker frame sets top directly.
    feed.handle_message('{"type":"ticker","msg":{"yes_bid":90,"yes_ask":92}}')
    chk.eq("ws ticker yes_bid", feed.top.yes_bid, 90)
    chk.eq("ws ticker yes_ask", feed.top.yes_ask, 92)
    # Malformed frame must not raise.
    feed.handle_message("not json")
    chk.ok("ws malformed ignored", True)


def _test_ws_desync(chk, cfg):
    feed = bot.WebSocketFeed(cfg, bot.KalshiSigner(cfg), "T", logging.getLogger("t"))
    feed.connected = True  # simulate an open socket so is_fresh() is meaningful
    feed.handle_message('{"type":"orderbook_snapshot","seq":5,"msg":{"yes":[[86,10]],"no":[[11,5]]}}')
    chk.ok("not desynced after snapshot", not feed._desynced)
    chk.ok("fresh after snapshot", feed.is_fresh())
    feed.handle_message('{"type":"orderbook_delta","seq":6,"msg":{"side":"no","price":12,"delta":1}}')
    chk.ok("in-order delta stays synced", not feed._desynced)
    feed.handle_message('{"type":"orderbook_delta","seq":8,"msg":{"side":"no","price":13,"delta":1}}')
    chk.ok("sequence gap flags desync", feed._desynced)
    chk.ok("desync forces REST (not fresh)", not feed.is_fresh())
    feed.handle_message('{"type":"orderbook_snapshot","seq":9,"msg":{"yes":[[86,10]],"no":[[11,5]]}}')
    chk.ok("snapshot clears desync", not feed._desynced and feed.is_fresh())


def _test_manage_dryrun(chk):
    """run_manage in dry-run must simulate a full entry->exit and not spam orders."""
    cfg = bot.Config()  # DEMO + DRY_RUN defaults -> live orders disabled
    cfg.poll_interval_sec = 0.0
    orders = []
    books = [
        bot.TopOfBook(yes_bid=86, yes_ask=87),   # entry
        bot.TopOfBook(yes_bid=92, yes_ask=94),   # hold (no exit)
        bot.TopOfBook(yes_bid=99, yes_ask=100),  # take-profit
    ]
    state = {"i": 0}

    class _Stub:
        def get_all_markets(self, **kw):
            return [{"ticker": "KXBTCD-T", "close_time": "2026-07-10T20:00:00Z",
                     "yes_bid": 86, "yes_ask": 87, "volume": 100}]

        def get_top(self, ticker):
            b = books[min(state["i"], len(books) - 1)]
            state["i"] += 1
            return b

        def place_order(self, ticker, side, action, count, price_cents,
                        order_type="limit", client_order_id=None):
            orders.append((action, count, price_cents))
            return {"dry_run": True}

    signer = bot.KalshiSigner(cfg)  # no creds -> not ready (WS skipped, REST used)
    bot.run_manage(cfg, _Stub(), signer, logging.getLogger("t"), cycles=3)
    actions = [o[0] for o in orders]
    chk.ok("manage placed a buy", "buy" in actions)
    chk.ok("manage placed a sell", "sell" in actions)
    chk.eq("manage did not spam (exactly 2 orders)", len(orders), 2)


def _test_reconcile(chk, cfg):
    class _StubClient:
        def get_fills(self, ticker=None, limit=200):
            return [
                {"side": "yes", "action": "buy", "count": 100, "yes_price": 87, "fee": 8},
                {"side": "yes", "action": "buy", "count": 50, "yes_price": 89, "fee": 4},
                {"side": "yes", "action": "sell", "count": 30, "yes_price": 95, "fee": 3},
            ]
    pos = bot.reconcile_position_from_fills(_StubClient(), "T", logging.getLogger("t"))
    chk.ok("reconcile non-null", pos is not None)
    # net = 100 + 50 - 30 = 120
    chk.eq("reconcile net contracts", pos.contracts, 120)
    # cost = 100*87 + 50*89 - 30*95 = 8700 + 4450 - 2850 = 10300; avg = 10300/120 = 85.8 -> 86
    chk.eq("reconcile avg entry", pos.entry_price_cents, 86)

    class _FlatClient:
        def get_fills(self, ticker=None, limit=200):
            return [
                {"side": "yes", "action": "buy", "count": 10, "yes_price": 87, "fee": 1},
                {"side": "yes", "action": "sell", "count": 10, "yes_price": 90, "fee": 1},
            ]
    chk.ok("reconcile flat -> None",
           bot.reconcile_position_from_fills(_FlatClient(), "T", logging.getLogger("t")) is None)


def _test_paper_harness(chk, cfg):
    """Drive run_paper with a scripted synthetic book through a full trade."""
    tmp = tempfile.mkdtemp()
    cfg2 = bot.Config()
    cfg2.db_path = os.path.join(tmp, "test.db")
    cfg2.take_profit_cents = 99

    # Book script: enter at 87c, then rise to a 99c bid (take-profit).
    books = [
        bot.TopOfBook(yes_bid=86, yes_ask=87),   # cycle 0: entry
        bot.TopOfBook(yes_bid=92, yes_ask=94),   # cycle 1: hold
        bot.TopOfBook(yes_bid=99, yes_ask=100),  # cycle 2: take-profit
    ]
    state = {"i": 0}

    def book_source(_ticker):
        b = books[min(state["i"], len(books) - 1)]
        state["i"] += 1
        return b

    summary = bot.run_paper(cfg2, client=None, logger=logging.getLogger("t"),
                            cycles=3, interval=0.0, book_source=book_source,
                            ticker="KXBTCD-TEST")
    chk.eq("paper produced 1 trade", summary.get("trades"), 1)
    chk.eq("paper win rate 100%", summary.get("win_rate"), 1.0)
    chk.ok("paper positive net P&L", summary.get("total_net_pnl_cents", 0) > 0)
    # Entry 87c -> exit 99c on ~11 contracts nets ~ (99-87)*11 - fees > 0.
    chk.ok("paper expectancy computed", "net_expectancy_per_1000_cents" in summary)


def _test_sign_strips_query(chk):
    """The signed path must exclude the query string (Kalshi requirement)."""
    if not bot._HAVE_CRYPTO:
        print("  SKIP  sign strips query (cryptography not installed)")
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    cfg = bot.Config()
    cfg.api_key_id = "kid"
    cfg.private_key_pem = pem
    signer = bot.KalshiSigner(cfg)
    client = bot.KalshiClient(cfg, signer, logging.getLogger("t"))

    captured = {}
    real_sign = signer.sign
    signer.sign = lambda method, path: (captured.__setitem__("path", path) or real_sign(method, path))

    class _Resp:
        status_code = 200
        content = b'{"fills":[]}'

        def json(self):
            return {"fills": []}

        def raise_for_status(self):
            pass

    client.session.request = lambda *a, **k: _Resp()
    client.get_fills(ticker="KXBTCD-X", limit=200)
    chk.eq("signed path has no query string",
           captured.get("path"), "/trade-api/v2/portfolio/fills")


def _test_get_top_from_market(chk):
    cfg = bot.Config()
    client = bot.KalshiClient(cfg, bot.KalshiSigner(cfg), logging.getLogger("t"))
    # Market object carries a quote -> used directly (order book not needed).
    client.get_market = lambda t: {"yes_bid": 86, "yes_ask": 88}
    client.get_orderbook = lambda t, depth=1: {"yes": [], "no": []}
    top = client.get_top("X")
    chk.eq("get_top uses market yes_bid", top.yes_bid, 86)
    chk.eq("get_top uses market yes_ask", top.yes_ask, 88)
    # Empty market quote -> fall back to the order book endpoint.
    client.get_market = lambda t: {"yes_bid": 0, "yes_ask": 0}
    client.get_orderbook = lambda t, depth=1: {"yes": [[70, 5]], "no": [[11, 3]]}
    top2 = client.get_top("X")
    chk.eq("get_top falls back to orderbook", (top2.yes_bid, top2.yes_ask), (70, 89))


def _test_market_selection(chk):
    cfg = bot.Config()  # entry band 85-90

    class _Ladder:
        def get_all_markets(self, **kw):
            return [
                # current hour (soonest close): empty strike, in-band strike, liquid off-band strike
                {"ticker": "KX-A-T1", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 0,  "yes_ask": 0,  "volume": 0},
                {"ticker": "KX-A-T2", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 86, "yes_ask": 88, "volume": 500},
                {"ticker": "KX-A-T3", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 40, "yes_ask": 42, "volume": 9000},
                # later hour, in-band + high volume, but must be ignored (not soonest)
                {"ticker": "KX-B-T2", "close_time": "2026-07-11T19:00:00Z", "yes_bid": 87, "yes_ask": 89, "volume": 99999},
            ]
    t = bot.resolve_active_ticker(_Ladder(), "KXBTCD", logging.getLogger("t"), cfg)
    chk.eq("picks in-band strike in current hour", t, "KX-A-T2")

    class _NoBand:
        def get_all_markets(self, **kw):
            return [
                {"ticker": "E1", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 0,  "yes_ask": 0,  "volume": 0},
                {"ticker": "E2", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 40, "yes_ask": 42, "volume": 100},
                {"ticker": "E3", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 55, "yes_ask": 57, "volume": 9000},
            ]
    chk.eq("no in-band -> most liquid quoted",
           bot.resolve_active_ticker(_NoBand(), "KXBTCD", logging.getLogger("t"), cfg), "E3")

    class _Empty:
        def get_all_markets(self, **kw):
            return []
    chk.ok("no markets -> None",
           bot.resolve_active_ticker(_Empty(), "KXBTCD", logging.getLogger("t"), cfg) is None)

    # List omits quotes -> probe across the strike ladder for a live quote.
    quotes = {"KXBTCD-H-T110000": (86, 88)}  # only the near-money strike is quoted

    class _Probe:
        def get_all_markets(self, **kw):
            return [
                {"ticker": "KXBTCD-H-T100000", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 0, "yes_ask": 0},
                {"ticker": "KXBTCD-H-T110000", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 0, "yes_ask": 0},
                {"ticker": "KXBTCD-H-T120000", "close_time": "2026-07-11T18:00:00Z", "yes_bid": 0, "yes_ask": 0},
            ]

        def get_market(self, ticker):
            yb, ya = quotes.get(ticker, (0, 0))
            return {"yes_bid": yb, "yes_ask": ya}

    chk.eq("probe across ladder finds in-band strike",
           bot.resolve_active_ticker(_Probe(), "KXBTCD", logging.getLogger("t"), cfg),
           "KXBTCD-H-T110000")


def _test_supabase_sink(chk):
    cfg = bot.Config()
    cfg.supabase_url = "https://example.supabase.co"
    cfg.supabase_service_key = "svc-role-key"
    sink = bot.SupabaseSink(cfg, logging.getLogger("t"))
    chk.ok("sink enabled when configured", sink.enabled)

    calls = []

    class _Resp:
        status_code = 201
        text = ""

    class _StubSession:
        headers = {}

        def post(self, url, params=None, headers=None, json=None, timeout=None):
            calls.append(("POST", url, json))
            return _Resp()

        def patch(self, url, params=None, headers=None, json=None, timeout=None):
            calls.append(("PATCH", url, json))
            return _Resp()

    sink._session = _StubSession()
    sink.start_run("run-1", "paper")
    sink.record_trade("run-1", "T", "take_profit", 10, 87, 99, 1, 1, 1000.0, 1005.0)

    trade_calls = [c for c in calls if c[1].endswith("/kalshi_trades")]
    chk.eq("trade posted once", len(trade_calls), 1)
    row = trade_calls[0][2]
    chk.eq("sink gross_pnl", row["gross_pnl"], (99 - 87) * 10)
    chk.eq("sink net_pnl", row["net_pnl"], (99 - 87) * 10 - 1 - 1)
    chk.eq("sink notional", row["notional"], 87 * 10)
    chk.ok("run posted to kalshi_runs",
           any(c[1].endswith("/kalshi_runs") for c in calls))

    # Disabled sink (no env) must be a silent no-op.
    cfg2 = bot.Config()
    cfg2.supabase_url = None
    cfg2.supabase_service_key = None
    sink2 = bot.SupabaseSink(cfg2, logging.getLogger("t"))
    chk.ok("sink disabled without config", not sink2.enabled)
    sink2.record_trade("r", "T", "stop_loss", 1, 50, 49, 1, 1, 0.0, 1.0)  # no raise
    chk.ok("disabled sink no-op", True)


def _test_pem_normalize(chk):
    """A PEM mangled by an env-var editor must still load (no startup crash)."""
    if not bot._HAVE_CRYPTO:
        print("  SKIP  pem normalize (cryptography not installed)")
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,  # -----BEGIN RSA PRIVATE KEY-----
        serialization.NoEncryption(),
    ).decode()

    def _ready(pem_or=None, b64=None):
        c = bot.Config()
        c.api_key_id = "kid"
        c.private_key_pem = pem_or
        c.private_key_b64 = b64
        s = bot.KalshiSigner(c)
        return s.ready and not s.load_error

    # Case 1: real newlines replaced by the literal two chars backslash-n.
    chk.ok("literal \\n PEM repaired", _ready(pem.replace("\n", "\\n")))
    # Case 2: newlines replaced by spaces AND header/footer jammed on (Railway).
    chk.ok("space-mangled PEM repaired", _ready(pem.replace("\n", " ")))
    # Case 3: everything jammed onto a single line, no separators at all.
    chk.ok("fully-jammed PEM repaired", _ready(pem.replace("\n", "")))
    # Case 4: base64 of the PEM file (the unmanglable path).
    import base64 as _b64
    chk.ok("base64 key path works", _ready(b64=_b64.b64encode(pem.encode()).decode()))

    # Case 5: a bad key sets load_error but does NOT raise (no crash).
    cfg2 = bot.Config()
    cfg2.api_key_id = "kid"
    cfg2.private_key_pem = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----"
    s2 = bot.KalshiSigner(cfg2)
    chk.ok("bad key -> not ready, error recorded, no crash",
           (not s2.ready) and bool(s2.load_error))


def _test_ai_layer(chk):
    from types import SimpleNamespace as NS
    import ai_strategy as ais

    # Guardrails: AI can only veto or size DOWN, never up / never create entries.
    chk.eq("no AI -> rules stand", ais.gate_entry(None, 100, "advisory")[:2], (True, 100))
    hold = NS(action="hold", max_contracts=0, reason="thin", confidence=0.2)
    chk.ok("AI veto blocks entry", ais.gate_entry(hold, 100, "advisory")[0] is False)
    enter = NS(action="enter", max_contracts=10, reason="ok", confidence=0.9)
    chk.eq("advisory keeps rule size", ais.gate_entry(enter, 100, "advisory")[1], 100)
    chk.eq("decider sizes DOWN", ais.gate_entry(enter, 100, "decider")[1], 10)
    big = NS(action="enter", max_contracts=999, reason="ok", confidence=0.9)
    chk.eq("decider never sizes UP", ais.gate_entry(big, 100, "decider")[1], 100)

    chk.eq("AI early exit fires", ais.ai_early_exit(NS(action="exit")), "exit")
    chk.eq("AI scale_out fires", ais.ai_early_exit(NS(action="scale_out")), "scale_out")
    chk.ok("no early exit on hold", ais.ai_early_exit(NS(action="hold")) is None)
    chk.ok("no early exit on None", ais.ai_early_exit(None) is None)

    # Off by default: no flag / no key -> disabled, decide() returns None.
    strat = ais.AIStrategy(bot.Config(), logging.getLogger("t"))
    chk.ok("AI off by default", not strat.enabled)
    chk.ok("disabled decide -> None", strat.decide({}) is None)

    # Enabled path with an injected client (no network) parses a decision.
    if ais._HAVE_AI:
        import os
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        cfg = bot.Config()
        cfg.ai_enabled = True

        class _Stub:
            class messages:
                @staticmethod
                def parse(**kw):
                    return NS(parsed_output=NS(action="enter", confidence=0.8,
                                               max_contracts=5, reason="good setup"))
        s = ais.AIStrategy(cfg, logging.getLogger("t"), client=_Stub())
        chk.ok("AI enabled with key+client", s.enabled)
        chk.eq("AI decide parses action", s.decide({"x": 1}).action, "enter")
        os.environ.pop("ANTHROPIC_API_KEY", None)


def _test_signing(chk):
    """Generate an ephemeral RSA key, sign, and verify RSA-PSS/SHA-256."""
    if not bot._HAVE_CRYPTO:
        print("  SKIP  signing (cryptography not installed)")
        return
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    cfg = bot.Config()
    cfg.api_key_id = "test-key-id"
    cfg.private_key_pem = pem
    signer = bot.KalshiSigner(cfg)
    chk.ok("signer ready", signer.ready)

    headers = signer.sign("GET", "/trade-api/v2/portfolio/fills")
    chk.ok("has access key header", headers.get("KALSHI-ACCESS-KEY") == "test-key-id")
    chk.ok("has timestamp header", bool(headers.get("KALSHI-ACCESS-TIMESTAMP")))

    import base64
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    message = f"{ts}GET/trade-api/v2/portfolio/fills".encode()
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    try:
        key.public_key().verify(
            sig, message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        chk.ok("signature verifies", True)
    except Exception as exc:
        chk.ok(f"signature verifies ({exc})", False)


def run_selftests(cfg=None, logger=None) -> int:
    cfg = cfg or bot.Config()
    # Silence the file logger's console noise during tests.
    logging.getLogger("kalshi_btc").handlers.clear()
    chk = _Check()
    print("Running offline self-tests...\n")
    _test_fees(chk, cfg)
    _test_sizing(chk)
    _test_decisions(chk, cfg)
    _test_orderbook(chk)
    _test_ws_parsing(chk, cfg)
    _test_ws_desync(chk, cfg)
    _test_manage_dryrun(chk)
    _test_supabase_sink(chk)
    _test_market_selection(chk)
    _test_get_top_from_market(chk)
    _test_ai_layer(chk)
    _test_sign_strips_query(chk)
    _test_pem_normalize(chk)
    _test_reconcile(chk, cfg)
    _test_paper_harness(chk, cfg)
    _test_signing(chk)
    print(f"\n{chk.passed} passed, {chk.failed} failed")
    return 0 if chk.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_selftests())
