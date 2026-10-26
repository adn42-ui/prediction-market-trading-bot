"""Pure decision logic: no I/O, so it can be unit-tested.

Conventions (match the API):
  * every price here is YES-normalized in [0, 1]; a NO order's limit price is 1 - yes_price
  * position > 0 means YES shares held, < 0 means NO shares held
  * the engine nets an opposite-side BUY against what you hold, so "buy yes" while short NO
    reduces the NO position first (and credits 1 per netted share)
"""
import math
from dataclasses import dataclass, field

TICK = 0.005


def tick_down(p):
    return round(min(max(math.floor(p / TICK + 1e-9) * TICK, TICK), 1 - TICK), 3)


def tick_up(p):
    return round(min(max(math.ceil(p / TICK - 1e-9) * TICK, TICK), 1 - TICK), 3)


@dataclass
class Intent:
    exchange_id: str
    side: str          # "yes" | "no"
    action: str        # "buy" | "sell"
    quantity: int
    price: float       # side-relative limit price, on the 0.005 tick
    edge: float        # expected value per share vs fair, in probability points
    reason: str
    kind: str = "take"  # "take" | "make" | "exit"
    meta: dict = field(default_factory=dict)

    def cost(self):
        return self.quantity * self.price if self.action == "buy" else 0.0


def kelly_yes(fair, price):
    """Fraction of bankroll to stake buying YES at `price` when YES really has probability `fair`."""
    return max(0.0, (fair - price) / (1 - price)) if price < 1 else 0.0


def walk(levels, ok, max_qty):
    """Take resting levels (best first) while ok(price); returns (qty, worst_price)."""
    qty, worst = 0, None
    for lvl in levels:
        if not ok(lvl["price"]) or qty >= max_qty:
            break
        take = min(int(lvl["quantity"]), max_qty - qty)
        if take <= 0:
            continue
        qty += take
        worst = lvl["price"]
    return qty, worst


def decide_take(ex_id, fair, book, position, equity, cash, cfg):
    """Cross the spread when the book is mispriced by at least cfg.take_edge.

    book: {"bids": [{price, quantity}...] desc, "asks": [...] asc} in YES terms.
    Sizing is fractional Kelly on total equity, capped per exchange in SUSQies.
    """
    bids, asks = book.get("bids") or [], book.get("asks") or []
    intents = []
    cap_value = min(cfg["max_position_value"], cfg["max_position_frac"] * equity)

    # --- buy YES (hit asks below fair) -------------------------------------
    if asks and fair - asks[0]["price"] >= cfg["take_edge"]:
        limit = fair - cfg["take_edge"]
        ask = asks[0]["price"]
        target_val = min(cfg["kelly_mult"] * kelly_yes(fair, ask) * equity, cap_value)
        target = int(target_val / ask)
        want = target - position
        if want > 0:
            qty, worst = walk(asks, lambda p: p <= limit + 1e-9, want)
            qty = _cash_cap(qty, worst, position, cash, cfg, netting_side=position < 0)
            if qty >= cfg["min_qty"]:
                intents.append(Intent(ex_id, "yes", "buy", qty, round(worst, 3), round(fair - ask, 4),
                                      f"ask {ask:.3f} < fair {fair:.3f}"))

    # --- buy NO (hit bids above fair) --------------------------------------
    elif bids and bids[0]["price"] - fair >= cfg["take_edge"]:
        limit = fair + cfg["take_edge"]
        bid = bids[0]["price"]
        no_price = 1 - bid
        target_val = min(cfg["kelly_mult"] * kelly_yes(1 - fair, no_price) * equity, cap_value)
        target = -int(target_val / no_price)
        want = position - target
        if want > 0:
            qty, worst = walk(bids, lambda p: p >= limit - 1e-9, want)
            if worst is not None:
                qty = _cash_cap(qty, 1 - worst, -position, cash, cfg, netting_side=position > 0)
                if qty >= cfg["min_qty"]:
                    intents.append(Intent(ex_id, "no", "buy", qty, round(1 - worst, 3), round(bid - fair, 4),
                                          f"bid {bid:.3f} > fair {fair:.3f}"))

    # --- exit: position no longer has edge (exit_mode "fair" only) ----------
    elif cfg.get("exit_mode", "fair") != "fair":
        pass   # "profit" mode sells through decide_take_profit instead
    elif position > 0 and bids and bids[0]["price"] - fair >= cfg["exit_edge"]:
        qty, worst = walk(bids, lambda p: p >= fair + cfg["exit_edge"] - 1e-9, position)
        if qty >= cfg["min_qty"]:
            intents.append(Intent(ex_id, "yes", "sell", qty, round(worst, 3), round(bids[0]["price"] - fair, 4),
                                  f"exit long: bid {bids[0]['price']:.3f} >= fair {fair:.3f}", kind="exit"))
    elif position < 0 and asks and fair - asks[0]["price"] >= cfg["exit_edge"]:
        qty, worst = walk(asks, lambda p: p <= fair - cfg["exit_edge"] + 1e-9, -position)
        if qty >= cfg["min_qty"]:
            intents.append(Intent(ex_id, "no", "sell", qty, round(1 - worst, 3), round(fair - asks[0]["price"], 4),
                                  f"exit short: ask {asks[0]['price']:.3f} <= fair {fair:.3f}", kind="exit"))
    return intents


