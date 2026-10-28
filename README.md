# Election Prediction Market Trading Bot

An automated trading bot I built for the **2026 Susquehanna (SIG) Predictions Cup**, a competition where participants trade on the outcomes of the 2026 U.S. midterm elections. It forecasts every House, Senate and governor race, compares those forecasts with the competition's market prices, and buys contracts that look underpriced.

## The idea in plain English

A **prediction market** lets people buy and sell contracts on future events. A contract like *"Democrats win the Texas Senate race"* pays **1** if it happens and **0** if it doesn't. The price is the market's estimate of the chance it happens: a price of 0.40 means traders think it's about 40% likely.

If you can estimate that chance better than the price suggests, you have an **edge**. Say you think the real chance is 50% and the contract sells for 0.40. Buying it at 0.40 makes money on average, because over many bets like it you win half the time and collect 1 each time.

The bot does this automatically for every race in the competition:

1. **Estimate the real chance of each outcome** (the "fair value"), using its own election model and prices on real-money prediction markets.
2. **Compare the fair value with the competition's prices.**
3. **Buy when a contract is clearly cheaper than its fair value**, and sell later when the gap closes or the position is up enough.
4. **Size each bet carefully**, so no single race or wrong forecast can wipe out the account.

## How it works

```
public polls & results ─► election model ─┐
                                          ├─► fair value per contract ─► compare with competition prices ─► trade
Kalshi / Polymarket prices ───────────────┘
```

### 1. The election forecast (`election_forecast.py`)

- Downloads public polling, past results and race data (sources below).
- Estimates the expected margin in each race from polls, the national generic ballot, past results and incumbency.
- Runs **200,000 simulated elections**. Races are linked: a national polling miss shifts every race in the same direction, and nearby states move together. This matters because polling errors are usually shared, not independent.
- Counts how often each candidate wins, and how often each party controls the House and the Senate, across all the simulations.

### 2. Fair value (`sigbot/fair.py`)

The model's numbers are blended with prices from **Kalshi**, a large real-money prediction market, because a market where people bet real money is hard to beat:

- Kalshi and the model are weighted **3 to 1**.
- If the model disagrees with Kalshi by more than 15 points, the bot trusts Kalshi alone for that race. For example, the model can't see that a popular Republican governor in Vermont wins far more votes than his party usually does there.
- Races the model can't handle, such as three-way races or ranked-choice voting, use Kalshi only.
- Races with no Kalshi market use the model alone, but need a bigger edge and get smaller bets.
- Forecasts older than 36 hours are ignored, so the bot never trades on stale data.

### 3. Trading (`sigbot/`)

Every 20 seconds the bot checks the competition's prices and can do three things:

- **Take mispriced offers.** It buys YES when the price is at least 3 points below fair value, and buys NO (bets against the outcome) when the price is at least 3 points above it.
- **Arbitrage.** When a market has several outcomes and exactly one must happen, buying one of each costs less than 1 if their prices add up to less than 1. The bot buys every outcome and locks in the difference whatever the result.
- **Find longshots** (`sigbot longshots`). It lists cheap contracts whose real chance is higher than their price suggests.

## Risk controls

- **Bet sizing with half-Kelly.** The [Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion) gives the bet size that grows a bankroll fastest. The bot bets half of that to stay safe when its estimates are wrong.
- **Position limits.** Each contract is capped at 20% of the account, and the Democratic and Republican contracts on one race share a single cap.
- **Dry run first.** By default the bot only logs the trades it *would* make. Real orders need the `--live` flag.
- **Kill switch.** Running `stop.ps1` creates a `STOP` file, which stops the bot and cancels its open orders.
- **Rate limits and a hard stop** before the competition closes.

## Tech stack

- **Python 3.13**, using the standard library for almost everything
- **NumPy** for the vectorized election simulation (its only third-party dependency)
- The competition's REST API, plus Kalshi and Polymarket's public APIs
- `unittest` tests for the strategy logic, the forecast, and the bot loop

The trading rules in `sigbot/strategy.py` are pure functions with no network calls, so they can be unit-tested on their own.

## Running it

You need an API key for the competition platform. The code reads it from an environment variable, and it is never stored in the repo.

```powershell
py -3.13 -m venv .venv313
.\.venv313\Scripts\python.exe -m pip install -r requirements.txt
$env:SUPER_MARKET_API_KEY = "<your key>"

.\run-forecast.ps1                                  # run the election model
.\run-bot.ps1                                       # forecast, then a dry run (no real orders)
.\run-bot.ps1 --live                                # place real orders
.\.venv313\Scripts\python.exe -m sigbot fair        # fair value vs. market price for every contract
.\.venv313\Scripts\python.exe -m sigbot status      # positions, profit/loss and rank
.\.venv313\Scripts\python.exe -m sigbot longshots   # cheap contracts worth a look
.\stop.ps1                                          # kill switch
```

Tests and model validation:

```powershell
.\.venv313\Scripts\python.exe -m unittest -v
.\.venv313\Scripts\python.exe .\election_forecast.py --validate-only
```

Settings such as edge thresholds, bet sizes and limits are in `sigbot-config.json`.

## Project layout

| File | What it does |
|---|---|
| `election_forecast.py` | Election model: downloads data, simulates races, writes fair values |
| `sigbot/fair.py` | Blends the model with Kalshi and Polymarket prices |
| `sigbot/strategy.py` | Trading decisions: when to buy or sell, and how much |
| `sigbot/longshot.py` | Finds underpriced cheap contracts |
| `sigbot/bot.py` | Main loop: prices, positions, limits, kill switch |
| `sigbot/client.py` | API client with rate limiting |
| `recommender.py`, `trader.py` | Earlier read-only recommender and one-time order executor, replaced by `sigbot` |
| `test_*.py` | Unit tests |

## Limitations

- **The model was checked for calibration, not backtested.** The public historical data has past forecasts and outcomes, but not the inputs this model uses. So I could check that its probabilities are realistic, using the Brier score and calibration bins, but not replay the 2026 strategy on past elections.
- **Simple race model.** It treats races as two-party contests, which is why three-way and ranked-choice races fall back to Kalshi.
- **It depends on Kalshi** for most fair values. When Kalshi is wrong, the bot is wrong too.

## Data sources

- [ElectIndex 2026 forecast data](https://github.com/ElectIndex/26_us_forecast_data)
- [FiveThirtyEight forecast methodology data](https://github.com/fivethirtyeight/data/tree/master/forecast-methodology)
- [MIT Election Data and Science Lab](https://electionlab.mit.edu/data)
- [FEC election results](https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/)

Bots are allowed by the competition's official rules (section 6).
