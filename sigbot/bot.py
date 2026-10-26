"""Main loop: read state, compare books to fair value, place (or log) orders."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import strategy
from .client import ApiError, Client
from .fair import FairValues
from .longshot import read_hold

DEFAULTS = {
    "base_url": "https://sig.thesuper.market/api/v1",
    "tournament_slug": "midterm-elections",   # null = auto-detect
    "fair_values_file": "fair_values.json",   # written by election_forecast.py
    "max_model_age_hours": 36,
    "max_model_gap": 0.15,              # model farther than this from Kalshi/Polymarket is ignored
    "stop_file": "STOP",                # same kill switch as stop.ps1: present = no live orders
    "hold_file": "hold.txt",            # exchange ids the bot never trades (written by `sigbot buy`)
    "competition_end_utc": "2026-11-04T17:00:00Z",   # 12:00pm EST, Nov 4 (official rules, s.6)
    "stop_minutes_before_end": 3,
    "loop_seconds": 20,
    "markets_refresh_cycles": 15,
    "reads_per_min": 80,                # reported account-wide limit is 100 reads / 30 writes per min
    "writes_per_min": 25,
    "max_orders_per_cycle": 8,
    "max_books_per_cycle": 10,          # full-book reads per cycle; keeps a 20s loop under the read limit

    # taker
    "take_edge": 0.03,                  # cross only when book is >= 3 points away from fair
    "exit_mode": "profit",              # "profit": sell only well above cost; "fair": sell once at fair value
    "take_profit_pct": 0.25,            # profit mode: sell when the sale price is >= 25% above avg cost
    "exit_edge": 0.0,                   # fair mode: close once the other side trades at/through fair
    "kelly_mult": 0.5,                  # half-Kelly
    "max_position_value": 15000,        # SUSQies at cost, per exchange
    "max_position_frac": 0.20,          # ...and at most this share of equity per exchange
    "max_group_value": 15000,           # per race: the DEM and REP contracts on one race share this
    "max_gross_exposure_frac": 0.95,    # sum of |position value| / equity
    "max_cash_per_order_frac": 0.30,
    "min_qty": 5,
    "take_order_ttl_s": 20,             # unfilled remainder of a taker order expires quickly

    # maker (off by default; spends write budget)
    "make_enabled": False,
    "make_half_spread": 0.03,
    "make_min_edge": 0.015,
    "make_size": 100,
    "quote_ttl_s": 120,
    "requote_threshold": 0.01,

    # multi-outcome arbitrage
    "arb_enabled": True,
    "arb_every_cycles": 3,
    "arb_min_edge": 0.01,
    "arb_max_qty": 5000,

    "fair_cache_seconds": 45,
    "fair_max_spread": 0.10,
    "log_file": "trades.jsonl",
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:   # -sig: PowerShell 5.1 writes a BOM
            cfg.update({k: v for k, v in json.load(fh).items() if not k.startswith("_")})
    return cfg


KEY_NAMES = ("SUPER_MARKET_API_KEY", "SIG_API_KEY")


def load_api_key():
    key = next((os.environ[n] for n in KEY_NAMES if os.environ.get(n)), None)
    if not key and os.path.exists(".env"):
        with open(".env", encoding="utf-8-sig") as fh:
            for line in fh:
                name, _, value = line.strip().partition("=")
                if name in KEY_NAMES and value:
                    key = value.strip().strip('"').strip("'")
    return key


def now_utc():
    return datetime.now(timezone.utc)


def iso_in(seconds):
    return (now_utc() + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def log(msg):
    print(f"{now_utc().strftime('%H:%M:%S')} {msg}", flush=True)


def resolve_tournament(client, slug=None):
    if slug:
        t = client.tournament(slug)
        return t["slug"], t["id"], t
    ts = client.tournaments(status="active")
    if not ts:
        raise SystemExit("No active tournaments on this key. Register for the Predictions Cup and accept the terms on the site.")
    cup = [t for t in ts if "cup" in t["name"].lower() or "prediction" in t["name"].lower()]
    pick = (cup or ts)[0]
    if len(cup or ts) > 1:
        log(f"Several tournaments found, using '{pick['name']}' ({pick['slug']}). Set tournament_slug to choose.")
    return pick["slug"], pick["id"], pick


class Bot:
    def __init__(self, cfg, client, live=False):
        self.cfg = cfg
        self.c = client
        self.live = live
        self.slug, self.tid, _ = resolve_tournament(client, cfg["tournament_slug"])
        self.fair = FairValues(cfg["fair_values_file"], cfg["fair_cache_seconds"], cfg["fair_max_spread"], log,
                               cfg["max_model_age_hours"], cfg["max_model_gap"])
        self.markets = []
        self.ex_info = {}      # exchange_id -> (market, exchange)
        self.cycle = 0
        end = datetime.fromisoformat(cfg["competition_end_utc"].replace("Z", "+00:00"))
        self.stop_at = end - timedelta(minutes=cfg["stop_minutes_before_end"])

    def stopped(self):
        return bool(self.cfg["stop_file"]) and os.path.exists(self.cfg["stop_file"])

    # ---- state -----------------------------------------------------------
    def refresh_markets(self):
        self.markets = self.c.tournament_markets(self.slug, status="open")
        self.ex_info = {str(ex["id"]): (m, ex) for m in self.markets for ex in m["exchanges"]}

    def read_positions(self):
        try:
            data = self.c.positions(self.slug)
        except ApiError as e:
            if e.status == 409:   # holdings without a valuation price yet
                log(f"[warn] positions unavailable: {e}")
                return {}, 0.0
            raise
        pos = {p["exchangeId"]: p for p in data["positions"] if not p["settled"] and p["quantity"]}
        return pos, data["summary"]["totalMarketValue"]

    # ---- execution -------------------------------------------------------
    def record(self, entry):
        entry["ts"] = now_utc().isoformat()
        entry["live"] = self.live
        with open(self.cfg["log_file"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def send(self, it):
        m, ex = self.ex_info.get(it.exchange_id, ({"title": "?"}, {"option": None}))
        label = f"{m['title'][:48]}{' / ' + ex['option'] if ex.get('option') else ''}"
        tag = "LIVE" if self.live else "DRY "
        log(f"{tag} {it.kind:4} {it.action} {it.quantity:>6} {it.side.upper():3} @ {it.price:.3f}  "
            f"edge {it.edge:+.3f}  [{label}] {it.reason}")
        entry = {"type": "order", "exchangeId": it.exchange_id, "market": label, **{
            k: getattr(it, k) for k in ("side", "action", "quantity", "price", "edge", "kind", "reason")}}
        if not self.live:
            self.record(entry)
            return None
        if self.stopped():
            log("     STOP file present: not sent.")
            return None
        ttl = self.cfg["quote_ttl_s"] if it.kind == "make" else self.cfg["take_order_ttl_s"]
        try:
            res = self.c.place_order(it.exchange_id, it.side, it.action, it.quantity, it.price, self.tid, iso_in(ttl))
            entry["result"] = res
            if res.get("quantityTraded"):
                log(f"     filled {res['quantityTraded']} for {res['totalCost']:.2f}"
                    f"{' (resting ' + str(res.get('remainingQuantity')) + ')' if res.get('open') else ''}")
        except ApiError as e:
            entry["error"] = {"status": e.status, "code": e.code, "message": str(e)}
            log(f"     rejected: {e}")
        self.record(entry)
        return entry.get("result")

    # ---- one pass --------------------------------------------------------
    def step(self):
        cfg = self.cfg
        if self.cycle % cfg["markets_refresh_cycles"] == 0 or not self.markets:
            self.refresh_markets()
            self.fair.reload()
        self.cycle += 1

        hold = read_hold(cfg["hold_file"])   # re-read each cycle: `sigbot buy` can add to it while running
        tracked = [e for e in self.fair.entries if e in self.ex_info and e not in hold]
        cash = self.c.account(self.tid)["balance"] or 0.0
        pos, holdings = self.read_positions()
        equity = cash + holdings
        gross = sum(abs(p["marketValue"]) for p in pos.values())
        held = [e for e in pos if e not in hold and e not in tracked]   # positions with no fair value
        quotes = self.c.prices(tracked + held, self.tid) if tracked or held else {}
        open_orders = self.c.open_orders(self.tid) if cfg["make_enabled"] else []

        # 1) take profits first. Proceeds only show up in cash next cycle, so a cycle that sells
        #    stops here and the next one buys with the updated balance.
        if cfg["exit_mode"] == "profit":
            sold = self.take_profits(pos, hold, quotes)
            if sold:
                log(f"cycle {self.cycle}: took profit on {sold} position(s); buying resumes next cycle")
                return

        # rank candidates by top-of-book edge before spending reads on full books
        # per-race exposure: DEM-YES and REP-YES on one race are the same bet
        group_of = lambda ex: self.fair.entry(ex).get("group") or ex
        group_gross = {}
        for ex_id, p in pos.items():
            group_gross[group_of(ex_id)] = group_gross.get(group_of(ex_id), 0) + abs(p["marketValue"])

        # rank candidates by top-of-book edge before spending reads on full books
        cands = []
        for ex_id in tracked:
            q = quotes.get(ex_id)
            fair, srcs = self.fair.get(ex_id)
            if q is None or fair is None:
                continue
            entry = self.fair.entry(ex_id)
            ecfg = {**cfg, "take_edge": entry.get("take_edge", cfg["take_edge"]),
                    "kelly_mult": cfg["kelly_mult"] * entry.get("size_mult", 1.0)}
            bid, ask = q["bestBid"], q["bestAsk"]
            position = pos.get(ex_id, {}).get("quantity", 0)
            lo, hi = self.fair.bounds(ex_id, fair, srcs)
            yes_edge = lo - ask if ask is not None else -1
            no_edge = bid - hi if bid is not None else -1
            fair = lo if yes_edge >= no_edge else hi   # judge each side at its adverse bound
            edge = max(yes_edge, no_edge)
            if cfg["exit_mode"] == "fair":   # sell-at-fair exits; "profit" mode sold above instead
                if position > 0 and bid is not None:
                    edge = max(edge, bid - fair + ecfg["take_edge"] - cfg["exit_edge"])
                if position < 0 and ask is not None:
                    edge = max(edge, fair - ask + ecfg["take_edge"] - cfg["exit_edge"])
            cands.append((edge - ecfg["take_edge"], edge, ex_id, fair, srcs, q, position, ecfg))
        cands.sort(key=lambda c: c[0], reverse=True)

        intents = []
        books = 0
        for excess, edge, ex_id, fair, srcs, q, position, ecfg in cands:
            if excess >= 0:
                if books >= cfg["max_books_per_cycle"]:   # best edges first; the rest wait a cycle
                    continue
                books += 1
                book = self.c.orderbook(ex_id, self.tid, depth=20)
                intents += strategy.decide_take(ex_id, fair, book, position, equity, cash, ecfg)
            elif cfg["make_enabled"]:
                mine = [o for o in open_orders if o["exchangeId"] == ex_id]
                intents += strategy.decide_make(ex_id, fair, q["bestBid"], q["bestAsk"], position, equity, mine, ecfg)

        # risk: exits always allowed; new exposure only within gross and per-race limits
        intents.sort(key=lambda i: (i.kind != "exit", -i.edge))
        sent = 0
        for it in intents:
            if sent >= cfg["max_orders_per_cycle"]:
                break
            if it.kind != "exit":
                g = group_of(it.exchange_id)
                room = cfg["max_group_value"] - group_gross.get(g, 0)
                if room < it.price * cfg["min_qty"]:
                    continue
                if it.cost() > room:
                    it.quantity = int(room / it.price)
                if gross + it.cost() > cfg["max_gross_exposure_frac"] * equity:
                    continue
                gross += it.cost()
                group_gross[g] = group_gross.get(g, 0) + it.cost()
            self.send(it)
            sent += 1

        if cfg["arb_enabled"] and self.cycle % cfg["arb_every_cycles"] == 0:
            sent += self.scan_arb(cash)

        log(f"cycle {self.cycle}: cash {cash:,.0f}  equity {equity:,.0f}  gross {gross:,.0f}  "
            f"tracked {len(tracked)}/{len(self.ex_info)}  orders {sent}  "
            f"reads left {self.c.reads.remaining()}/min")

    def take_profits(self, pos, hold, quotes):
        """Sell positions trading well above their average cost. Never touches hold.txt entries."""
        cfg = self.cfg
        sold = books = 0
        for ex_id, p in pos.items():
            if ex_id in hold or sold >= cfg["max_orders_per_cycle"]:
                continue
            q = quotes.get(ex_id)
            if not q or not p.get("avgCost"):
                continue
            target = p["avgCost"] * (1 + cfg["take_profit_pct"])
            # cheap check on top of book before spending a read on the full book
            sell_now = q["bestBid"] if p["quantity"] > 0 else (1 - q["bestAsk"] if q["bestAsk"] is not None else None)
            if sell_now is None or sell_now < target - 1e-9 or books >= cfg["max_books_per_cycle"]:
                continue
            books += 1
            book = self.c.orderbook(ex_id, self.tid, depth=20)
            for it in strategy.decide_take_profit(ex_id, p["quantity"], p["avgCost"], book, cfg):
                self.send(it)
                sold += 1
        return sold

    def scan_arb(self, cash):
        n = 0
        for m in self.markets:
            if not m.get("isMultiOutcome") or len(m["exchanges"]) < 2:
                continue
            detail = self.c.tournament_market(self.slug, m["id"])
            arb = strategy.decide_arb(detail, cash, self.cfg)
            if not arb:
                continue
            log(f"{'LIVE' if self.live else 'DRY '} arb  {arb['qty']} of each of {len(arb['legs'])} outcomes, "
                f"cost {arb['cost']:,.2f}, locked edge {arb['edge']:.3f}/share [{m['title'][:60]}]")
            entry = {"type": "arb", "marketId": m["id"], "market": m["title"], **arb}
            if self.live and self.stopped():
                log("     STOP file present: not sent.")
            elif self.live:
                try:
                    entry["result"] = self.c.multi_leg(arb["legs"], self.tid, all_or_none=True)
                except ApiError as e:
                    entry["error"] = {"status": e.status, "code": e.code, "message": str(e)}
                    log(f"     arb rejected: {e}")
            self.record(entry)
            n += 1
        return n

    def run(self):
        log(f"tournament {self.slug} ({self.tid})  mode {'LIVE' if self.live else 'DRY-RUN'}  "
            f"stops at {self.stop_at.isoformat()}")
        if self.live and self.stopped():
            raise SystemExit(f"'{self.cfg['stop_file']}' file present: refusing to trade live. Delete it to go live.")
        try:
            while True:
                if now_utc() >= self.stop_at:
                    log("Competition end reached; stopping.")
                    break
                if self.live and self.stopped():
                    log("STOP file detected; stopping.")
                    break
                t0 = time.time()
                try:
                    self.step()
                except ApiError as e:
                    log(f"[error] {e}")
                    if e.code in ("INVALID_API_KEY", "API_KEY_REVOKED", "API_KEY_EXPIRED", "ACCOUNT_BANNED",
                                  "TERMS_NOT_ACKNOWLEDGED", "INSUFFICIENT_SCOPES", "RESIDENCE_UPDATE_REQUIRED"):
                        raise SystemExit(f"Fatal: {e}. Fix this on the site, then restart.")
                except FileNotFoundError as e:
                    raise SystemExit(f"Missing file: {e.filename}")
                time.sleep(max(1.0, self.cfg["loop_seconds"] - (time.time() - t0)))
        except KeyboardInterrupt:
            log("Interrupted.")
        finally:
            if self.live:
                self.cancel_own_orders()

    def cancel_own_orders(self):
        """Cancel resting orders on exit, except on held exchanges (a `sigbot buy --rest` order
        waiting for sellers must survive bot restarts)."""
        hold = read_hold(self.cfg["hold_file"])
        try:
            mine = [o for o in self.c.open_orders(self.tid) if str(o["exchangeId"]) not in hold]
            for o in mine:
                self.c.cancel(o["id"])
            kept = f", kept orders on {len(hold)} held exchange(s)" if hold else ""
            log(f"Cancelled {len(mine)} resting orders{kept}.")
        except ApiError as e:
            log(f"[warn] cancelling orders failed: {e}")
