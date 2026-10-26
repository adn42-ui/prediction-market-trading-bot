"""Fair-value estimates for each exchange, blended from real-money markets and your own view.

fair_values.json maps an exchange ID to its sources, e.g.
    "12345": {"label": "Dems win House", "kalshi": "CONTROLH-2026-D",
              "polymarket": "which-party-will-win-the-house-in-2026", "polymarket_outcome": "Democratic",
              "manual": 0.70, "weights": {"kalshi": 2, "polymarket": 1, "manual": 1},
              "adjust": 0.0, "invert": false}
Only one source is needed. `invert` flips a source quoted for the opposite outcome (p -> 1 - p).
`adjust` nudges the blended fair (+0.02 = you think YES is 2 points more likely than the market).

election_forecast.py writes this file automatically, adding `model` (its simulated probability),
`group` (entries on the same race share an exposure cap) and `take_edge` (per-entry threshold).
A `model` value older than max_model_age_hours is ignored, so a stale forecast can't drive trades.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY = "https://gamma-api.polymarket.com"


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "sigbot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _mid(bid, ask, last, max_spread):
    """Midpoint when the book is tight enough to trust, else last trade."""
    if bid is not None and ask is not None and 0 < bid <= ask < 1 and ask - bid <= max_spread:
        return (bid + ask) / 2
    if last is not None and 0 < last < 1:
        return last
    return None


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def kalshi_prob(ticker, max_spread=0.10):
    m = _get_json(f"{KALSHI}/markets/{urllib.parse.quote(ticker)}")["market"]
    bid = _f(m.get("yes_bid_dollars"))
    ask = _f(m.get("yes_ask_dollars"))
    last = _f(m.get("last_price_dollars"))
    if bid is None and m.get("yes_bid") is not None:  # older cent-denominated fields
        bid, ask, last = m["yes_bid"] / 100, m["yes_ask"] / 100, (m.get("last_price") or 0) / 100 or None
    return _mid(bid, ask, last, max_spread)


def polymarket_prob(slug, outcome=None, max_spread=0.10):
    rows = _get_json(f"{POLY}/markets?slug={urllib.parse.quote(slug)}")
    if not rows:
        raise LookupError(f"polymarket slug not found: {slug}")
    m = rows[0]
    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    prices = [float(p) for p in (json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"])]
    idx = 0 if outcome is None else [o.lower() for o in outcomes].index(outcome.lower())
    if idx == 0:
        p = _mid(_f(m.get("bestBid")), _f(m.get("bestAsk")), _f(m.get("lastTradePrice")), max_spread)
        if p is not None:
            return p
    return prices[idx]


class FairValues:
    def __init__(self, path, cache_seconds=60, max_spread=0.10, log=print, max_model_age_hours=36, max_model_gap=0.15):
        self.path = path
        self.cache_seconds = cache_seconds
        self.max_spread = max_spread
        self.log = log
        self.max_model_age_hours = max_model_age_hours
        self.max_model_gap = max_model_gap
        self._cache = {}  # (kind, key) -> (time, value)
        self._gap_logged = set()
        self.reload()

    def reload(self):
        with open(self.path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        self.model_ok = True
        if cfg.get("generated_at"):
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(cfg["generated_at"])).total_seconds() / 3600
            self.model_ok = age_h <= self.max_model_age_hours
            if not self.model_ok:
                self.log(f"[fair] model values are {age_h:.0f}h old (> {self.max_model_age_hours}h): ignoring them. "
                         "Re-run run-forecast.ps1 to refresh.")
        self.entries = {str(k): v for k, v in cfg.get("markets", cfg).items()
                        if not str(k).startswith("_")
                        and (v.get("kalshi") or v.get("polymarket") or v.get("manual") is not None
                             or (v.get("model") is not None and self.model_ok))}

    def entry(self, exchange_id):
        return self.entries.get(str(exchange_id), {})

    def bounds(self, exchange_id, fair, sources):
        """(low, high) to judge trades against. When the model is the only source, use its
        uncertainty range: buy YES only below `low`, buy NO only above `high`. With a real-money
        market in the blend the point estimate is trusted (low == high == fair)."""
        rng = self.entry(exchange_id).get("model_range")
        if set(sources) == {"model"} and rng and len(rng) == 2:
            return min(rng[0], fair), max(rng[1], fair)
        return fair, fair

    def _cached(self, kind, key, fn):
        hit = self._cache.get((kind, key))
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        try:
            val = fn()
        except Exception as e:  # one dead source must not stop the bot
            self.log(f"[fair] {kind}:{key} failed: {e}")
            val = hit[1] if hit else None
        self._cache[(kind, key)] = (time.time(), val)
        return val

    def get(self, exchange_id):
        """Returns (fair, {source: prob}) or (None, {}) if nothing usable."""
        e = self.entries.get(str(exchange_id))
        if not e:
            return None, {}
        found = {}
        if e.get("kalshi"):
            found["kalshi"] = self._cached("kalshi", e["kalshi"], lambda: kalshi_prob(e["kalshi"], self.max_spread))
        if e.get("polymarket"):
            key = (e["polymarket"], e.get("polymarket_outcome"))
            found["polymarket"] = self._cached("poly", key, lambda: polymarket_prob(*key, self.max_spread))
        if e.get("manual") is not None:
            found["manual"] = float(e["manual"])
        if e.get("model") is not None and self.model_ok:
            found["model"] = float(e["model"])
        found = {k: v for k, v in found.items() if v is not None}
        if not found:
            return None, {}
        if e.get("invert"):  # applies to outside markets only; manual/model are already in this contract's terms
            found = {k: (v if k in ("manual", "model") else 1 - v) for k, v in found.items()}
        markets = [v for k, v in found.items() if k in ("kalshi", "polymarket")]
        if "model" in found and markets:
            gap = found["model"] - sum(markets) / len(markets)
            if abs(gap) > self.max_model_gap:
                # A gap this large means the model is missing something real-money traders price
                # (candidate quality, incumbency: VT-GOV is model 0.01 vs Kalshi 0.74). Trust the market.
                if exchange_id not in self._gap_logged:
                    self.log(f"[fair] {e.get('label', exchange_id)}: model {found['model']:.2f} is {gap:+.2f} "
                             f"from markets; dropping model")
                    self._gap_logged.add(exchange_id)
                del found["model"]
        w = e.get("weights", {})
        total = sum(w.get(k, 1) for k in found)
        fair = sum(v * w.get(k, 1) for k, v in found.items()) / total + float(e.get("adjust", 0))
        return min(max(fair, 0.01), 0.99), found
