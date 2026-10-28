"""
Usage:
  python -m sigbot check                 verify key, list tournaments and balance
  python -m sigbot scan                  list every market + quotes, write fair_values.template.json
  python -m sigbot fair                  show fair value vs book for every mapped exchange (no trading)
  python -m sigbot run [--live]          trade loop (dry-run unless --live)
  python -m sigbot status                positions, P&L, leaderboard rank
  python -m sigbot cancel-all            cancel every resting order in the tournament
  python -m sigbot longshots [--max-price 0.12]
                                         cheap high-payout contracts ranked by fair/price
  python -m sigbot buy EXCHANGE_ID --side yes|no --spend 7000 --max-price 0.08 [--confirm]
                                         one-off buy: preview, then --confirm places it and
                                         adds it to hold.txt so the bot never sells it
Options: --config sigbot-config.json
"""
import argparse
import json
import os
import sys

from .bot import Bot, iso_in, load_api_key, load_config, resolve_tournament
from .client import ApiError, Client
from .fair import FairValues
from .longshot import add_hold, plan_buy, rank_longshots


def main():
    ap = argparse.ArgumentParser(prog="sigbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "scan", "fair", "run", "status", "cancel-all", "longshots", "buy"])
    ap.add_argument("exchange_id", nargs="?", help="buy: exchange to buy")
    ap.add_argument("--config", default="sigbot-config.json")
    ap.add_argument("--live", action="store_true", help="actually place orders")
    ap.add_argument("--max-price", type=float, default=None, help="longshots/buy: highest price per share")
    ap.add_argument("--side", choices=["yes", "no"], help="buy: which side")
    ap.add_argument("--spend", type=float, help="buy: SUSQies to spend")
    ap.add_argument("--confirm", action="store_true", help="buy: actually place the order")
    ap.add_argument("--at", type=int, nargs="+", metavar="RANK",
                    help="status: also show the P&L at these ranks, e.g. --at 10 50 100 250")
    ap.add_argument("--rest", type=float, default=0, metavar="HOURS",
                    help="buy: leave the unfilled part resting at --max-price for HOURS, waiting for sellers")
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        client = Client(load_api_key(), cfg["base_url"], cfg["reads_per_min"], cfg["writes_per_min"])
    except ValueError as e:
        sys.exit(str(e))

    try:
        if args.command == "check":
            acct = client.account()
            print(f"Key OK. User: {acct.get('username')}  default-scope balance: {acct.get('balance')}")
            for t in client.tournaments(status="any"):
                print(f"  [{t['status']:6}] {t['name']}  slug={t['slug']}  id={t['id']}  "
                      f"balance={t['myBalance']:,.2f} {t['currencyName']}  ends={t['endDate']}"
                      f"{'  (not enrolled yet: first order enrols you)' if t['isPendingEnrolment'] else ''}")

        elif args.command == "scan":
            slug, tid, _ = resolve_tournament(client, cfg["tournament_slug"])
            markets = client.tournament_markets(slug, status="open")
            ids = [str(ex["id"]) for m in markets for ex in m["exchanges"]]
            quotes = client.prices(ids, tid) if ids else {}
            template = {"_help": "Fill kalshi / polymarket / manual for the exchanges you want traded; delete the rest.",
                        "markets": {}}
            for m in markets:
                print(f"\n#{m['id']} {m['title']}  [{', '.join(m['categories'])}]"
                      f"{'  multi-outcome' if m['isMultiOutcome'] else ''}  settles {m['settlementDate']}")
                for ex in m["exchanges"]:
                    q = quotes.get(str(ex["id"]), {})
                    print(f"    exchange {ex['id']:>8}  {str(ex['option'] or 'YES'):28} "
                          f"bid {fmt(q.get('bestBid'))}  ask {fmt(q.get('bestAsk'))}  last {fmt(q.get('latestPrice'))}")
                    template["markets"][str(ex["id"])] = {
                        "label": f"{m['title']}{' / ' + ex['option'] if ex['option'] else ''}",
                        "kalshi": "", "polymarket": "", "polymarket_outcome": None, "manual": None,
                        "_bid": q.get("bestBid"), "_ask": q.get("bestAsk")}
            with open("fair_values.template.json", "w", encoding="utf-8") as fh:
                json.dump(template, fh, indent=2)
            print(f"\n{len(markets)} markets, {len(ids)} exchanges. Wrote fair_values.template.json")

        elif args.command == "fair":
            slug, tid, _ = resolve_tournament(client, cfg["tournament_slug"])
            fv = FairValues(cfg["fair_values_file"], max_spread=cfg["fair_max_spread"],
                            max_model_age_hours=cfg["max_model_age_hours"], max_model_gap=cfg["max_model_gap"])
            ids = [k for k in fv.entries if fv.get(k)[0] is not None]
            quotes = client.prices(ids, tid) if ids else {}
            rows = []
            for k in ids:
                fair, srcs = fv.get(k)
                q = quotes.get(k, {})
                b, a = q.get("bestBid"), q.get("bestAsk")
                lo, hi = fv.bounds(k, fair, srcs)
                edge = max(lo - a if a is not None else -1, b - hi if b is not None else -1)
                rows.append((edge, k, fair, srcs, b, a))
            for edge, k, fair, srcs, b, a in sorted(rows, reverse=True):
                src = " ".join(f"{s}={v:.3f}" for s, v in srcs.items())
                flag = "  <== TRADE" if edge >= fv.entry(k).get("take_edge", cfg["take_edge"]) else ""
                print(f"{k:>8}  fair {fair:.3f}  bid {fmt(b)}  ask {fmt(a)}  edge {edge:+.3f}  "
                      f"{fv.entries[k].get('label', '')[:50]}  ({src}){flag}")

        elif args.command == "run":
            Bot(cfg, client, live=args.live).run()

        elif args.command == "status":
            slug, tid, t = resolve_tournament(client, cfg["tournament_slug"])
            pnl = client.pnl(slug)
            print(f"{t['name']}: account value {pnl['totalAccountValue']:,.2f}  ROI {pnl['roi']}%  "
                  f"unrealized {pnl['unrealizedPnl']:,.2f}")
            for p in client.positions(slug)["positions"]:
                if p["quantity"]:
                    side = "YES" if p["quantity"] > 0 else "NO"
                    print(f"  {abs(p['quantity']):>7.0f} {side:3} @ {p['avgCost']:.3f} -> {fmt(p['currentPrice'])}  "
                          f"P&L {p['unrealizedPnl']:+,.0f}  {p['marketTitle'][:50]} {p['option'] or ''}")
            lb = client.leaderboard(slug, limit=5)
            print(f"My rank: {lb['myRank']} of {lb['total']}  (ranked by P&L)")
            for e in lb["leaderboard"]:
                print(f"  #{e.get('rank')} {e.get('username')}  {e.get('pnl', e.get('finalTotalValue'))}")
            # P&L needed to reach each milestone rank
            for rank in args.at or []:
                row = client.leaderboard(slug, limit=1, offset=rank - 1)["leaderboard"]
                if row:
                    print(f"  rank {rank:>4}: P&L {row[0].get('pnl', row[0].get('finalTotalValue')):>12,.0f}")

        elif args.command == "cancel-all":
            slug, tid, _ = resolve_tournament(client, cfg["tournament_slug"])
            print(client.cancel_all(tid))

        elif args.command == "longshots":
            slug, tid, _ = resolve_tournament(client, cfg["tournament_slug"])
            fv = FairValues(cfg["fair_values_file"], max_spread=cfg["fair_max_spread"],
                            max_model_age_hours=cfg["max_model_age_hours"], max_model_gap=cfg["max_model_gap"],
                            log=lambda m: None)
            quotes = client.prices(list(fv.entries), tid)
            rows = rank_longshots(fv, quotes, args.max_price or 0.12)
            print(f"{'exch':>6} {'side':4} {'price':>6} {'pays':>6} {'fair':>6} {'value':>6}  market  (sources)")
            for ratio, k, side, price, p, srcs in rows[:25]:
                src = " ".join(f"{s}={v:.3f}" for s, v in srcs.items())
                print(f"{k:>6} {side.upper():4} {price:6.3f} {1 / price:5.1f}x {p:6.3f} {ratio:6.2f}  "
                      f"{fv.entry(k).get('label', '')[:55]}  ({src})")
            print("value > 1: SIG sells it below the fair chance (good longshot). < 1: overpriced lottery ticket.")

        elif args.command == "buy":
            if not (args.exchange_id and args.side and args.spend and args.max_price):
                sys.exit("buy needs EXCHANGE_ID --side yes|no --spend N --max-price P")
            slug, tid, _ = resolve_tournament(client, cfg["tournament_slug"])
            book = client.orderbook(args.exchange_id, tid, depth=100)
            cash = client.account(tid)["balance"] or 0.0
            held = {p["exchangeId"]: p for p in client.positions(slug)["positions"] if p["quantity"]}
            budget = min(args.spend, cash)
            plan = plan_buy(book, args.side, budget, args.max_price)
            labels = {}
            if os.path.exists(cfg["fair_values_file"]):
                with open(cfg["fair_values_file"], encoding="utf-8-sig") as fh:
                    labels = {k: v.get("label") for k, v in json.load(fh).get("markets", {}).items()}
            label = labels.get(args.exchange_id) or f"exchange {args.exchange_id}"
            if budget < args.max_price:
                sys.exit(f"Not enough cash: you have {cash:,.2f}, and one share costs up to {args.max_price}. "
                         f"Sell something first, then retry.")
            if not plan and not args.rest:
                best =(book.get("asks") or [{}])[0].get("price") if args.side == "yes" else \
                    (round(1 - book["bids"][0]["price"], 3) if book.get("bids") else None)
                sys.exit(f"Nothing offered on {args.side.upper()} at or below {args.max_price} for {label}"
                         f"{f' (cheapest offer now: {best})' if best is not None else ' (no offers at all)'}. "
                         f"Add --rest HOURS to leave a buy order waiting at {args.max_price}.")
            plan = plan or {"qty": 0, "cost": 0.0, "avg": 0.0, "worst": args.max_price, "payout": 0}
            print(f"{label}\n  now: buy {plan['qty']:,} {args.side.upper()} for {plan['cost']:,.2f} "
                  f"(avg {plan['avg']:.4f}, worst {plan['worst']:.3f})  cash {cash:,.2f}")
            rest_qty = 0
            if args.rest:
                # one limit order for the whole budget at max-price: crosses what is offered now,
                # the remainder rests until sellers arrive or it expires
                rest_qty = int((budget - plan["cost"]) / args.max_price)
                print(f"  then: {rest_qty:,} more resting at {args.max_price:.3f} for {args.rest:g}h "
                      f"(fills only if sellers come; cash is not reserved)")
            total = plan["qty"] + rest_qty
            cost = plan["cost"] + rest_qty * args.max_price
            print(f"  pays {total:,} SUSQies if it wins ({total / cost:.1f}x), 0 if it loses"
                  + ("  [if fully filled]" if rest_qty else ""))
            if args.exchange_id in held:
                q = held[args.exchange_id]["quantity"]
                if (q > 0) != (args.side == "yes"):
                    print(f"  WARNING: you hold {abs(q):.0f} of the other side; this nets against it first.")
            if not args.confirm:
                print("Preview only. Add --confirm to place it.")
                return
            if cfg["stop_file"] and os.path.exists(cfg["stop_file"]):
                sys.exit("STOP file present: not placing the order.")
            if rest_qty:
                res = client.place_order(args.exchange_id, args.side, "buy", total, args.max_price, tid,
                                         iso_in(int(args.rest * 3600)))
            else:
                res = client.place_order(args.exchange_id, args.side, "buy", plan["qty"], plan["worst"], tid, iso_in(30))
            print(f"  filled {res.get('quantityTraded', 0):,} for {res.get('totalCost', 0):,.2f}"
                  + (f"; {abs(res.get('remainingQuantity') or 0):,.0f} resting (order {res.get('orderId')})"   # NO side is signed negative
                     if res.get("open") and rest_qty else "  (rest expires in 30s)" if res.get("open") else ""))
            if res.get("quantityTraded") or (rest_qty and res.get("open")):
                add_hold(args.exchange_id, label, cfg["hold_file"])
                print(f"  added {args.exchange_id} to {cfg['hold_file']}: the bot will not trade it.")
    except ApiError as e:
        hint = {
            "FORBIDDEN": "Confirm your email and register for the tournament on the site.",
            "TERMS_NOT_ACKNOWLEDGED": "Accept the current Terms on the site.",
            "INSUFFICIENT_SCOPES": "Create a key with both 'read' and 'trade' scopes.",
            "INVALID_API_KEY": "Check SUPER_MARKET_API_KEY.",
        }.get(e.code, "")
        sys.exit(f"API error: {e}  {hint}")


def fmt(x):
    return f"{x:.3f}" if isinstance(x, (int, float)) else "  -  "


if __name__ == "__main__":
    main()
