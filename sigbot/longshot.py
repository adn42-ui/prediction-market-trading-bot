"""High-payout bets: find cheap contracts the market underprices, and size a one-off buy.

Pure functions; __main__ wires them to the API. A contract bought at price p pays 1/p times the
stake if it wins. That only beats the field on average when the real chance is above p, so
contracts are ranked by fair / price (>1 means SIG sells it below what real-money markets say).
"""
import os

HOLD_FILE = "hold.txt"


def rank_longshots(fv, quotes, max_price, min_fair=0.02):
    """[(value_ratio, exchange_id, side, price, fair_p, sources)] for every side priced <= max_price.

    Sides whose fair chance is below min_fair are skipped: fair values are clamped at 0.01, so a
    0.3% Kalshi contract would otherwise look like a bargain at the 0.005 minimum tick.
    """
    rows = []
    for ex_id, q in quotes.items():
        fair, srcs = fv.get(ex_id)
        if fair is None:
            continue
        lo, hi = fv.bounds(ex_id, fair, srcs)   # model-only: judge YES at low end, NO at high end
        ask, bid = q.get("bestAsk"), q.get("bestBid")
        if ask is not None and 0 < ask <= max_price and lo >= min_fair:
            rows.append((lo / ask, ex_id, "yes", ask, lo, srcs))
        if bid is not None and 0 < 1 - bid <= max_price and 1 - hi >= min_fair:
            rows.append(((1 - hi) / (1 - bid), ex_id, "no", round(1 - bid, 3), 1 - hi, srcs))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def plan_buy(book, side, spend, max_price):
    """Walk the book for `side` up to max_price until `spend` is used.

    Returns {qty, cost, avg, worst, payout} in side-relative prices, or None if nothing is
    offered at or below max_price. YES buys take asks; NO buys take YES bids at 1 - price.
    """
    if side == "yes":
        levels = [(lvl["price"], int(lvl["quantity"])) for lvl in book.get("asks") or []]
    else:
        levels = [(round(1 - lvl["price"], 3), int(lvl["quantity"])) for lvl in book.get("bids") or []]
    qty, cost, worst = 0, 0.0, None
    for price, size in levels:
        if price > max_price + 1e-9 or cost >= spend:
            break
        take = min(size, int((spend - cost) / price))
        if take <= 0:
            break
        qty += take
        cost += take * price
        worst = price
    if not qty:
        return None
    return {"qty": qty, "cost": round(cost, 2), "avg": round(cost / qty, 4), "worst": worst, "payout": qty}


def read_hold(path=HOLD_FILE):
    """Exchange ids the bot must never trade (one per line, # comments allowed)."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig") as fh:   # tolerate a BOM if edited with PowerShell
        return {line.split("#")[0].strip() for line in fh if line.split("#")[0].strip()}


def add_hold(exchange_id, label="", path=HOLD_FILE):
    if str(exchange_id) in read_hold(path):
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{exchange_id}  # {label}\n")
