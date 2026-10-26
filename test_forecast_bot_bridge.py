"""The pieces that connect election_forecast.py to sigbot: criteria, caveats, quotes, Kalshi export."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import election_forecast as ef


class FakeAPI:
    """Answers the GET paths make_recommendations uses, and records them."""
    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if path.startswith("/markets/") and path.endswith("/nodes"):
            return {"market_id": "1", "root": {"node_type": "operator", "operator": "AND", "children": [
                {"node_type": "contract", "title": "Will the Democratic Party win the Texas Senate?",
                 "contract_details": {"description": "Resolves YES if the Democratic nominee wins."}}]}}
        if path.startswith("/exchanges/prices"):
            return {"data": [{"exchangeId": "11", "bestBid": 0.20, "bestAsk": 0.22},
                             {"exchangeId": "12", "bestBid": 0.30, "bestAsk": 0.31},
                             {"exchangeId": "13", "bestBid": 0.60, "bestAsk": 0.62}], "missingIds": []}
        if path.startswith("/exchanges/11/orderbook"):
            return {"bids": [{"price": 0.20, "quantity": 500}], "asks": [{"price": 0.22, "quantity": 700}]}
        if "/portfolio/positions" in path:
            return {"positions": [{"marketTitle": "Will the Democratic Party win the Texas Senate?",
                                   "costBasis": 400.0, "marketValue": 350.0}], "summary": {}}
        if path.startswith("/orders"):
            return {"data": []}
        raise AssertionError(f"unexpected GET {path}")


MARKETS = [
    {"id": "1", "title": "Will the Democratic Party win the Texas Senate?", "exchanges": [{"id": "11", "option": None}]},
    {"id": "2", "title": "Will the Democratic Party win the Nebraska Senate?", "exchanges": [{"id": "12", "option": None}]},
    {"id": "3", "title": "Will the Democratic Party win the U.S. Senate?", "exchanges": [{"id": "13", "option": None}]},
]


def forecast(p, caveat=None):
    return ef.RaceForecast(p, max(0, p - .1), min(1, p + .1), .001, 0, 5, 3, "2026-10-20", caveat)


class Bridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(ef, "CACHE", Path(self.tmp))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def run_recs(self, api=None):
        api = api or FakeAPI()
        forecasts = {"TX-SEN": forecast(0.45), "NE-SEN": forecast(0.70, "competitive three-way race")}
        chambers = {"US Senate": {"p_dem": 0.66, "model_low": 0.58, "model_high": 0.74, "simulation_se": .001}}
        recs, recon = ef.make_recommendations(api, MARKETS, "tid", forecasts, chambers, "2026-10-25T00:00:00+00:00")
        return {r["market_id"]: r for r in recs}, recon, api

    def test_resolution_text_comes_from_nodes(self):
        text = ef.resolution_text(FakeAPI().get("/markets/1/nodes"))
        self.assertIn("Democratic nominee wins", text)

    def test_criteria_are_read_once_then_cached(self):
        _, _, api = self.run_recs()
        self.assertEqual(sum(p.endswith("/nodes") for p in api.paths), 3)
        _, _, api2 = self.run_recs()
        self.assertEqual(sum(p.endswith("/nodes") for p in api2.paths), 0)

    def test_previously_blocked_market_now_trades(self):
        recs, _, api = self.run_recs()
        tx = recs["1"]
        # conservative low 0.35 vs YES ask 0.22 -> 13 pts of edge
        self.assertEqual(tx["recommendation"], "BUY YES")
        self.assertGreater(tx["quantity"], 0)
        self.assertEqual(tx["criteria_source"], "nodes")
        # only the market clearing the edge cost a full-book read
        self.assertEqual([p.split("?")[0] for p in api.paths if "orderbook" in p], ["/exchanges/11/orderbook"])

    def test_caveat_race_is_not_traded_on_the_model(self):
        recs, _, _ = self.run_recs()
        ne = recs["2"]
        self.assertEqual(ne["recommendation"], "NO TRADE")
        self.assertIn("three-way", ne["model_caveat"])

    def test_existing_exposure_uses_cost_basis(self):
        _, recon, _ = self.run_recs()
        self.assertEqual(recon["existing_exposure"], 400.0)

    def test_race_caveat_rules(self):
        self.assertIsNone(ef.race_caveat({"threeway": "False", "rcv": "False", "same_party": "", "dem_name": "A",
                                          "rep_name": "B", "ind_frac": "0.0", "ind_name": "Minor Libertarian"}))
        self.assertIn("three-way", ef.race_caveat({"threeway": "True", "dem_name": "(No Democrat)", "rep_name": "B"}))
        self.assertIn("ranked-choice", ef.race_caveat({"rcv": "True", "dem_name": "A", "rep_name": "B"}))
        self.assertIn("same-party", ef.race_caveat({"same_party": "DEM", "dem_name": "A", "rep_name": "(no Republican — top-two)"}))

    def test_kalshi_tickers(self):
        self.assertEqual(ef.kalshi_candidates("chamber", "US House", "DEM"), ["CONTROLH-2026-D"])
        self.assertEqual(ef.kalshi_candidates("race", "AK-SEN", "REP"), ["SENATEAK-26-R"])
        self.assertEqual(ef.kalshi_candidates("race", "KS-GOV", "DEM"), ["GOVPARTYKS-26-D", "GOVPARTYKS-27-D"])
        self.assertEqual(ef.kalshi_candidates("race", "WI-03", "DEM"), ["HOUSEWI3-26-D", "HOUSEWIAL-26-D"])

    def test_independent_markets_map_but_never_use_the_model(self):
        self.assertEqual(ef.market_event("Will the Independent Party win the Nebraska Senate?"), ("race", "NE-SEN", "IND"))
        markets = MARKETS + [{"id": "4", "title": "Will the Independent Party win the Texas Senate?",
                              "exchanges": [{"id": "14", "option": None}]}]
        forecasts = {"TX-SEN": forecast(0.45)}   # no caveat on the race itself
        recs, _ = ef.make_recommendations(FakeAPI(), markets, "tid", forecasts, {}, "2026-10-25T00:00:00+00:00")
        ind = next(r for r in recs if r["market_id"] == "4")
        self.assertEqual(ind["recommendation"], "NO TRADE")
        self.assertIn("independent", ind["model_caveat"])
        with mock.patch.object(ef, "verify_kalshi", return_value=set()), \
             mock.patch.object(ef, "kalshi_independents", return_value={"TX-SEN": "SENATETX-26-IND"}):
            out = ef.fair_values(recs, "2026-10-25T00:00:00+00:00", {"TX-SEN": "Some Independent"})
        self.assertEqual(out["markets"]["14"]["kalshi"], "SENATETX-26-IND")
        self.assertNotIn("model", out["markets"]["14"])

    def test_fair_values_export(self):
        recs, _, _ = self.run_recs()
        live = {"SENATETX-26-D", "SENATENE-26-D"}  # pretend Kalshi lists TX and NE, not the Senate-control one
        with mock.patch.object(ef, "verify_kalshi", return_value=live):
            out = ef.fair_values(list(recs.values()), "2026-10-25T00:00:00+00:00")
        m = out["markets"]
        self.assertEqual(out["generated_at"], "2026-10-25T00:00:00+00:00")
        self.assertEqual(m["11"]["kalshi"], "SENATETX-26-D")
        self.assertEqual(m["11"]["model"], 0.45)
        self.assertEqual(m["11"]["take_edge"], ef.TAKE_EDGE_WITH_KALSHI)
        self.assertEqual(m["11"]["group"], "race:TX-SEN")
        self.assertNotIn("model", m["12"])            # caveat race: Kalshi only
        self.assertEqual(m["12"]["kalshi"], "SENATENE-26-D")
        self.assertNotIn("kalshi", m["13"])           # model only -> wider edge, smaller bets
        self.assertEqual(m["13"]["take_edge"], ef.TAKE_EDGE_MODEL_ONLY)
        self.assertEqual(m["13"]["size_mult"], 0.5)
        self.assertNotIn("size_mult", m["11"])


if __name__ == "__main__":
    unittest.main()
