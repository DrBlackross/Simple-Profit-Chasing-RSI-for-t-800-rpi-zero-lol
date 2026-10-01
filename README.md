# T-800 RSI Crypto Trading Bots

Python-based RSI cryptocurrency trading bots for **Kraken** and **Coinbase**, with paper-trading support, optional live trading, Flask dashboards, Discord notifications, retry handling, order timeouts, and optional WS2812 “T-800 eye” LED status indicators.

> ⚠️ **Warning:** This project is experimental software, not financial advice. Cryptocurrency trading is high risk. Run paper trading first, review every configuration value, and never use API keys with withdrawal permissions.

## Supported Bots

| Script | Exchange | Default Market | Default Timeframe | Dashboard Port |
|---|---|---:|---:|---:|
| `SSRsi-Kraken-Gemini-T800.py` | Kraken | `BTC/USDC` | `15m` | `5000` |
| `SSRsi-Coinbase-Gemini-T800.py` | Coinbase | `DOGE/USDC` | `5m` | `5010` |

Both bots are configured for **paper trading by default**. Live trading is enabled only when `LIVE_TRADING=true` is set in the environment.

## Features

- RSI-based entry and exit signals using RSI(14)
- Paper trading mode for safer testing
- Optional live limit-order execution through CCXT
- Kraken support for configurable trading pairs through `TRADING_PAIR`
- Coinbase support for the configured DOGE/USDC market
- Flask dashboard for balances, price, RSI, PnL, fees, configuration, and recent trades
- JSON dashboard update endpoint at `/update_data`
- Discord webhook notifications for bot startup and filled live orders
- API retry wrapper with exponential backoff
- Candle buffer for RSI calculation
- Order timeout and stale-order cancellation
- Position-cost and average-entry tracking
- Fee-aware break-even checks
- Configurable profit target and stop loss
- Cooldown after filled orders to reduce churn
- Optional Raspberry Pi / WS2812 LED status “eyes”
- Log files for reviewing activity and troubleshooting

## Strategy Summary

The bots calculate the Relative Strength Index (RSI) from OHLCV candle data.

### Buy logic

A buy may be placed when:

- RSI falls below the configured oversold threshold
- The bot has sufficient quote-currency balance
- The bot is not already tracking an open position
- There is no active order
- The post-trade cooldown has passed

### Sell logic

A sell may be placed when:

- RSI rises above the configured overbought threshold
- The bot has enough asset balance to satisfy the exchange minimum
- The current price is at or above the bot’s fee-aware break-even / profit conditions

### Stop-loss logic

If the current price falls below the configured stop-loss price relative to the average entry price, the bot attempts an urgent sell independently of the RSI signal.

### Important behavior

The scripts use limit orders. For urgent exits such as stop losses or qualified take-profit exits, the bot offsets the order price across the spread to encourage a faster fill. This still does **not** guarantee execution during fast market moves, low liquidity, exchange downtime, API errors, or unusual market conditions.

## Default Settings

| Setting | Kraken Bot | Coinbase Bot |
|---|---:|---:|
| Default pair | `BTC/USDC` | `DOGE/USDC` |
| Candle timeframe | `15m` | `5m` |
| RSI period | `14` | `14` |
| Buy threshold | RSI `< 30` | RSI `< 30` |
| Sell threshold | RSI `> 70` | RSI `> 70` |
| Default paper balance | `1000 USDC` | `100 USDC` |
| Trade cooldown | 5 minutes | 5 minutes |
| Default order timeout | 15 minutes | 5 minutes |
| Default profit target | 1.5% | 1.5% |
| Default stop loss | 3% | 3% |
| Estimated fee rate | 0.4% per side | 0.4% per side |

The default profit target, stop loss, and estimated fee rate can be overridden with environment variables.

## Requirements

- Python 3.10 or newer recommended
- A Kraken or Coinbase account if using live trading
- Exchange API credentials with **trading only**
- Flask templates matching the names expected by the scripts
- Optional: Raspberry Pi-compatible hardware and WS2812 LEDs for the T-800 eye effects

### Python packages

Install the core dependencies:

