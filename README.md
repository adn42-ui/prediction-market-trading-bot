# 2026 Susquehanna Predictions Cup forecast

## Trading bot (sigbot)

`election_forecast.py` now also writes `fair_values.json`: one entry per competition
contract with the model probability and, where Kalshi lists the race, the Kalshi ticker
(verified live). `sigbot/` reads it and trades through the official API (bots are
allowed by the official rules, s.6).

```powershell
.\run-bot.ps1            # forecast, then dry-run (orders logged to trades.jsonl)
.\run-bot.ps1 --live     # real orders; refused while the STOP file exists
.\.venv313\Scripts\python.exe -m sigbot fair     # fair value vs book for every contract
.\.venv313\Scripts\python.exe -m sigbot status   # positions, P&L, rank
.\stop.ps1                                       # kill switch: bot stops and cancels resting orders
```

How fair value is formed for each contract:

- Kalshi and model are blended 3:1. The model's own validation is a calibration
  benchmark, not a backtest, so the deep real-money market gets more weight.
- If the model is more than 15 points from Kalshi it is dropped for that contract
  (e.g. VT governor: model 1% R vs Kalshi 74% R; the model can't see Phil Scott's
  personal vote). About 19 of 174 overlapping contracts hit this guard.
- Races the two-party model can't describe (three-way, ranked-choice, same-party
  top-two, missing nominee, all "Independent Party" contracts) use Kalshi only.
- Contracts with no Kalshi market (~42, mostly safe seats) use the model alone, need
  an 8-point edge instead of 3, and bet half size.
- Model values older than 36 hours are ignored; re-run the forecast daily.
- DEM and REP contracts on the same race share one exposure cap (`max_group_value`).

Settings live in `sigbot-config.json`. Check the resolution text for Nebraska Senate:
whether an Osborn win resolves the *Democratic* contract decides how it should be priced.

`trader.py` (one-time noon executor) is superseded by sigbot and left unchanged.

## Forecast

`election_forecast.py` is recommendation-only. It downloads and caches public
polling/race data, reads competition markets with GET requests, runs exactly
200,000 reproducible joint simulations, and writes `forecast-report.txt` and
`forecast-report.json`. It never imports or calls the live order executor.

## Environment

Tested with standard CPython 3.13.7 and NumPy 2.5.3 in `.venv313`. NumPy is the
only third-party dependency. Pandas, SciPy, scikit-learn, matplotlib, and a
Bayesian framework are not used: standard-library code handles CSVs, caching,
metrics, and calibration; NumPy handles the vectorized hierarchical simulation.

```powershell
py -3.13 -m venv .venv313
.\.venv313\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

In the authenticated PowerShell window:

```powershell
cd C:\Users\akach\Downloads\sig-cup-recommender
.\run-forecast.ps1
```

The report records source URLs, retrieval timestamps, hashes, current generic
ballot and race polls, historical baselines, incumbency, time to Election Day,
resolution criteria, executable asks, spread, fees, liquidity, numerical model
uncertainty, and Monte Carlo sampling error. Missing polls remain missing; no
subjective confidence rating is invented.

Public inputs:

- https://github.com/ElectIndex/26_us_forecast_data
- https://github.com/fivethirtyeight/data/tree/master/forecast-methodology
- https://electionlab.mit.edu/data
- https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/

The simulation uses seed `20261001` with shared national, regional, and
race-specific errors. House and Senate control are calculated in the same draws.
Related/complementary contracts share one exposure group. A recommendation is
actionable only when exact resolution text supports its party mapping and its
conservative edge—using the adverse end of the model interval—clears the minimum.

Validation uses expanding-year holdouts with calibration choices fitted only on
earlier years and reports Brier score, log loss, calibration bins, raw-reference,
and 50% baselines. The public historical file has forecast probabilities and
outcomes, not complete snapshots of this model's features; therefore validation
is a limited calibration benchmark, not a direct backtest of the 2026 model.

Offline validation and tests:

```powershell
.\.venv313\Scripts\python.exe .\election_forecast.py --validate-only
.\.venv313\Scripts\python.exe -m unittest -v
```

Public downloads use a six-hour cache, request pacing, `Retry-After`, and bounded
read retries. Order submissions are never retried by this model because it makes
no write requests. Existing trading guards and limits remain in the legacy
executor; `STOP` is preserved. Credentials, environments, caches, logs, reports,
and authorization/order state are ignored by Git.
