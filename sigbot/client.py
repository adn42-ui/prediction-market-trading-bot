"""Thin client for the Super Market API (Predictions Cup). Stdlib only."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ApiError(Exception):
    def __init__(self, status, code, message, details=None, retry_after=None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.details = details
        self.retry_after = retry_after


class RateLimiter:
    """Sliding 60s window. The account-wide limit is shared by every key, so stay under it."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.stamps = []
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.stamps = [t for t in self.stamps if now - t < 60]
                if len(self.stamps) < self.per_minute:
                    self.stamps.append(now)
                    return
                wait = 60 - (now - self.stamps[0]) + 0.05
            time.sleep(wait)

    def remaining(self):
        with self.lock:
            now = time.monotonic()
            self.stamps = [t for t in self.stamps if now - t < 60]
            return self.per_minute - len(self.stamps)


class Client:
    def __init__(self, api_key, base_url, reads_per_min=80, writes_per_min=25, timeout=20):
        if not api_key:
            raise ValueError("No API key. Set SUPER_MARKET_API_KEY (or put it in a .env file).")
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.reads = RateLimiter(reads_per_min)
        self.writes = RateLimiter(writes_per_min)
        self.timeout = timeout

    # ---- transport -------------------------------------------------------
    def _request(self, method, path, params=None, body=None, retries=3):
        url = self.base + path
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        limiter = self.reads if method == "GET" else self.writes
        for attempt in range(retries + 1):
            limiter.acquire()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "sigbot/1.0")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    err = json.loads(raw).get("error", {})
                except Exception:
                    err = {}
                retry_after = e.headers.get("Retry-After")
                exc = ApiError(e.code, err.get("code", "HTTP_ERROR"),
                               err.get("message", raw[:200].decode(errors="replace")),
                               err.get("details"), retry_after)
                # Retry only what is safe to retry. Orders carry an idempotency key, so
                # re-sending the same body after 429/5xx cannot double-fill.
                if attempt < retries and (e.code == 429 or e.code >= 500):
                    time.sleep(float(retry_after) if retry_after else 2 * (attempt + 1))
                    continue
                raise exc
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ApiError(0, "NETWORK", str(e))

    def get(self, path, **params):
        return self._request("GET", path, params)

    # ---- reads -----------------------------------------------------------
    def account(self, tournament_id=None):
        return self.get("/account", tournamentId=tournament_id)

    def tournaments(self, status="active"):
        return self.get("/tournaments", status=status, limit=100)["data"]

    def tournament(self, slug):
        return self.get(f"/tournaments/{slug}")

    def tournament_markets(self, slug, status="open"):
        out, cursor = [], None
        while True:
            page = self.get(f"/tournaments/{slug}/markets", status=status, limit=100, cursor=cursor)
            out += page["data"]
            cursor = page["pagination"].get("nextCursor")
            if not page["pagination"].get("hasMore") or not cursor:
                return out

    def tournament_market(self, slug, market_id):
        return self.get(f"/tournaments/{slug}/markets/{market_id}")

    def prices(self, exchange_ids, tournament_id):
        out = []
        ids = list(exchange_ids)
        for i in range(0, len(ids), 100):
            out += self.get("/exchanges/prices", ids=",".join(ids[i:i + 100]),
                            tournamentId=tournament_id)["data"]
        return {p["exchangeId"]: p for p in out}

    def orderbook(self, exchange_id, tournament_id, depth=20):
        return self.get(f"/exchanges/{exchange_id}/orderbook", tournamentId=tournament_id, depth=depth)

    def open_orders(self, tournament_id):
        out, cursor = [], None
        while True:
            page = self.get("/orders", status="open", limit=200, tournamentId=tournament_id, cursor=cursor)
            out += page["data"]
            cursor = page["pagination"].get("nextCursor")
            if not page["pagination"].get("hasMore") or not cursor:
                return out

    def positions(self, slug):
        return self.get(f"/tournaments/{slug}/portfolio/positions")

    def pnl(self, slug):
        return self.get(f"/tournaments/{slug}/portfolio/pnl", period="all")

    def leaderboard(self, slug, limit=10, offset=0):
        return self.get(f"/tournaments/{slug}/leaderboard", limit=limit, offset=offset)

    # ---- writes ----------------------------------------------------------
    def place_order(self, exchange_id, side, action, quantity, price, tournament_id, expires_at=None):
        body = {
            "exchangeId": str(exchange_id),
            "side": side,
            "action": action,
            "quantity": int(quantity),
            "price": price,
            "tournamentId": tournament_id,
            "idempotencyKey": str(uuid.uuid4()),
        }
        if expires_at:
            body["expirationDate"] = expires_at
        return self._request("POST", "/orders", body=body)

    def multi_leg(self, legs, tournament_id, all_or_none=True):
        for leg in legs:
            leg["tournamentId"] = tournament_id
        body = {"legs": legs, "idempotencyKey": str(uuid.uuid4()),
                "executionMode": "allOrNone" if all_or_none else "default"}
        return self._request("POST", "/orders/multi-leg", body=body)

    def cancel(self, order_id):
        return self._request("DELETE", f"/orders/{order_id}")

    def cancel_all(self, tournament_id):
        return self._request("POST", "/orders/cancel-all", body={"tournamentId": tournament_id})