```bash
pip install ccxt flask python-dotenv pandas requests
```

Optional LED support:

```bash
pip install rpi-ws281x
```

> The bot can run without LED hardware. The LED component is intended to disable itself when the `rpi_ws281x` package or compatible hardware is unavailable. [1][2]

## Project Structure

A recommended repository structure:

```text
t800-rsi-trading-bots/
├── SSRsi-Kraken-Gemini-T800.py
├── SSRsi-Coinbase-Gemini-T800.py
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── templates/
│   ├── index-kraken.html
│   └── index-coinbase.html
└── static/
    └── ...
```

The Kraken script renders `templates/index-kraken.html`.

The Coinbase script renders `templates/index-coinbase.html`.

## Installation

### 1. Clone the repository

```bash
git clone [https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git](https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git)
cd YOUR-REPOSITORY
```

### 2. Create and activate a virtual environment

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

If you do not have a `requirements.txt` file yet, create one containing:

```text
ccxt
Flask
python-dotenv
pandas
requests
```

Optional Raspberry Pi LED support:

```text
rpi-ws281x
```

### 4. Create your environment file

Copy the example file:

```bash
cp .env.example .env
```

Then edit `.env` and enter only the credentials needed for the bot you plan to run.

## Environment Configuration

Create a local `.env` file in the same folder as the scripts.

### Example `.env`

```dotenv
# Safety: false means paper trading.
LIVE_TRADING=false

# Optional Discord webhook. Leave blank to disable notifications.
DISCORD_WEBHOOK_URL=

# Kraken credentials — required only for Kraken live trading.
KRAKEN_API_KEY=
KRAKEN_API_SECRET=

# Kraken market override.
TRADING_PAIR=BTC/USDC

# Coinbase credentials — required only for Coinbase live trading.
COINBASE_API_KEY=
COINBASE_API_SECRET=

# Strategy / risk controls.
MIN_PROFIT_PCT=0.015
STOP_LOSS_PCT=0.03
ESTIMATED_FEE_RATE=0.004
```

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `LIVE_TRADING` | Enables real exchange orders when set to `true` | `false` |
| `KRAKEN_API_KEY` | Kraken API key | `your_key_here` |
| `KRAKEN_API_SECRET` | Kraken API secret | `your_secret_here` |
| `COINBASE_API_KEY` | Coinbase API key | `your_key_here` |
| `COINBASE_API_SECRET` | Coinbase API secret | `your_secret_here` |
| `DISCORD_WEBHOOK_URL` | Optional Discord webhook URL | `https://discord.com/api/webhooks/...` |
| `TRADING_PAIR` | Kraken market pair override | `BTC/USDC` |
| `MIN_PROFIT_PCT` | Minimum desired profit target as a decimal | `0.015` |
| `STOP_LOSS_PCT` | Stop-loss threshold as a decimal | `0.03` |
| `ESTIMATED_FEE_RATE` | Estimated fee rate per order side as a decimal | `0.004` |

### Percentage examples

```text
0.015 = 1.5%
0.03  = 3%
0.004 = 0.4%
```

## Running the Bots

### Run Kraken in paper mode

Make sure this exists in `.env`:

```dotenv
LIVE_TRADING=false
```

Start the bot:

```bash
python3 SSRsi-Kraken-Gemini-T800.py
```

Open the dashboard:

```text
http://127.0.0.1:5000
```

For another machine on your local network, use:

```text
http://YOUR-SERVER-IP:5000
```

### Run Coinbase in paper mode

Start the bot:

```bash
python3 SSRsi-Coinbase-Gemini-T800.py
```

Open the dashboard:

```text
http://127.0.0.1:5010
```

For another machine on your local network:

```text
http://YOUR-SERVER-IP:5010
```

### Run both bots

The scripts use different Flask ports, so they can run at the same time:

```bash
python3 SSRsi-Kraken-Gemini-T800.py
```

In a second terminal:

```bash
python3 SSRsi-Coinbase-Gemini-T800.py
```

## Live Trading Checklist

Do not enable live trading until the paper-trading behavior has been reviewed over time.

Before setting `LIVE_TRADING=true`:

