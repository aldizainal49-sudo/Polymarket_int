# Polymarket Weather / Temperature Bot

A **very conservative** trading bot for Polymarket that only touches
**weather / temperature** markets. It is built to chase **small, consistent
edges on near-certain outcomes** — not big wins. Its default behaviour is to
**skip almost everything** and stay idle until a market is extremely clear.

> **No AI / LLM is used.** There is no Claude, OpenAI, or any paid AI service
> in this bot. All decisions come from deterministic rules, weather forecasts,
> and order-book math.

> **This is not financial advice and it does not promise profit.** Markets can
> resolve against you, forecasts can be wrong, and APIs can fail. The bot is
> designed to *protect capital first*. Start in `DRY_RUN` and keep it there
> until you fully understand the behaviour.

---

## 1. Strategy

The bot looks for temperature markets that are *already* priced as
near-certainties and where an independent weather forecast **strongly agrees**
with that pricing, with a comfortable safety margin.

A trade is only opened when **every** one of these is true:

| Gate | Requirement |
|------|-------------|
| Category | Weather / temperature only (sports, politics, crypto, finance, culture, news are hard-rejected) |
| Entry price | between **0.95** and **0.985** |
| Resolution time | resolves within **12h** (preferred ≤ 6h); up to 24h only if rules are crystal-clear |
| Bot probability | **≥ 0.95** (from the forecast margin) |
| Confidence score | **≥ 0.95** (harsh geometric mean of 8 sub-scores) |
| Forecast distance | forecast clears the threshold by **≥ 3 °C / 5 °F** |
| Spread | **≤ 1%** |
| Liquidity | **≥ $500** on the side we buy |
| Rules | clear, with a named resolution source |
| Parsing | city, date, market type, and threshold all parsed successfully |
| Risk | no existing position in that market, open positions < limit, daily loss limit not hit |

If any single check fails, the market is **skipped** and the reason is recorded.

### Prediction logic (temperature)

- **Highest temp ≥ threshold** → buy YES only if the forecast high is *well above* the threshold.
- **Highest temp < threshold** → buy YES only if the forecast high is *well below* the threshold.
- **Lowest temp ≤ threshold** → buy YES only if the forecast low is *well below* the threshold.
- **Lowest temp > threshold** → buy YES only if the forecast low is *well above* the threshold.
- **Exact / range temperature** → **skipped by default** (`ALLOW_EXACT_TEMP_MARKETS=false`). Too hard to call reliably.

"Well above/below" means clearing the threshold by at least the configured
margin (`MIN_TEMP_DISTANCE_C` / `MIN_TEMP_DISTANCE_F`). If the forecast sits
**near** the threshold, the bot **skips** — this is the single most important
rule for avoiding losses.

### Confidence score

`confidence_score` is the **geometric mean** of eight sub-scores:

1. `city_parse_confidence`
2. `date_parse_confidence`
3. `rules_clarity_score`
4. `weather_data_quality`
5. `distance_from_threshold_score`
6. `time_to_resolution_score`
7. `liquidity_score`
8. `spread_score`

A geometric mean is used on purpose: **if any single component is weak, the
whole score collapses** and the bot skips. Final confidence must be ≥ 0.95.

---

## 2. Why only 0.95–0.985?

- **Below 0.95**: the market is implying meaningful doubt (≥ 5% chance of
  losing). That is not "near-certain", so it falls outside this bot's mandate.
- **Above 0.985**: the remaining profit is tiny (≤ ~1.5¢) while the downside is
  still the full stake. The risk/reward becomes poor, fees/slippage eat the
  edge, and these levels are often illiquid. The bot will **not chase** a price
  above 0.985.

This narrow band targets the "boring", high-probability tail where a small,
repeatable edge can exist — and pairs it with an independent forecast check so
the bot isn't just trusting the market price.

---

## 3. Why skip so much?

Because **avoiding losses is priority #1.** A single avoidable loss can wipe out
many small wins. The bot deliberately skips when:

- the forecast is near the threshold,
- the rules are ambiguous or the resolution source is unnamed,
- the spread is wide or liquidity is thin,
- parsing of city/date/threshold failed,
- the weather data is incomplete,
- it resolves too far out,
- a risk limit would be breached.

**If nothing qualifies, the bot does nothing and waits.** That is the intended,
correct behaviour. Expect roughly 5–20 high-quality candidates per day at most —
often fewer.

---

## 4. Install on an Ubuntu VPS

```bash
# 1) System packages
sudo apt update
sudo apt install python3 python3-venv python3-pip git -y

# 2) Get the code
git clone https://github.com/aldizainal49-sudo/Polymarket_int.git
cd Polymarket_int

# 3) Virtual environment + dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4) Configuration
cp .env.example .env
nano .env        # fill in values (see section 5)
```

Python **3.11+** is required.

---

## 5. Filling in `.env`

Copy `.env.example` to `.env` and edit it. The most important switches:

| Variable | Meaning |
|----------|---------|
| `DRY_RUN` | `true` = never send real orders (default, keep it true to start) |
| `LIVE_TRADING` | must be `true` **and** `DRY_RUN=false` for any real order |
| `WEATHER_PROVIDER` | `open_meteo` (default, no key) or `weatherapi` / `meteostat` / `visual_crossing` |
| `WEATHER_API_KEY` | only needed for the non-default providers |
| `MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE` | the 0.95–0.985 window |
| `MIN_BOT_PROBABILITY` / `MIN_CONFIDENCE_SCORE` | the 0.95 gates |
| `MIN_TEMP_DISTANCE_C` / `MIN_TEMP_DISTANCE_F` | the safety margin (3 °C / 5 °F) |
| `MIN_LIQUIDITY_USD` / `MAX_SPREAD_PERCENT` | microstructure gates |
| `MAX_POSITION_PER_MARKET_USD` | size cap per market ($1 default) |
| `MAX_OPEN_POSITIONS` / `MAX_DAILY_LOSS_USD` | risk limits (3 / $3 default) |

