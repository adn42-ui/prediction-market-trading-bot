import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sigbot import strategy as s  # noqa: E402
from sigbot.bot import DEFAULTS  # noqa: E402

CFG = dict(DEFAULTS)
FAIR_EXITS = dict(DEFAULTS, exit_mode="fair")


def book(bids=(), asks=()):
    return {"bids": [{"price": p, "quantity": q} for p, q in bids],
            "asks": [{"price": p, "quantity": q} for p, q in asks]}


class Ticks(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(s.tick_down(0.6249), 0.62)
        self.assertEqual(s.tick_up(0.6201), 0.625)
        self.assertEqual(s.tick_down(0.0), 0.005)
        self.assertEqual(s.tick_up(1.2), 0.995)


class Take(unittest.TestCase):
    def test_no_trade_inside_edge(self):
        self.assertEqual(s.decide_take("1", 0.60, book([(0.59, 500)], [(0.61, 500)]), 0, 100000, 100000, CFG), [])

    def test_buy_yes_when_ask_cheap(self):
        out = s.decide_take("1", 0.70, book([(0.55, 500)], [(0.60, 300), (0.65, 10000), (0.68, 10000)]),
                            0, 100000, 100000, CFG)
        self.assertEqual(len(out), 1)
        it = out[0]
        self.assertEqual((it.side, it.action), ("yes", "buy"))
        # only levels <= fair - take_edge (0.67) are taken
        self.assertEqual(it.price, 0.65)
        # half-Kelly at 0.60 with fair 0.70: 0.5 * 0.25 = 12.5% of 100k = 12.5k -> 20833 shares; capped by 15k/0.6=25000
        self.assertEqual(it.quantity, 10300)

    def test_buy_no_when_bid_rich(self):
        out = s.decide_take("1", 0.30, book([(0.40, 1000)], [(0.45, 1000)]), 0, 100000, 100000, CFG)
        it = out[0]
        self.assertEqual((it.side, it.action, it.price), ("no", "buy", 0.6))
        self.assertEqual(it.quantity, 1000)

    def test_respects_existing_position(self):
        # target ~20833 shares; already hold 25000 -> nothing more
        out = s.decide_take("1", 0.70, book([], [(0.60, 99999)]), 25000, 100000, 100000, CFG)
        self.assertEqual(out, [])

    def test_position_cap(self):
        out = s.decide_take("1", 0.95, book([], [(0.10, 10**6)]), 0, 100000, 100000, CFG)
        # 15000 SUSQies cap / 0.10 = 150000 shares, but cash cap 30% of 100k / 0.10 = 300000; Kelly huge
        self.assertEqual(out[0].quantity, 150000)

    def test_cash_cap(self):
        out = s.decide_take("1", 0.70, book([], [(0.60, 10**6)]), 0, 100000, 1000, CFG)
        self.assertEqual(out[0].quantity, int(1000 * 0.30 / 0.60))

    def test_exit_long_at_fair(self):
        out = s.decide_take("1", 0.50, book([(0.51, 400)], [(0.53, 400)]), 300, 100000, 100000, FAIR_EXITS)
        it = out[0]
        self.assertEqual((it.side, it.action, it.kind, it.quantity, it.price), ("yes", "sell", "exit", 300, 0.51))

    def test_exit_short(self):
        out = s.decide_take("1", 0.50, book([(0.47, 400)], [(0.49, 400)]), -250, 100000, 100000, FAIR_EXITS)
        it = out[0]
        self.assertEqual((it.side, it.action, it.kind, it.quantity, it.price), ("no", "sell", "exit", 250, 0.51))

    def test_flip_from_short_nets_without_cash(self):
        # short 1000 NO, no cash: buying YES nets those shares, so it is still allowed up to 1000
        out = s.decide_take("1", 0.70, book([], [(0.60, 5000)]), -1000, 100000, 0, CFG)
        self.assertEqual(out[0].quantity, 1000)


class TakeProfit(unittest.TestCase):
    def test_profit_mode_never_sells_at_fair(self):
        # same book as test_exit_long_at_fair: in profit mode reaching fair is not a reason to sell
        self.assertEqual(s.decide_take("1", 0.50, book([(0.51, 400)], [(0.53, 400)]), 300, 100000, 100000, CFG), [])

    def test_sells_yes_only_at_or_above_target(self):
        # bought at 0.70; +25% target is 0.875. Bids: 500 at 0.90, 300 at 0.88, 1000 at 0.85
        it, = s.decide_take_profit("1", 1200, 0.70, book([(0.90, 500), (0.88, 300), (0.85, 1000)]), CFG)
        self.assertEqual((it.side, it.action, it.kind, it.quantity, it.price), ("yes", "sell", "exit", 800, 0.88))
        self.assertIn("+26%", it.reason)

    def test_sells_no_position_against_yes_asks(self):
        # NO bought at 0.70 (CO-04 style); YES ask 0.10 -> NO sells at 0.90, above 0.875
        it, = s.decide_take_profit("1", -2000, 0.70, book(asks=[(0.10, 1500), (0.20, 5000)]), CFG)
        self.assertEqual((it.side, it.action, it.quantity, it.price), ("no", "sell", 1500, 0.9))

    def test_holds_when_not_up_enough(self):
        self.assertEqual(s.decide_take_profit("1", 1000, 0.70, book([(0.85, 5000)]), CFG), [])

    def test_favorites_held_to_resolution(self):
        # bought at 0.896: +25% would be 1.12, impossible -> never sold early
        self.assertEqual(s.decide_take_profit("1", 1000, 0.896, book([(0.99, 5000)]), CFG), [])


class Make(unittest.TestCase):
    def test_quotes_both_sides(self):
        cfg = dict(CFG, make_half_spread=0.03, make_min_edge=0.015, make_size=100)
        out = s.decide_make("1", 0.50, 0.40, 0.60, 0, 100000, [], cfg)
        self.assertEqual([(i.side, i.price) for i in out], [("yes", 0.47), ("no", 0.47)])

    def test_never_crosses(self):
        cfg = dict(CFG, make_half_spread=0.03, make_min_edge=0.0)
        out = s.decide_make("1", 0.50, 0.40, 0.46, 0, 100000, [], cfg)
        self.assertEqual(out[0].price, 0.455)

    def test_skips_existing_quote(self):
        cfg = dict(CFG, make_half_spread=0.03, make_min_edge=0.015)
        mine = [{"side": "yes", "action": "buy", "priceLimit": 0.47}]
        out = s.decide_make("1", 0.50, 0.40, 0.60, 0, 100000, mine, cfg)
        self.assertEqual([i.side for i in out], ["no"])


class Arb(unittest.TestCase):
    def market(self, asks, flag=True):
        return {"isMultiOutcome": True, "hasArbitrageOpportunity": flag,
                "exchanges": [{"id": str(i), "asks": [{"price": p, "quantity": q}]} for i, (p, q) in enumerate(asks)]}

    def test_finds_arb(self):
        arb = s.decide_arb(self.market([(0.30, 500), (0.30, 800), (0.35, 900)]), 100000, CFG)
        self.assertEqual(arb["qty"], 500)
        self.assertAlmostEqual(arb["edge"], 0.05)

    def test_ignores_without_flag(self):
        self.assertIsNone(s.decide_arb(self.market([(0.30, 500), (0.30, 500)], flag=False), 100000, CFG))

    def test_ignores_no_edge(self):
        self.assertIsNone(s.decide_arb(self.market([(0.50, 500), (0.50, 500)]), 100000, CFG))


if __name__ == "__main__":
    unittest.main()
