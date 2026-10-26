import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sigbot import longshot  # noqa: E402
from sigbot.bot import DEFAULTS, Bot  # noqa: E402
from sigbot.fair import FairValues  # noqa: E402
from test_sigbot_loop import FakeClient  # noqa: E402


def book(bids=(), asks=()):
    return {"bids": [{"price": p, "quantity": q} for p, q in bids],
            "asks": [{"price": p, "quantity": q} for p, q in asks]}


class Longshots(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fv_path = os.path.join(self.dir, "fv.json")

    def fv(self, markets):
        with open(self.fv_path, "w") as fh:
            json.dump({"markets": markets}, fh)
        return FairValues(self.fv_path, log=lambda m: None)

    def test_ranks_underpriced_first_and_skips_expensive(self):
        fv = self.fv({"1": {"label": "R House", "manual": 0.10},     # ask 0.07: value 1.43
                      "2": {"label": "longshot", "manual": 0.05},    # ask 0.08: value 0.63
                      "3": {"label": "favorite", "manual": 0.60}})   # ask 0.55: too expensive
        quotes = {"1": {"bestBid": 0.06, "bestAsk": 0.07}, "2": {"bestBid": 0.07, "bestAsk": 0.08},
                  "3": {"bestBid": 0.50, "bestAsk": 0.55}}
        rows = longshot.rank_longshots(fv, quotes, 0.12)
        self.assertEqual([(r[1], r[2]) for r in rows], [("1", "yes"), ("2", "yes")])
        self.assertAlmostEqual(rows[0][0], 0.10 / 0.07)

    def test_no_side_longshot(self):
        # YES bid 0.93 -> NO costs 0.07; fair YES 0.88 -> NO worth 0.12
        fv = self.fv({"1": {"label": "x", "manual": 0.88}})
        rows = longshot.rank_longshots(fv, {"1": {"bestBid": 0.93, "bestAsk": 0.95}}, 0.10)
        self.assertEqual((rows[0][2], rows[0][3]), ("no", 0.07))
        self.assertAlmostEqual(rows[0][4], 0.12)

    def test_clamped_fair_is_not_a_bargain(self):
        # Kalshi 0.3% is clamped to 0.01; at the 0.005 tick that would look like value 2.0
        fv = self.fv({"1": {"label": "x", "manual": 0.003}})
        self.assertEqual(longshot.rank_longshots(fv, {"1": {"bestBid": None, "bestAsk": 0.005}}, 0.12), [])

    def test_plan_buy_walks_book_within_budget_and_price(self):
        plan = longshot.plan_buy(book(asks=[(0.06, 50000), (0.07, 50000), (0.09, 10**6)]), "yes", 7000, 0.08)
        # both cheap levels fully taken (6,500 spent); the 0.09 level is above the price cap
        self.assertEqual((plan["qty"], plan["cost"], plan["worst"]), (100000, 6500.0, 0.07))
        self.assertEqual(plan["payout"], plan["qty"])
        tight = longshot.plan_buy(book(asks=[(0.06, 50000), (0.07, 10**6)]), "yes", 7000, 0.08)
        self.assertEqual(tight["qty"], 50000 + int(4000 / 0.07))   # budget binds first
        self.assertLessEqual(tight["cost"], 7000)

    def test_plan_buy_no_side_uses_bids(self):
        plan = longshot.plan_buy(book(bids=[(0.93, 1000), (0.90, 1000)]), "no", 10**6, 0.08)
        self.assertEqual((plan["qty"], plan["worst"]), (1000, 0.07))

    def test_plan_buy_nothing_cheap_enough(self):
        self.assertIsNone(longshot.plan_buy(book(asks=[(0.20, 100)]), "yes", 7000, 0.08))

    def test_files_written_by_powershell_with_bom_still_load(self):
        # the opening-day trader.py crashed on exactly this (noon-run.log, 2026-10-01)
        hold = os.path.join(self.dir, "hold.txt")
        with open(hold, "w", encoding="utf-8-sig") as fh:
            fh.write("917  # NJ-07\n")
        self.assertEqual(longshot.read_hold(hold), {"917"})
        with open(self.fv_path, "w", encoding="utf-8-sig") as fh:
            json.dump({"markets": {"1": {"label": "x", "manual": 0.4}}}, fh)
        self.assertEqual(FairValues(self.fv_path, log=lambda m: None).get("1")[0], 0.4)
        from sigbot.bot import load_config
        cfg_path = os.path.join(self.dir, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8-sig") as fh:
            json.dump({"take_edge": 0.05}, fh)
        self.assertEqual(load_config(cfg_path)["take_edge"], 0.05)

    def test_hold_file_roundtrip(self):
        path = os.path.join(self.dir, "hold.txt")
        longshot.add_hold("101", "R House", path)
        longshot.add_hold("101", "dup", path)
        self.assertEqual(longshot.read_hold(path), {"101"})

    def test_bot_exit_keeps_resting_order_on_held_exchange(self):
        with open(self.fv_path, "w") as fh:
            json.dump({"markets": {"101": {"label": "a", "manual": 0.5}}}, fh)
        hold = os.path.join(self.dir, "hold.txt")
        longshot.add_hold("917", "NJ-07 longshot", hold)
        cfg = dict(DEFAULTS, fair_values_file=self.fv_path, log_file=os.path.join(self.dir, "log.jsonl"),
                   stop_file=os.path.join(self.dir, "STOP"), hold_file=hold, competition_end_utc="2000-01-01T00:00:00Z")
        c = FakeClient()
        c.resting = [{"id": 1, "exchangeId": "917"}, {"id": 2, "exchangeId": "101"}]
        Bot(cfg, c, live=True).run()   # end date passed: stops at once, then cleans up
        self.assertEqual(c.cancelled_ids, [2])
        self.assertEqual(c.cancelled, 0)   # no blanket cancel-all

    def run_buy(self, argv, book_asks):
        from unittest import mock
        import contextlib, io
        from sigbot import __main__ as cli
        c = FakeClient()
        c.books["917"] = {"bids": [], "asks": [{"price": p, "quantity": q} for p, q in book_asks]}
        c.account = lambda tid=None: {"balance": 7005.09}
        c.place_order = lambda *a: (c.orders.append(a) or
                                    {"orderId": 9, "open": True, "quantityTraded": 2018, "totalCost": 133.72,
                                     "remainingQuantity": a[3] - 2018})
        hold = os.path.join(self.dir, "hold.txt")
        cfg = dict(DEFAULTS, stop_file=os.path.join(self.dir, "STOP"), hold_file=hold, fair_values_file=self.fv_path)
        out = io.StringIO()
        with mock.patch.object(cli, "Client", lambda *a, **k: c), mock.patch.object(cli, "load_config", lambda p: cfg), \
             mock.patch.object(sys, "argv", ["sigbot", "buy"] + argv), contextlib.redirect_stdout(out):
            cli.main()
        return c, out.getvalue(), hold

    def test_buy_rest_places_one_limit_for_whole_budget(self):
        with open(self.fv_path, "w") as fh:
            json.dump({"markets": {"917": {"label": "R NJ-07", "kalshi": "X"}}}, fh)
        argv = ["917", "--side", "yes", "--spend", "7000", "--max-price", "0.07", "--rest", "24"]
        c, out, hold = self.run_buy(argv, [(0.065, 1000), (0.07, 1018)])
        self.assertEqual(c.orders, [])                       # preview places nothing
        self.assertIn("resting at 0.070 for 24h", out)
        c, out, hold = self.run_buy(argv + ["--confirm"], [(0.065, 1000), (0.07, 1018)])
        (ex, side, action, qty, price, tid, expires), = c.orders
        self.assertEqual((ex, side, action, price), ("917", "yes", "buy", 0.07))
        self.assertEqual(qty, 2018 + int((7000 - (1000 * 0.065 + 1018 * 0.07)) / 0.07))
        self.assertIn("resting (order 9)", out)
        self.assertEqual(longshot.read_hold(hold), {"917"})

    def test_bot_takes_profit_first_and_skips_buys_that_cycle(self):
        with open(self.fv_path, "w") as fh:
            json.dump({"markets": {"101": {"label": "a", "manual": 0.70}, "103": {"label": "b", "manual": 0.30}}}, fh)
        hold = os.path.join(self.dir, "hold.txt")
        longshot.add_hold("917", "longshot", hold)
        cfg = dict(DEFAULTS, fair_values_file=self.fv_path, log_file=os.path.join(self.dir, "log.jsonl"),
                   arb_enabled=False, stop_file=os.path.join(self.dir, "STOP"), hold_file=hold,
                   competition_end_utc="2099-01-01T00:00:00Z")
        c = FakeClient()
        # 102 bought at 0.30, now bid 0.48 (+60%); 917 is up even more but is held
        c.books["917"] = {"bids": [{"price": 0.20, "quantity": 9999}], "asks": []}
        positions = [{"exchangeId": "102", "quantity": 1000, "avgCost": 0.30, "marketValue": 480, "settled": False},
                     {"exchangeId": "917", "quantity": 4118, "avgCost": 0.07, "marketValue": 824, "settled": False}]
        c.positions = lambda slug: {"positions": positions, "summary": {"totalMarketValue": 1304}}
        bot = Bot(cfg, c, live=True)
        bot.step()
        self.assertEqual([(o[0], o[1], o[2], o[3]) for o in c.orders], [("102", "yes", "sell", 1000)])
        positions[:] = positions[1:]   # 102 sold; next cycle buys resume
        bot.step()
        self.assertEqual(sorted(o[0] for o in c.orders[1:]), ["101", "103"])
        self.assertNotIn("917", [o[0] for o in c.orders])

    def test_bot_never_trades_held_exchange(self):
        with open(self.fv_path, "w") as fh:
            json.dump({"markets": {"101": {"label": "a", "manual": 0.70}, "103": {"label": "b", "manual": 0.30}}}, fh)
        hold = os.path.join(self.dir, "hold.txt")
        longshot.add_hold("101", "my longshot", hold)
        cfg = dict(DEFAULTS, fair_values_file=self.fv_path, log_file=os.path.join(self.dir, "log.jsonl"),
                   arb_enabled=False, stop_file=os.path.join(self.dir, "STOP"), hold_file=hold,
                   competition_end_utc="2099-01-01T00:00:00Z")
        c = FakeClient()
        Bot(cfg, c, live=True).step()
        self.assertEqual([o[0] for o in c.orders], ["103"])


if __name__ == "__main__":
    unittest.main()