def decide_take_profit(ex_id, quantity, avg_cost, book, cfg):
    """Sell a position whose sale price is at least take_profit_pct above what was paid.

    quantity > 0 is YES shares (sold into YES bids); < 0 is NO shares (sold at 1 - YES ask).
    avg_cost is side-relative, as GET .../portfolio/positions reports it. Only levels at or
    above the target are taken, so a thin book sells part now and the rest later.
    """
    target = avg_cost * (1 + cfg["take_profit_pct"])
    if target >= 1 - TICK:   # e.g. a 0.90 favorite can't gain 25%: it is held to resolution
        return []
    if quantity > 0:
        qty, worst = walk(book.get("bids") or [], lambda p: p >= target - 1e-9, int(quantity))
        side, price = "yes", worst
    else:
        qty, worst = walk(book.get("asks") or [], lambda p: 1 - p >= target - 1e-9, int(-quantity))
        side, price = "no", None if worst is None else round(1 - worst, 3)
    if qty < cfg["min_qty"] or price is None:
        return []
    gain = price / avg_cost - 1
    return [Intent(ex_id, side, "sell", qty, round(price, 3), round(price - avg_cost, 4),
                   f"take profit: bought {avg_cost:.3f}, selling {price:.3f} (+{gain:.0%})", kind="exit")]


def _cash_cap(qty, price, held_same_sign, cash, cfg, netting_side):
    """Shares that net against an opposite position free up cash, so only the rest needs paying for."""
    if qty <= 0 or price is None:
        return 0
    netted = min(qty, abs(held_same_sign)) if netting_side else 0
    budget = max(0.0, cash * cfg["max_cash_per_order_frac"])
    payable = int(budget / price) if price > 0 else 0
    return min(qty, netted + payable)


def decide_make(ex_id, fair, best_bid, best_ask, position, equity, open_orders, cfg):
    """Post resting quotes around fair. Quotes expire on their own (cfg.quote_ttl_s), so no cancels are needed.

    open_orders: this exchange's resting orders from GET /orders.
    """
    hs = cfg["make_half_spread"]
    size = cfg["make_size"]
    max_shares = cfg["max_position_value"] / max(fair, 1 - fair)
    want_bid = tick_down(fair - hs)
    want_ask = tick_up(fair + hs)
    if best_ask is not None:
        want_bid = min(want_bid, round(best_ask - TICK, 3))   # never cross: that is the taker's job
    if best_bid is not None:
        want_ask = max(want_ask, round(best_bid + TICK, 3))
    intents = []

    def resting(side, price):
        for o in open_orders:
            if o["side"] == side and o["action"] == "buy" and o.get("priceLimit") is not None:
                if abs(o["priceLimit"] - price) < cfg["requote_threshold"] + 1e-9:
                    return True
        return False

    # YES bid (we buy YES) — skip if already long enough
    if position < max_shares and fair - want_bid >= cfg["make_min_edge"] and not resting("yes", want_bid):
        intents.append(Intent(ex_id, "yes", "buy", size, want_bid, round(fair - want_bid, 4),
                              f"quote bid {want_bid:.3f} (fair {fair:.3f})", kind="make"))
    # YES ask == NO bid at 1 - ask
    no_px = round(1 - want_ask, 3)
    if -position < max_shares and want_ask - fair >= cfg["make_min_edge"] and not resting("no", no_px):
        intents.append(Intent(ex_id, "no", "buy", size, no_px, round(want_ask - fair, 4),
                              f"quote ask {want_ask:.3f} (fair {fair:.3f})", kind="make"))
    return intents


def decide_arb(market, cash, cfg):
    """Mutually exclusive multi-outcome market whose YES asks sum below 1: buy one of each, all-or-none."""
    if not market.get("isMultiOutcome") or not market.get("hasArbitrageOpportunity"):
        return None
    legs, total, sizes = [], 0.0, []
    for ex in market["exchanges"]:
        asks = ex.get("asks") or []
        if not asks:
            return None
        legs.append({"exchangeId": str(ex["id"]), "side": "yes", "action": "buy", "price": asks[0]["price"]})
        total += asks[0]["price"]
        sizes.append(int(asks[0]["quantity"]))
    edge = 1 - total
    if edge < cfg["arb_min_edge"]:
        return None
    qty = min(min(sizes), int(cash * cfg["max_cash_per_order_frac"] / total), cfg["arb_max_qty"])
    if qty < cfg["min_qty"]:
        return None
    for leg in legs:
        leg["quantity"] = qty
    return {"legs": legs, "edge": round(edge, 4), "qty": qty, "cost": round(qty * total, 2)}
