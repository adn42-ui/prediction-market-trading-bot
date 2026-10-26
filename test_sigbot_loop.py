"""Runs the full bot loop against an in-memory fake of the API (no network for SIG).
Set SIGBOT_ONLINE=1 to also pull live Kalshi/Polymarket prices for fair value."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sigbot.bot import DEFAULTS, Bot  # noqa: E402
from sigbot.client import RateLimiter  # noqa: E402


class FakeClient:
    def __init__(self):
        self.reads = RateLimiter(1000)
        self.orders, self.multi, self.cancelled = [], [], 0
        self.books = {
            "101": {"bids": [{"price": 0.50, "quantity": 2000}], "asks": [{"price": 0.55, "quantity": 2000}]},  # fair 0.70 -> buy YES
            "102": {"bids": [{"price": 0.48, "quantity": 2000}], "asks": [{"price": 0.52, "quantity": 2000}]},  # fair 0.50 -> nothing
            "103": {"bids": [{"price": 0.40, "quantity": 800}], "asks": [{"price": 0.45, "quantity": 800}]},    # fair 0.30 -> buy NO
            "201": {"bids": [], "asks": [{"price": 0.30, "quantity": 400}]},   # multi-outcome legs summing to 0.90
            "202": {"bids": [], "asks": [{"price": 0.30, "quantity": 600}]},
            "203": {"bids": [], "asks": [{"price": 0.30, "quantity": 900}]},
        }

    def tournaments(self, status="active"):
        return [{"id": "t-uuid", "slug": "midterm-elections", "name": "Predictions Cup"}]

    def tournament(self, slug):
        return {"id": "t-uuid", "slug": slug, "name": "Predictions Cup"}

    def tournament_markets(self, slug, status="open"):
        return [
            {"id": "1", "title": "Dems win House", "isMultiOutcome": False, "exchanges": [{"id": "101", "option": None}]},
            {"id": "2", "title": "Coin flip race", "isMultiOutcome": False, "exchanges": [{"id": "102", "option": None}]},
            {"id": "3", "title": "Long-shot race", "isMultiOutcome": False, "exchanges": [{"id": "103", "option": None}]},
            {"id": "4", "title": "Which party wins X", "isMultiOutcome": True,
             "exchanges": [{"id": i, "option": o} for i, o in (("201", "A"), ("202", "B"), ("203", "C"))]},
        ]

    def tournament_market(self, slug, mid):
        m = [x for x in self.tournament_markets(slug) if x["id"] == mid][0]
        return {**m, "hasArbitrageOpportunity": True,
                "exchanges": [{**e, **self.books[e["id"]]} for e in m["exchanges"]]}

    def account(self, tid=None):
        return {"balance": 100000.0}

    def positions(self, slug):
        return {"positions": [], "summary": {"totalMarketValue": 0.0}}

    def prices(self, ids, tid):
        out = {}
        for i in ids:
            b = self.books[i]
            out[i] = {"exchangeId": i, "bestBid": b["bids"][0]["price"] if b["bids"] else None,
                      "bestAsk": b["asks"][0]["price"] if b["asks"] else None}
        return out

    def orderbook(self, ex, tid, depth=20):
        return self.books[ex]

    def open_orders(self, tid):
        return self.resting

    def place_order(self, ex, side, action, qty, price, tid, expires_at=None):
        self.orders.append((ex, side, action, qty, price, expires_at))
        return {"orderId": len(self.orders), "open": False, "quantityTraded": qty, "totalCost": qty * price}

    def multi_leg(self, legs, tid, all_or_none=True):
        self.multi.append(legs)
        return {"results": []}

    def cancel_all(self, tid):
        self.cancelled += 1
        return {"cancelled": 0}

    resting = []
    cancelled_ids = []

    def cancel(self, order_id):
        self.cancelled_ids = self.cancelled_ids + [order_id]
        return {"orderId": order_id}


class BotLoop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fv = {"markets": {"101": {"label": "Dems House", "manual": 0.70},
                          "102": {"label": "Coin flip", "manual": 0.50},
                          "103": {"label": "Long shot", "manual": 0.30}}}
        if os.environ.get("SIGBOT_ONLINE"):
            fv["markets"]["901"] = {"label": "live check", "kalshi": "CONTROLH-2026-D",
                                    "polymarket": "will-the-democratic-party-control-the-house-after-the-2026-midterm-elections",
                                    "polymarket_outcome": "Yes"}
        self.fv_path = os.path.join(self.dir, "fv.json")
        with open(self.fv_path, "w") as fh:
            json.dump(fv, fh)
        self.cfg = dict(DEFAULTS, fair_values_file=self.fv_path, log_file=os.path.join(self.dir, "log.jsonl"),
                        arb_every_cycles=1, competition_end_utc="2099-01-01T00:00:00Z",
                        stop_file=os.path.join(self.dir, "STOP"))

    def write_fv(self, fv):
        with open(self.fv_path, "w") as fh:
            json.dump(fv, fh)

    def test_stop_file_blocks_live_orders(self):
        open(self.cfg["stop_file"], "w").close()
        c = FakeClient()
        bot = Bot(self.cfg, c, live=True)
        with self.assertRaises(SystemExit):
            bot.run()
        bot.step()  # even if the loop were bypassed, every write path refuses
        self.assertEqual(c.orders, [])
        self.assertEqual(c.multi, [])

    def test_model_source_and_per_entry_edge(self):
        from datetime import datetime, timezone
        # model says 0.62 vs ask 0.55 (7 pts): trades at the default 3-pt edge, not with a model-only 8-pt edge
        self.write_fv({"generated_at": datetime.now(timezone.utc).isoformat(),
                       "markets": {"101": {"label": "a", "model": 0.62, "take_edge": 0.08},
                                   "103": {"label": "b", "model": 0.30}}})
        c = FakeClient()
        Bot(dict(self.cfg, arb_enabled=False), c, live=True).step()
        self.assertEqual([o[0] for o in c.orders], ["103"])

    def test_stale_model_is_ignored(self):
        self.write_fv({"generated_at": "2026-10-01T00:00:00+00:00",
                       "markets": {"101": {"label": "a", "model": 0.90}, "103": {"label": "b", "manual": 0.30}}})
        c = FakeClient()
        bot = Bot(dict(self.cfg, arb_enabled=False), c, live=True)
        self.assertNotIn("101", bot.fair.entries)
        bot.step()
        self.assertEqual([o[0] for o in c.orders], ["103"])

    def test_model_dropped_when_far_from_kalshi(self):
        from unittest import mock
        from sigbot import fair as fair_mod
        self.write_fv({"markets": {"1": {"label": "VT gov R", "kalshi": "GOVPARTYVT-26-R", "model": 0.01},
                                   "2": {"label": "close", "kalshi": "X", "model": 0.60,
                                         "weights": {"kalshi": 3, "model": 1}}}})
        logs = []
        with mock.patch.object(fair_mod, "kalshi_prob", side_effect=lambda t, s: 0.74 if t.startswith("GOV") else 0.56):
            fv = fair_mod.FairValues(self.fv_path, log=logs.append)
            vt, vt_src = fv.get("1")
            close, _ = fv.get("2")
        self.assertAlmostEqual(vt, 0.74)
        self.assertNotIn("model", vt_src)
        self.assertTrue(any("dropping model" in m for m in logs))
        self.assertAlmostEqual(close, (3 * 0.56 + 0.60) / 4)   # within 15 pts: blended

    def test_model_only_trades_against_its_adverse_bound(self):
        # replay of the first live dry run: FL-16 D model 0.395 (range 0.12-0.73) vs ask 0.22 was
        # going to be bought; with the range it is not. CO-04 D (range up to 0.12) vs bid 0.30 still is.
        books = {"101": {"bids": [{"price": 0.20, "quantity": 5000}], "asks": [{"price": 0.22, "quantity": 5000}]},
                 "103": {"bids": [{"price": 0.30, "quantity": 5000}], "asks": [{"price": 0.33, "quantity": 5000}]}}
        self.write_fv({"markets": {
            "101": {"label": "FL-16 D", "model": 0.395, "model_range": [0.1246, 0.7306], "take_edge": 0.08},
            "103": {"label": "CO-04 D", "model": 0.0227, "model_range": [0.0024, 0.1188], "take_edge": 0.08}}})
        c = FakeClient()
        c.books.update(books)
        Bot(dict(self.cfg, arb_enabled=False), c, live=True).step()
        self.assertEqual([(o[0], o[1]) for o in c.orders], [("103", "no")])

    def test_range_ignored_when_a_market_price_exists(self):
        from unittest import mock
        from sigbot import fair as fair_mod
        self.write_fv({"markets": {"1": {"label": "x", "kalshi": "K", "model": 0.40, "model_range": [0.1, 0.7]}}})
        with mock.patch.object(fair_mod, "kalshi_prob", return_value=0.42):
            fv = fair_mod.FairValues(self.fv_path, log=lambda m: None)
            fair, srcs = fv.get("1")
        self.assertEqual(fv.bounds("1", fair, srcs), (fair, fair))

    def test_book_reads_capped_per_cycle(self):
        self.write_fv({"markets": {k: {"label": k, "manual": 0.9} for k in ("101", "102", "103")}})
        c = FakeClient()
        calls = []
        real = c.orderbook
        c.orderbook = lambda ex, tid, depth=20: calls.append(ex) or real(ex, tid, depth)
        Bot(dict(self.cfg, arb_enabled=False, max_books_per_cycle=2), c, live=False).step()
        self.assertEqual(len(calls), 2)

    def test_size_mult_halves_model_only_bets(self):
        # lift every cap except Kelly so the multiplier is the only difference
        cfg = dict(self.cfg, arb_enabled=False, max_position_value=10**9, max_position_frac=1,
                   max_group_value=10**9, max_cash_per_order_frac=1)
        sizes = []
        for entry in ({"label": "a", "manual": 0.70}, {"label": "a", "manual": 0.70, "size_mult": 0.5}):
            self.write_fv({"markets": {"101": entry}})
            c = FakeClient()
            c.books["101"]["asks"][0]["quantity"] = 10**6
            Bot(cfg, c, live=True).step()
            sizes.append(c.orders[0][3])
        self.assertAlmostEqual(sizes[1] * 2, sizes[0], delta=2)

    def test_group_cap_shared_by_dem_and_rep_contracts(self):
        # both contracts on one race look cheap; together they may not exceed max_group_value
        self.write_fv({"markets": {"101": {"label": "D", "manual": 0.70, "group": "race:XX-SEN"},
                                   "103": {"label": "R", "manual": 0.30, "group": "race:XX-SEN"}}})
        c = FakeClient()
        Bot(dict(self.cfg, arb_enabled=False, max_group_value=1500), c, live=True).step()
        spent = sum(o[3] * o[4] for o in c.orders)
        self.assertLessEqual(spent, 1500 + 1e-6)
        self.assertEqual(len(c.orders), 2)

    def test_dry_run_places_nothing(self):
        c = FakeClient()
        bot = Bot(self.cfg, c, live=False)
        bot.step()
        self.assertEqual(c.orders, [])
        self.assertEqual(c.multi, [])
        with open(self.cfg["log_file"]) as fh:
            kinds = [json.loads(line)["type"] for line in fh]
        self.assertEqual(sorted(kinds), ["arb", "order", "order"])

    def test_live_places_expected_orders(self):
        c = FakeClient()
        bot = Bot(self.cfg, c, live=True)
        bot.step()
        by_ex = {o[0]: o for o in c.orders}
        self.assertEqual(set(by_ex), {"101", "103"})
        self.assertEqual(by_ex["101"][1:3], ("yes", "buy"))
        self.assertEqual(by_ex["103"][1:3], ("no", "buy"))
        self.assertEqual(by_ex["103"][4], 0.6)          # NO limit = 1 - 0.40 bid
        self.assertTrue(by_ex["101"][5].endswith("Z"))  # short expiry attached
        self.assertEqual(len(c.multi), 1)
        self.assertEqual([leg["quantity"] for leg in c.multi[0]], [400, 400, 400])

    def test_live_fair_sources(self):
        if not os.environ.get("SIGBOT_ONLINE"):
            self.skipTest("set SIGBOT_ONLINE=1")
        bot = Bot(self.cfg, FakeClient(), live=False)
        fair, srcs = bot.fair.get("901")
        print("\nlive Dem-House fair:", round(fair, 4), srcs)
        self.assertTrue(0.01 < fair < 0.99)
        self.assertEqual(set(srcs), {"kalshi", "polymarket"})


if __name__ == "__main__":
    unittest.main()