**Polymarket credentials** (`POLYMARKET_API_KEY`, `..._SECRET`,
`..._PASSPHRASE`, `..._PRIVATE_KEY`, optional `..._PROXY_ADDRESS`) are only
required for **live** trading. Leave them blank while in `DRY_RUN`.

> Keep `.env` private. Never commit it. The private key controls real funds.

---

## 6. Run a dry run

A dry run does the full pipeline (scan, forecast, score, decide) but **never
sends real orders** — it records *simulated* orders and positions in SQLite.

```bash
source venv/bin/activate

# Single pass, then exit (great for a first look):
python main.py scan

# Continuous loop in simulation:
python main.py dry-run
```

You'll see lines like `SKIP [SKIP_FORECAST_TOO_CLOSE] ...` and, occasionally,
`TRADE [SIMULATED] ...`.

---

## 7. Run live mode

Only do this after you trust the dry-run behaviour.

```bash
# In .env:
#   DRY_RUN=false
#   LIVE_TRADING=true
#   (and all POLYMARKET_* credentials filled in)

source venv/bin/activate
python main.py live
```

Safety in live mode:
- Only **limit** orders are used (never market orders).
- A **double safety check** runs: once when the trade is assembled, and again
  against a freshly fetched order book immediately before sending.
- If the ask has moved **above 0.985**, the bot **abandons** the trade instead
  of chasing.
- If credentials are missing or `.env` doesn't permit live trading, the `live`
  command automatically falls back to **simulation**.

---

## 8. Check the logs

```bash
# From the repo (file log):
python main.py logs --lines 100
tail -f weather_bot.log

# Under systemd (journald):
journalctl -u polymarket-weather-bot -f
```

---

## 9. Check positions and P&L

```bash
python main.py status      # config + today's counters
python main.py positions   # open/closed positions
python main.py pnl         # realized P&L, win/loss counts
python main.py rejected    # recently skipped markets + reasons
```

---

## 10. Stop the bot

```bash
# Foreground process: press Ctrl+C

# Under systemd:
sudo systemctl stop polymarket-weather-bot
sudo systemctl disable polymarket-weather-bot   # stop it starting at boot
```

---

## 11. Remove the bot

```bash
# Stop & remove the service
sudo systemctl stop polymarket-weather-bot
sudo systemctl disable polymarket-weather-bot
sudo rm /etc/systemd/system/polymarket-weather-bot.service
sudo systemctl daemon-reload

# Remove the code (and local DB/log)
cd ~
rm -rf Polymarket_int
```

---

## 12. Run 24/7 with systemd

```bash
# Optional: a dedicated unprivileged user
sudo adduser --system --group polybot
# Put the repo at /home/polybot/Polymarket_int and create the venv there.

# Install the unit file (edit the paths/User inside it first if needed)
sudo cp polymarket-weather-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start now + enable at boot
sudo systemctl enable --now polymarket-weather-bot

# Status & logs
systemctl status polymarket-weather-bot
journalctl -u polymarket-weather-bot -f
```

The provided unit runs `main.py dry-run` by default. When you are ready for
live trading, set `DRY_RUN=false` and `LIVE_TRADING=true` in `.env` and change
the `ExecStart` line to `... main.py live`, then
`sudo systemctl daemon-reload && sudo systemctl restart polymarket-weather-bot`.

---

## Project layout

| File | Responsibility |
|------|----------------|
| `main.py` | CLI + scan/decision orchestration loop |
| `config.py` | `.env` configuration (typed, with safety gates) |
| `polymarket_client.py` | Gamma API (markets) + CLOB API (order book, orders) |
| `weather_market_scanner.py` | Filter & parse temperature markets |
| `weather_data_client.py` | Open-Meteo (default) + WeatherAPI/Meteostat/Visual Crossing |
| `temperature_predictor.py` | Prediction logic + confidence scoring |
| `orderbook_analyzer.py` | Spread, liquidity, market price, implied probability |
| `risk_manager.py` | Price/size/position/daily-loss limits |
| `trader.py` | Limit-order execution with double safety check |
| `database.py` | SQLite (markets, forecasts, decisions, orders, positions, stats, rejections) |
| `utils.py` | Logging, conversions, scoring helpers, decision labels |
| `polymarket-weather-bot.service` | systemd unit for 24/7 operation |

---

## Decision labels

Every market the bot looks at is tagged with one of:

`TRADE_HIGH_CONFIDENCE`, `SKIP_PRICE_OUT_OF_RANGE`, `SKIP_LOW_CONFIDENCE`,
`SKIP_FORECAST_TOO_CLOSE`, `SKIP_LOW_LIQUIDITY`, `SKIP_WIDE_SPREAD`,
`SKIP_RULES_AMBIGUOUS`, `SKIP_PARSE_FAILED`, `SKIP_TOO_FAR_FROM_RESOLUTION`,
`SKIP_ALREADY_HAS_POSITION`, `SKIP_RISK_LIMIT` (plus a few internal variants
such as `SKIP_NOT_TEMPERATURE`, `SKIP_EXACT_TEMP_DISABLED`,
`SKIP_WEATHER_DATA_INCOMPLETE`, `SKIP_PREDICTION_CONFLICT`, `SKIP_NO_ORDERBOOK`).

---

## Reminders

- The bot **must skip more often than it trades.**
- It will **not** martingale, average down, or go all-in.
- It does **not** guarantee any outcome and never claims 100% certainty.
- Capital protection comes before profit, every time.
```