- Confirm the exchange account and API credentials are correct
- Disable withdrawal permissions on the API key
- Enable only the minimum trading permissions required
- Confirm the exchange supports the configured market
- Verify exchange minimum order sizes and precision rules
- Verify account balances and quote currency
- Review the current values for fees, stop loss, profit target, and order timeout
- Test dashboard access locally
- Confirm Discord notifications, if used
- Start with an amount you can afford to lose
- Monitor the logs and open orders after startup

To enable live trading:

```dotenv
LIVE_TRADING=true
```

Then restart the selected bot.

> ⚠️ Setting `LIVE_TRADING=true` allows the script to submit real orders using the supplied API credentials. Use at your own risk.

## Dashboard

Each bot starts a Flask web dashboard.

The dashboard is designed to show:

- Current quote and asset balances
- Latest market price
- Latest RSI value
- Current PnL and PnL percentage
- Estimated fees paid
- Recent trade history
- RSI configuration
- Maker and taker fee values
- Order-timeout setting
- Trading enable/disable status

The scripts also expose an update endpoint:

```text
/update_data
```

Example:

```text
http://127.0.0.1:5000/update_data
```

## LED Status Indicators

If WS2812 LEDs are connected and `rpi_ws281x` is available, the T-800 LED controller can provide status feedback:

| Bot state | LED behavior |
|---|---|
| Waiting for RSI signal | Red and green breathing cycle |
| Buy order active | Green pulse |
| Sell order active | Red pulse |

The default LED configuration is:

```text
LED count: 2
GPIO pin: 18
Brightness: 10
```

This feature is optional. The bot should continue operating without physical LEDs.

## Logs

The bots write activity to local log files:

```text
rsi_trading-kraken.log
rsi_trading-coinbase.log
```

Watch logs live on Linux:

```bash
tail -f rsi_trading-kraken.log
```

or:

```bash
tail -f rsi_trading-coinbase.log
```

Logs are useful for reviewing:

- API and network errors
- RSI evaluations
- Balance updates
- Placed orders
- Filled orders
- Stop-loss events
- Risk-gated sell decisions
- Stale-order cancellation

## Security

Never commit real secrets, API keys, exchange credentials, or webhook URLs to GitHub.

Add this to `.gitignore`:

```gitignore
# Secrets
.env

# Python
.venv/
__pycache__/
*.py[cod]

# Logs
*.log

# OS files
.DS_Store
```

You should commit `.env.example`, but **never** commit your actual `.env` file.

## Limitations and Risks

- This project is not production-grade trading infrastructure.
- Paper fills are simulated and may not match real exchange execution.
- Real limit orders can remain open, partially fill, or fail to fill.
- Network outages, rate limits, exchange API changes, and exchange maintenance can interrupt the bot.
- A stop-loss implemented through an API-submitted order cannot guarantee a specific exit price.
- The estimated fee rate may differ from your actual fee tier.
- Historical RSI behavior does not predict future market performance.
- Cryptocurrency prices can move rapidly and unpredictably.
- You are responsible for testing, monitoring, exchange compliance, taxes, risk controls, and all trading decisions.

## Suggested Improvements

Potential next upgrades for this project:

- Add unit tests for RSI, cost-basis, fee, and exit calculations
- Add persistent trade and position storage with SQLite or PostgreSQL
- Add proper authentication for the Flask dashboard
- Add HTTPS and reverse-proxy deployment instructions
- Add exchange-specific order validation before submission
- Add partial-fill support
- Add a dry-run mode separate from paper-fill simulation
- Add configurable RSI thresholds through environment variables
- Add a trailing-stop implementation
- Add structured logs and log rotation
- Add Docker support
- Add systemd service files for automatic startup and recovery
- Add backtesting and historical performance reports
- Add alerting for failed API calls, unfilled orders, and stop-loss actions

## Disclaimer

This repository is provided for educational and experimental purposes only. It does not provide investment, trading, legal, or tax advice. Use of this code and any resulting trades is entirely your responsibility.

## License

Choose a license before publishing.

A common choice for open-source Python projects is the MIT License. If you use it, add a file named `LICENSE` to the repository containing the MIT License text.
