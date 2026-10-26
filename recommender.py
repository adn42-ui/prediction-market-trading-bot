#!/usr/bin/env python3
"""Read-only 2026 Susquehanna Predictions Cup recommendation report."""
from __future__ import annotations
import json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path

BASE = os.getenv("SUPER_MARKET_BASE_URL", "https://sig.thesuper.market/api/v1").rstrip("/")
KEY = os.getenv("SUPER_MARKET_API_KEY", "")
TOURNAMENT_SLUG = os.getenv("SUPER_MARKET_TOURNAMENT", "midterm-elections")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.05"))
COST_BPS = float(os.getenv("TRADING_COST_BPS", "0"))
MAX_EVIDENCE_AGE_DAYS = int(os.getenv("MAX_EVIDENCE_AGE_DAYS", "21"))
ROOT = Path(__file__).resolve().parent

def get(path: str) -> dict:
    if not KEY:
        raise SystemExit("Missing SUPER_MARKET_API_KEY. Create a read-scope key in Settings → API Keys and set it in your environment.")
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json", "User-Agent": "sig-cup-readonly-recommender/1.0"}, method="GET")
    # Deliberate hard guard: this program never places, changes, or cancels orders.
    assert req.method == "GET"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code not in (429, 500, 502, 503, 504) or attempt == 1:
                raise RuntimeError(f"API GET {path} failed ({e.code}): {body[:500]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 1:
                raise RuntimeError(f"API GET {path} timed out or failed after 2 attempts") from e
        print(f"Retrying slow read-only API request: {path}", file=sys.stderr, flush=True)
        time.sleep(2)
    raise RuntimeError(f"API GET {path} failed")

def paginate_markets() -> list[dict]:
    tournament_response = get(f"/tournaments/{TOURNAMENT_SLUG}")
    tournament = tournament_response.get("data", tournament_response)
    data, cursor = [], None
    while True:
        q = {"limit": "20", "status": "open"}
        if cursor: q["cursor"] = cursor
        print(f"Loading market page ({len(data)} loaded)...", file=sys.stderr, flush=True)
        page = get(f"/tournaments/{TOURNAMENT_SLUG}/markets?" + urllib.parse.urlencode(q))
        rows = page.get("data", page.get("markets", []))
        if not isinstance(rows, list): raise ValueError("Markets response has no list in data/markets")
        for market in rows:
            market["_tournament"] = tournament
        data.extend(rows)
        p = page.get("pagination", {})
        cursor = p.get("nextCursor") or page.get("nextCursor")
        if not (p.get("hasMore") or cursor): break
    return data

def tournament_context(m: dict) -> dict | None:
    if m.get("_tournament"):
        return {"tournament": m["_tournament"], "exchanges": m.get("exchanges", [])}
    for c in m.get("contexts", []):
        t = c.get("tournament") or {}
        if t.get("slug") == TOURNAMENT_SLUG: return c
    # Organization-bound keys may return a single already-scoped market.
    return m.get("contexts", [None])[0] if m.get("contexts") else None

def exchange_id(m: dict, ctx: dict | None) -> int | None:
    candidates = (ctx or {}).get("exchanges") or m.get("exchanges") or []
    if not candidates: return None
    yes = next((x for x in candidates if str(x.get("option", x.get("settlementOption", ""))).upper() == "YES"), candidates[0])
    return yes.get("id")

def levels(book: dict, *names: str) -> list:
    for n in names:
        v = book.get(n)
        if isinstance(v, list): return v
    return []

def px(level) -> float | None:
    if isinstance(level, (list, tuple)) and level: return float(level[0])
    if isinstance(level, dict):
        for k in ("price", "px", "limitPrice"):
            if level.get(k) is not None: return float(level[k])
    return None

def qty(level) -> float | None:
    if isinstance(level, (list, tuple)) and len(level) > 1: return float(level[1])
    if isinstance(level, dict):
        for k in ("quantity", "qty", "size", "shares"):
            if level.get(k) is not None: return float(level[k])
    return None

def executable_prices(book: dict) -> tuple[float | None, float | None, float | None, float | None]:
    """Return yes ask, no ask, yes bid, no bid; tolerate documented schema aliases."""
    yes_asks = levels(book, "yesAsks", "asks", "sellYes")
    yes_bids = levels(book, "yesBids", "bids", "buyYes")
    no_asks = levels(book, "noAsks", "sellNo")
    no_bids = levels(book, "noBids", "buyNo")
    ya = px(yes_asks[0]) if yes_asks else None
    yb = px(yes_bids[0]) if yes_bids else None
    na = px(no_asks[0]) if no_asks else (round(1 - yb, 10) if yb is not None else None)
    nb = px(no_bids[0]) if no_bids else (round(1 - ya, 10) if ya is not None else None)
    return ya, na, yb, nb

@dataclass
class Estimate:
    p_yes: float
    confidence: str
    freshness: str
    reason: str
    assumptions: list[str]
    sources: list[str]

def load_evidence() -> dict[str, Estimate]:
    raw = json.loads((ROOT / "evidence.json").read_text(encoding="utf-8"))
    return {k: Estimate(**v) for k, v in raw["markets"].items()}

def market_title(m: dict) -> str:
    return str(m.get("title") or m.get("question") or "Untitled market").strip()

def info_description(detail: dict) -> str:
    market = detail.get("data", detail.get("market", detail))
    root = market.get("rootNode", market.get("root_node", {})) or {}
    cd = root.get("contractDetails", root.get("contract_details", {})) or {}
    return str(cd.get("description") or market.get("description") or "")

def recommend(title: str, est: Estimate | None, ya: float | None, na: float | None) -> dict:
    if not est:
        return {"side":"NO TRADE","candidate_side":None,"available_price":None,"estimated_probability":None,"edge":None,"confidence":"low","reason":"No transparent evidence mapping; market cannot be modeled reliably."}
    try: age=(date.today()-date.fromisoformat(est.freshness[:10])).days
    except ValueError: age=10**6
    if age > MAX_EVIDENCE_AGE_DAYS:
        return {"side":"NO TRADE","candidate_side":None,"available_price":None,"estimated_probability":est.p_yes,"edge":None,"confidence":"low","reason":f"Evidence is stale ({age} days old)."}
    costs = COST_BPS / 10_000
    choices = []
    if ya is not None: choices.append((est.p_yes - ya - costs, "YES", ya, est.p_yes))
    if na is not None: choices.append(((1-est.p_yes) - na - costs, "NO", na, 1-est.p_yes))
    if not choices:
        return {"side":"NO TRADE","candidate_side":None,"available_price":None,"estimated_probability":est.p_yes,"edge":None,"confidence":est.confidence,"reason":"No executable ask is available in the order book."}
    edge, side, price, pside = max(choices)
    if est.confidence == "low" or edge < MIN_EDGE:
        return {"side":"NO TRADE","candidate_side":side,"available_price":price,"estimated_probability":pside,"edge":edge,"confidence":est.confidence,"reason":f"Best modeled edge is below {MIN_EDGE:.1%} or confidence is low. {est.reason}"}
    return {"side":side,"candidate_side":side,"available_price":price,"estimated_probability":pside,"edge":edge,"confidence":est.confidence,"reason":est.reason}

def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    evidence = load_evidence()
    markets = paginate_markets()
    rows, failures = [], []
    for m in markets:
        ctx = tournament_context(m)
        if not ctx: continue
        title = market_title(m); mid = m.get("id"); xid = exchange_id(m, ctx)
        tid = (ctx.get("tournament") or {}).get("id")
        try:
            tq = "?" + urllib.parse.urlencode({"tournamentId": tid}) if tid else ""
            detail = get(f"/markets/{mid}{tq}")
            book = get(f"/exchanges/{xid}/orderbook{tq}") if xid else {}
            ya, na, yb, nb = executable_prices(book)
            rec = recommend(title, evidence.get(title), ya, na)
            est = evidence.get(title)
            asks=levels(book,"yesAsks","asks","sellYes"); noasks=levels(book,"noAsks","sellNo")
            ya_qty=qty(asks[0]) if asks else None; na_qty=qty(noasks[0]) if noasks else (qty(levels(book,"yesBids","bids","buyYes")[0]) if levels(book,"yesBids","bids","buyYes") else None)
            rows.append({"market_id":mid,"exchange_id":xid,"market":title,"resolution_criteria":info_description(detail),"yes_ask":ya,"no_ask":na,"yes_bid":yb,"no_bid":nb,"yes_ask_quantity":ya_qty,"no_ask_quantity":na_qty,**rec,"data_freshness": now,"assumptions":est.assumptions if est else [],"sources":est.sources if est else []})
        except Exception as e:
            failures.append({"market":title,"error":str(e)})
    rows.sort(key=lambda r: (r["side"]=="NO TRADE", -(r["edge"] if r["edge"] is not None else -99)))
    out={"generated_at":now,"read_only":True,"minimum_edge":MIN_EDGE,"cost_bps":COST_BPS,"markets_seen":len(markets),"markets_scored":len(rows),"failures":failures,"recommendations":rows}
    (ROOT/"report.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    lines=["2026 SUSQUEHANNA PREDICTIONS CUP — READ-ONLY RECOMMENDATIONS",f"As of {now} | {len(rows)} markets | min edge {MIN_EDGE:.1%} | cost {COST_BPS:.0f} bps","",f"{'SIDE':9} {'ASK':>6} {'EST':>6} {'EDGE':>7} {'CONF':8} MARKET","-"*100]
    for r in rows:
        f=lambda x: "  -- " if x is None else f"{x:5.1%}"
        lines.append(f"{r['side'][:9]:9} {f(r['available_price']):>6} {f(r['estimated_probability']):>6} {f(r['edge']):>7} {r['confidence'][:8]:8} {r['market']}")
        lines.append(f"  {r['reason']}")
    if failures: lines += ["","WARNINGS"]+[f"- {x['market']}: {x['error']}" for x in failures]
    report="\n".join(lines)+"\n"; (ROOT/"report.txt").write_text(report,encoding="utf-8"); print(report)
    return 0

if __name__ == "__main__": raise SystemExit(main())
