# SSRsi-Kraken-Gemini-T800.py

from collections import deque
from datetime import datetime, timedelta
import functools
import logging
from logging import StreamHandler
import os
import threading
import time

from flask import Flask, jsonify, render_template

import ccxt
from dotenv import load_dotenv
import pandas as pd
import requests

load_dotenv()

# Silence overly chatty built-in Werkzeug web server logs
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# ==========================================
# 0. LED CONTROLLER MODULE (T-800 Eyes)
# Independent hardware driver for WS2812 LED status eyes.
# ==========================================
try:
    from rpi_ws281x import PixelStrip, Color

    LED_AVAILABLE = False
except ImportError:
    LED_AVAILABLE = False


class T800LEDController:
    """Runs in a background thread to manage breathing and fading animations

    based on live trading states without blocking the core bot loop.
    """

    def __init__(self, led_count=2, pin=18, brightness=10):
        self.enabled = LED_AVAILABLE
        if not self.enabled:
            logging.warning(
                "rpi_ws281x not available or running without root privileges. LEDs disabled."
            )
            return

        self.led_count = led_count
        self.pin = pin
        self.brightness = brightness

        # WS2812 Configuration
        self.freq_hz = 800000
        self.dma = 10
        self.invert = False
        self.channel = 0

        try:
            self.strip = PixelStrip(
                self.led_count,
                self.pin,
                self.freq_hz,
                self.dma,
                self.invert,
                self.brightness,
                self.channel,
            )
            self.strip.begin()
            self._clear()
            logging.info("T-800 LED hardware initialized successfully.")
        except Exception as e:
            self.enabled = False
            logging.error(f"Failed to initialize LED strip hardware: {e}")

    def _clear(self):
        if not self.enabled:
            return
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()

    def set_solid_color(self, red, green, blue):
        if not self.enabled:
            return
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, Color(red, green, blue))
        self.strip.show()

    def fade_pulse(self, r1, g1, b1, r2, g2, b2, steps=30, delay=0.03):
        """Smoothly transitions or pulses between two color states."""
        if not self.enabled:
            return
        for step in range(steps + 1):
            factor = step / float(steps)
            curr_r = int(r1 + (r2 - r1) * factor)
            curr_g = int(g1 + (g2 - g1) * factor)
            curr_b = int(b1 + (b2 - b1) * factor)
            self.set_solid_color(curr_r, curr_g, curr_b)
            time.sleep(delay)

    def animation_waiting_rsi(self):
        """Slow breathing cycle between Red and Green when waiting for RSI triggers."""
        self.fade_pulse(0, 0, 0, 150, 0, 0, steps=25, delay=0.0001)
        self.fade_pulse(150, 0, 0, 0, 0, 0, steps=25, delay=0.0001)
        self.fade_pulse(0, 0, 0, 0, 150, 0, steps=25, delay=0.0001)
        self.fade_pulse(0, 150, 0, 0, 0, 0, steps=25, delay=0.0001)

    def animation_sell_position(self):
        """Slow fade off and on RED when a sell position / order is in play."""
        self.fade_pulse(0, 0, 0, 200, 0, 0, steps=30, delay=0.0005)
        self.fade_pulse(200, 0, 0, 0, 0, 0, steps=30, delay=0.0005)

    def animation_buy_position(self):
        """Slow fade off and on GREEN when a buy position / order is in play."""
        self.fade_pulse(0, 0, 0, 0, 200, 0, steps=30, delay=0.0005)
        self.fade_pulse(0, 200, 0, 0, 0, 0, steps=30, delay=0.0005)


# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
API_KEY = os.getenv("KRAKEN_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "YOUR_API_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

DEFAULT_SYMBOL = "BTC/USDC"
TIMEFRAME = "15m"
RSI_PERIOD = 14  # was 2 - RSI(2) is noise
RSI_LOW = 30
RSI_HIGH = 70  # was 68 - bring RSI(14) thresholds in line with real signal
TRADE_AMOUNT_QUOTE = 10.0  # Budget per trade when not using 100% position sizing

PROFIT_PERCENTAGE = 0.75
PRICE_ADJUSTMENT = 0.006  # legacy, unused after __init__
MIN_QUOTE_TRADE = 1.0
MIN_CRYPTO_TRADE = 0.00001
POSITION_PERCENTAGE = 100
ORDER_TIMEOUT_MINUTES = 15

# --- Profit-chasing / risk-management (NEW) ---
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.015"))  # 1.5% net target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))  # 3% stop loss
ESTIMATED_FEE_RATE = float(os.getenv("ESTIMATED_FEE_RATE", "0.004"))  # 0.4% per side

live_trading_env = os.getenv("LIVE_TRADING", "false").lower() == "true"
trading_pair_env = os.getenv("TRADING_PAIR", DEFAULT_SYMBOL)

# Global dictionary tracking runtime state for the Web UI dashboard
bot_state = {
    "status": "Running",
    "last_check": "Never",
    "current_price": 0.0,
    "current_rsi": 0.0,
    "balance_quote": 0.0,
    "balance_asset": 0.0,
    "trading_enabled": True,
    "paper_trading": not live_trading_env,
    "order_timeout_minutes": ORDER_TIMEOUT_MINUTES,
    "active_orders": {},
    "positions": [],
    "recent_logs": [],
}


# ==========================================
# 2. LOGGING & FILTER SETUP
# ==========================================
class NoRecursiveWarningsFilter(logging.Filter):

    def filter(self, record):
        return not record.getMessage().startswith("Skipping malformed log line")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("rsi_trading-kraken.log", mode="a"),
        StreamHandler(),
    ],
)
logging.getLogger().addFilter(NoRecursiveWarningsFilter())


# ==========================================
# 3. ERROR RESILIENCE (RETRY DECORATOR)
# ==========================================
def retry_api_call(retries=3, delay=2, backoff=2):
    """Decorator that wraps API calls with exponential backoff to handle

    transient network dropouts or temporary rate limits gracefully.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries:
                        logging.error(
                            f"API call failed '{func.__name__}' after {retries} attempts: {e}"
                        )
                        raise e
                    logging.warning(
                        f"API call '{func.__name__}' failed ({e}). Retrying in {current_delay}s (Attempt {attempt}/{retries})..."
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff

        return wrapper

    return decorator


def send_discord_notification(title, description, color=0x00B894, fields=None):
    """Sends formatted event embeds out to a specified Discord Webhook."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": "RSI Trading Bot - Kraken T-800"},
        }
        if fields:
            embed["fields"] = fields
        payload = {
            "embeds": [embed],
            "username": "RSI Trading Bot",
            "avatar_url": "https://cryptologos.cc/logos/bitcoin-btc-logo.png",
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        logging.error(f"Failed to send Discord notification: {e}")


# ==========================================
# 4. CORE TRADING CLASS (RSI & KRAKEN LOGIC)
# ==========================================
class RSITrader:

    def __init__(
        self, api_key, secret_key, trading_pair=DEFAULT_SYMBOL, paper_trading=True
    ):
        self.paper_trading = paper_trading
        bot_state["paper_trading"] = self.paper_trading

        # Initialize LED Controller instance (T-800 hardware eyes)
        self.leds = T800LEDController()
        self.active_orders = {}
        bot_state["active_orders"] = self.active_orders
        self.start_led_thread()

        # Initialize CCXT driver for Kraken
        self.exchange = ccxt.kraken(
            {
                "apiKey": api_key,
                "secret": secret_key,
                "enableRateLimit": True,
                "timeout": 30000,
            }
        )
        self.load_markets_safe()

        # Parse trading pair formats (handles base/quote separation safely)
        cleaned_pair = trading_pair.upper().strip()
        if "/" in cleaned_pair:
            parts = cleaned_pair.split("/")
            raw_base, raw_quote = parts[0], parts[1]
        else:
            raw_base, raw_quote = cleaned_pair[:3], cleaned_pair[3:]

        self.base_symbol = "BTC" if raw_base == "XBT" else raw_base
        self.quote_symbol = "USDC" if raw_quote in ["USDC", "USDC.E"] else raw_quote

        potential_symbols = [
            f"{raw_base}/{raw_quote}",
            f"XBT/{raw_quote}" if raw_base == "BTC" else f"{raw_base}/{raw_quote}",
            cleaned_pair,
        ]

        # Resolve correct CCXT symbol representation matching Kraken's internal mapping
        self.kraken_symbol = None
        for sym in potential_symbols:
            if sym in self.exchange.markets:
                self.kraken_symbol = sym
                break
        if not self.kraken_symbol:
            self.kraken_symbol = "BTC/USDC"
            self.base_symbol = "BTC"
            self.quote_symbol = "USDC"

        # Fee defaults (Kraken taker tier 0)
        self.maker_fee = 0.0016
        self.taker_fee = 0.0026

        self.rsi_period = RSI_PERIOD
        self.rsi_low = RSI_LOW
        self.rsi_high = RSI_HIGH
        self.interval = TIMEFRAME
        self.trades = []
        self.log_file = "rsi_trading-kraken.log"
        self.profit_percentage = PROFIT_PERCENTAGE
        self.price_adjustment = self.profit_percentage / 100.0

        self.min_quote_trade = MIN_QUOTE_TRADE
        self.min_crypto_trade = MIN_CRYPTO_TRADE
        self.trade_amount_quote = TRADE_AMOUNT_QUOTE
        self.position_percentage = POSITION_PERCENTAGE

        self.skip_sell_due_to_dust = False
        self.initial_valuation_balance = 0.0
        self.trading_enabled = True
        bot_state["trading_enabled"] = self.trading_enabled
        self.order_timeout_minutes = ORDER_TIMEOUT_MINUTES

        # Prime the candle ring buffer for technical indicators
        self.candle_buffer = deque(maxlen=150)
        print("Initializing: Fetching initial candles from Kraken...")
        self.fetch_initial_candles()

        # Paper trading balances (1000 USDC starting balance)
        self.current_quote_balance = 1000.0 if self.paper_trading else 0.0
        self.current_crypto_balance = 0.0 if self.paper_trading else 0.0
        self.total_fees_paid = 0.0

        if not self.paper_trading:
            print("Initializing: Updating account balances...")
            self.update_balances()
        else:
            bot_state["balance_quote"] = self.current_quote_balance
            bot_state["balance_asset"] = self.current_crypto_balance

        self.initial_quote_balance = self.current_quote_balance
        self.initial_crypto_balance = self.current_crypto_balance
        self.last_trade_time = None
        self.trade_cooldown_minutes = 5  # was 1 - too short to prevent churn

        # --- Profit-chasing / cost basis state (NEW) ---
        self.estimated_fee_rate = ESTIMATED_FEE_RATE
        self.min_profit_pct = MIN_PROFIT_PCT
        self.stop_loss_pct = STOP_LOSS_PCT

        self.avg_entry_price = 0.0
        self.position_cost_usdc = 0.0
        self.position_volume = 0.0
        self.highest_price_since_entry = 0.0

        # Seed cost basis if we already hold crypto (live mode with pre-existing position)
        if self.current_crypto_balance > 0:
            seed_price = self.fetch_current_price()
            if seed_price and seed_price > 0:
                self.position_volume = self.current_crypto_balance
                self.position_cost_usdc = self.current_crypto_balance * seed_price
                self.avg_entry_price = seed_price
                self.highest_price_since_entry = seed_price
                logging.info(
                    f"[init] Seeded position: {self.position_volume:.8f} "
                    f"{self.base_symbol} @ {seed_price:.2f}"
                )

        # PnL baseline (no more 60000 fallback)
        current_price = self.fetch_current_price()
        if current_price and current_price > 0:
            self.initial_valuation_balance = self.current_quote_balance + (
                self.current_crypto_balance * current_price
            )
        else:
            self.initial_valuation_balance = self.current_quote_balance
            logging.warning(
                "[init] Could not fetch initial price; PnL baseline uses quote only"
            )

        self.prices = []
        self.rsis = []
        self.timestamps = []
        self.max_data_points = 100

        # Calculate initial historical RSI timeline points
        if len(self.candle_buffer) >= self.rsi_period + 1:
            df = pd.DataFrame(
                list(self.candle_buffer),
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = pd.to_numeric(df["close"])
            delta = df["close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
            rs = gain / loss
            rsi_series = 100 - (100 / (1 + rs))

            for i in range(len(df)):
                ts = datetime.fromtimestamp(df["timestamp"].iloc[i] / 1000.0).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                self.timestamps.append(ts)
                self.prices.append(float(df["close"].iloc[i]))
                val = rsi_series.iloc[i]
                self.rsis.append(float(val) if pd.notna(val) else 50.0)

        self.load_previous_trades()

    def start_led_thread(self):
        """Spawns a background thread to manage LED lighting states based on bot activity."""

        def led_worker():
            while True:
                try:
                    if not self.leds.enabled:
                        time.sleep(5)
                        continue

                    active_side = None
                    if self.active_orders:
                        first_order = next(iter(self.active_orders.values()))
                        active_side = first_order.get("side")

                    if active_side == "sell":
                        self.leds.animation_sell_position()
                    elif active_side == "buy":
                        self.leds.animation_buy_position()
                    else:
                        self.leds.animation_waiting_rsi()
                except Exception as e:
                    logging.error(f"Error in LED worker thread: {e}")
                    time.sleep(2)

        threading.Thread(target=led_worker, daemon=True).start()

    # --- Safe API Wrappers with Retries ---

    @retry_api_call(retries=3, delay=2, backoff=2)
    def load_markets_safe(self):
        return self.exchange.load_markets()

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_trading_fees_safe(self):
        return self.exchange.fetch_trading_fees()

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_initial_candles(self):
        try:
            candles = self.exchange.fetch_ohlcv(
                self.kraken_symbol, timeframe=self.interval, limit=150
            )
            for c in candles:
                self.candle_buffer.append(c)
        except Exception as e:
            logging.error(f"Error fetching initial candles: {e}")

    @retry_api_call(retries=3, delay=2, backoff=2)
    def calculate_rsi(self):
        """Fetches fresh OHLCV candles and evaluates the current RSI value."""
        try:
            market_symbol = self.kraken_symbol
            if hasattr(self.exchange, "market_id"):
                market_symbol = (
                    self.exchange.market_id(self.kraken_symbol) or self.kraken_symbol
                )

            candles = self.exchange.fetch_ohlcv(
                market_symbol, timeframe=self.interval, limit=10
            )
            if candles is None:
                return None
            for c in candles:
                if not c or any(x is None for x in c):
                    continue
                if not self.candle_buffer or c[0] > self.candle_buffer[-1][0]:
                    self.candle_buffer.append(c)
                elif self.candle_buffer and c[0] == self.candle_buffer[-1][0]:
                    self.candle_buffer[-1] = c

            if len(self.candle_buffer) < self.rsi_period + 1:
                return None

            df = pd.DataFrame(
                list(self.candle_buffer),
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["close"] = pd.to_numeric(df["close"])
            delta = df["close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
            rs = gain / loss
            rsi_series = 100 - (100 / (1 + rs))
            rsi_series = rsi_series.mask((gain == 0) & (loss == 0), 50)
            rsi_series = rsi_series.fillna(50)
            return float(rsi_series.iloc[-1])
        except Exception as e:
            logging.error(f"Error calculating RSI: {e}")
            return None

    @retry_api_call(retries=3, delay=2, backoff=2)
    def update_balances(self):
        """Fetches account balances while correctly handling Kraken asset prefixes."""
        if self.paper_trading:
            bot_state["balance_quote"] = self.current_quote_balance
            bot_state["balance_asset"] = self.current_crypto_balance
            return
        try:
            balance = self.exchange.fetch_balance()
            free_balances = balance.get("free", {})

            q_keys = [
                self.quote_symbol,
                f"Z{self.quote_symbol}",
                f"X{self.quote_symbol}",
                "USDC",
            ]
            quote_val = 0.0
            for k in q_keys:
                if val := free_balances.get(k):
                    quote_val = float(val)
                    break

            b_keys = [
                self.base_symbol,
                f"X{self.base_symbol}",
                f"Z{self.base_symbol}",
                "XBT",
                "XXBT",
            ]
            crypto_val = 0.0
            for k in b_keys:
                if val := free_balances.get(k):
                    crypto_val = float(val)
                    break

            if crypto_val != self.current_crypto_balance:
                if crypto_val >= self.min_crypto_trade:
                    self.skip_sell_due_to_dust = False

            self.current_crypto_balance = crypto_val
            self.current_quote_balance = quote_val

            bot_state["balance_quote"] = self.current_quote_balance
            bot_state["balance_asset"] = self.current_crypto_balance
        except Exception as e:
            logging.error(f"Failed to update balances: {e}")

    @retry_api_call(retries=3, delay=2, backoff=2)
    def cancel_order_safe(self, order_id, symbol):
        return self.exchange.cancel_order(order_id, symbol)

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_order_safe(self, order_id, symbol):
        return self.exchange.fetch_order(order_id, symbol)

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_current_price(self):
        ticker = self.exchange.fetch_ticker(self.kraken_symbol)
        return float(ticker["last"]) if ticker and "last" in ticker else None

    @retry_api_call(retries=3, delay=2, backoff=2)
    def create_order_safe(self, symbol, type_arg, side, amount, price, params=None):
        return self.exchange.create_order(
            symbol=symbol,
            type=type_arg,
            side=side,
            amount=amount,
            price=price,
            params=params,
        )

    # --- Cost basis / profit tracking helper (NEW) ---

    def _update_cost_basis_on_fill(self, side, quantity, filled_price, fee):
        """Update avg-entry / position-cost / position-volume after a confirmed fill."""
        if side == "buy":
            self.position_cost_usdc += (quantity * filled_price) + fee
            self.position_volume += quantity
            if self.highest_price_since_entry == 0.0:
                self.highest_price_since_entry = filled_price
            else:
                self.highest_price_since_entry = max(
                    self.highest_price_since_entry, filled_price
                )
        else:  # sell
            if self.position_volume > 0:
                fraction = min(quantity / self.position_volume, 1.0)
                self.position_cost_usdc *= 1.0 - fraction
                self.position_volume -= quantity
            if self.position_volume <= 1e-12 or self.position_cost_usdc < 0.01:
                self.position_cost_usdc = 0.0
                self.position_volume = 0.0
                self.avg_entry_price = 0.0
                self.highest_price_since_entry = 0.0

        if self.position_volume > 0:
            self.avg_entry_price = self.position_cost_usdc / self.position_volume

    def _evaluate_exit(self, price):
        """
        Returns (should_sell, reason). Applies the profit-chasing / stop-loss
        gate. This is what the original script was missing.
        """
        if self.position_volume <= 0 or self.avg_entry_price <= 0:
            # No tracked position -> allow exit (defensive fallback)
            return True, "no-tracked-position (fallback)"

        fee_round_trip = self.estimated_fee_rate * 2
        breakeven_price = self.position_cost_usdc / (
            self.position_volume * (1.0 - self.estimated_fee_rate)
        )
        target_sell_price = self.avg_entry_price * (
            1.0 + self.min_profit_pct + fee_round_trip
        )
        stop_price = self.avg_entry_price * (1.0 - self.stop_loss_pct)

        if price <= stop_price:
            return True, (
                f"stop-loss (price {price:.2f} <= stop {stop_price:.2f}; "
                f"avg entry {self.avg_entry_price:.2f})"
            )
        if price >= target_sell_price:
            return True, (
                f"take-profit (price {price:.2f} >= target {target_sell_price:.2f}; "
                f"avg entry {self.avg_entry_price:.2f})"
            )
        if price >= breakeven_price:
            return True, (
                f"above-breakeven (price {price:.2f} >= breakeven "
                f"{breakeven_price:.2f})"
            )
        return False, (
            f"below-breakeven (price {price:.2f} < breakeven "
            f"{breakeven_price:.2f}; target {target_sell_price:.2f}; "
            f"stop {stop_price:.2f})"
        )

    # --- Order Management & Lifecycle ---

    def check_filled_orders(self):
        """Checks if active limit orders have successfully filled on the exchange.

        In paper mode, simulates a fill on the next cycle at the order's limit price.
        """
        for order_id, order_info in list(self.active_orders.items()):
            # --- PAPER MODE: simulate a fill ---
            if self.paper_trading:
                side = order_info["side"]
                filled_price = order_info["price"]
                filled_quantity = order_info["quantity"]
                fee = filled_price * filled_quantity * self.estimated_fee_rate

                if side == "buy":
                    self.current_quote_balance -= (filled_quantity * filled_price) + fee
                    self.current_crypto_balance += filled_quantity
                else:
                    self.current_quote_balance += (filled_quantity * filled_price) - fee
                    self.current_crypto_balance -= filled_quantity

                self._update_cost_basis_on_fill(
                    side, filled_quantity, filled_price, fee
                )
                self.total_fees_paid += fee
                self.trades.append((pd.Timestamp.now(), filled_price, side))
                self.last_trade_time = datetime.now()

                emoji = "🟢" if side == "buy" else "🔴"
                logging.info(
                    f"[PAPER FILL] {emoji} {side.upper()} {filled_quantity:.8f} "
                    f"{self.base_symbol} @ {filled_price:.2f} (fee {fee:.4f})"
                )
                del self.active_orders[order_id]
                continue

            # --- LIVE MODE: check real order status ---
            try:
                order = self.fetch_order_safe(order_id, self.kraken_symbol)
                if order["status"] == "closed":
                    side = order_info["side"]
                    filled_price = (
                        float(order["price"])
                        if order.get("price")
                        else float(order.get("average", 0))
                    )
                    filled_quantity = float(order["filled"])
                    fee = (
                        float(order["fee"]["cost"])
                        if order.get("fee") and order["fee"].get("cost")
                        else 0.0
                    )

                    self._update_cost_basis_on_fill(
                        side, filled_quantity, filled_price, fee
                    )
                    self.total_fees_paid += fee
                    self.update_balances()
                    self.trades.append((pd.Timestamp.now(), filled_price, side))
                    self.last_trade_time = datetime.now()

                    emoji = "🟢" if side == "buy" else "🔴"
                    color = 0x00B894 if side == "buy" else 0xD63031
                    fields = [
                        {
                            "name": "Filled Price",
                            "value": f"{filled_price:.2f} {self.quote_symbol}",
                            "inline": True,
                        },
                        {
                            "name": "Quantity",
                            "value": f"{filled_quantity:.8f} {self.base_symbol}",
                            "inline": True,
                        },
                        {
                            "name": "Fee",
                            "value": f"{fee:.4f} {self.quote_symbol}",
                            "inline": True,
                        },
                    ]
                    logging.info(
                        f"Filled {side} order at price {filled_price} for "
                        f"{filled_quantity} {self.base_symbol}"
                    )
                    send_discord_notification(
                        f"{emoji} {side.upper()} Filled",
                        f"**{side.upper()}** {filled_quantity:.8f} "
                        f"{self.base_symbol} @ {filled_price:.2f} {self.quote_symbol}",
                        color=color,
                        fields=fields,
                    )
                    del self.active_orders[order_id]
            except Exception as e:
                logging.warning(f"Could not check order {order_id}: {e}")

    def check_and_cancel_stale_orders(self):
        """Cancels orders older than the defined timeout window."""
        if self.paper_trading:
            return
        current_time = datetime.now()
        for order_id, order_info in list(self.active_orders.items()):
            if (current_time - order_info["time"]) > timedelta(
                minutes=self.order_timeout_minutes
            ):
                try:
                    self.cancel_order_safe(order_id, self.kraken_symbol)
                except Exception as e:
                    error_msg = str(e)
                    if (
                        "EOrder:Unknown order" in error_msg
                        or "Order not found" in error_msg
                    ):
                        logging.warning(
                            f"Order {order_id} already gone on Kraken. Purging."
                        )
                    else:
                        logging.error(f"Failed to cancel order {order_id}: {e}")

                if order_id in self.active_orders:
                    del self.active_orders[order_id]

    def load_previous_trades(self):
        try:
            if not os.path.exists(self.log_file):
                return
            with open(self.log_file, "r") as f:
                for line in f:
                    if "Filled" in line and "order" in line:
                        try:
                            parts = line.split(" - ")
                            timestamp = datetime.strptime(
                                parts[0], "%Y-%m-%d %H:%M:%S,%f"
                            )
                            # Log line format: "Filled <side> order at price <price> for <qty> <base>"
                            side = line.split("Filled ")[1].split(" order")[0].strip()
                            price = float(line.split("at price ")[1].split()[0])
                            self.trades.append((pd.Timestamp(timestamp), price, side))
                        except Exception:
                            continue
        except Exception as e:
            logging.error(f"Error loading trades: {e}")

    def calculate_pnl(self):
        current_price = self.fetch_current_price()
        if current_price is None:
            return 0.0, 0.0
        current_value = self.current_quote_balance + (
            self.current_crypto_balance * current_price
        )
        initial_value = (
            self.initial_valuation_balance
            if self.initial_valuation_balance > 0
            else 1.0
        )
        pnl_quote = current_value - initial_value
        pnl_percent = (pnl_quote / initial_value * 100) if initial_value > 0 else 0.0
        return pnl_quote, pnl_percent

    def execute_trade(self, side, amount_to_use, urgent=False):
        """Places buy or sell limit orders.

        `urgent=True` crosses the spread and drops `postOnly` so stop-loss
        and take-profit exits fill immediately.
        """
        try:
            current_price = self.fetch_current_price()
            if not current_price:
                logging.error("Could not fetch current price for trade execution.")
                return

            # --- Determine limit price ---
            if urgent:
                if side == "sell":
                    target_price = current_price * 0.998
                else:
                    target_price = current_price * 1.002
            else:
                if side == "sell":
                    target_price = current_price * (1.0 + self.price_adjustment)
                else:
                    target_price = current_price * (1.0 - self.price_adjustment)
                    if target_price >= current_price:
                        target_price = current_price * 0.999

            if side == "buy":
                budget = (
                    min(amount_to_use, self.current_quote_balance)
                    if self.paper_trading
                    else (
                        self.current_quote_balance
                        if self.position_percentage >= 100
                        else min(amount_to_use, self.current_quote_balance)
                    )
                )
                if budget < self.min_quote_trade:
                    logging.warning(
                        f"Quote balance ({budget:.2f} {self.quote_symbol}) below "
                        f"minimum trade requirement ({self.min_quote_trade})."
                    )
                    return

                raw_quantity = budget / target_price
                quantity = self.format_quantity(raw_quantity)
                if (quantity * target_price) < self.min_quote_trade:
                    return

                if self.paper_trading:
                    order_id = f"paper_buy_{int(time.time() * 1000)}"
                    self.active_orders[order_id] = {
                        "side": "buy",
                        "price": target_price,
                        "quantity": quantity,
                        "time": datetime.now(),
                    }
                else:
                    try:
                        quantity = float(
                            self.exchange.amount_to_precision(
                                self.kraken_symbol, raw_quantity
                            )
                        )
                        target_price = float(
                            self.exchange.price_to_precision(
                                self.kraken_symbol, target_price
                            )
                        )
                        params = {} if urgent else {"postOnly": True}
                        order = self.create_order_safe(
                            symbol=self.kraken_symbol,
                            type_arg="limit",
                            side="buy",
                            amount=quantity,
                            price=target_price,
                            params=params,
                        )
                        self.active_orders[order["id"]] = {
                            "side": "buy",
                            "price": target_price,
                            "quantity": quantity,
                            "time": datetime.now(),
                        }
                        logging.info(
                            f"Placed BUY limit order ID {order['id']} at target price: {target_price}"
                        )
                    except Exception as order_err:
                        logging.error(f"Kraken rejected BUY order: {order_err}")

            elif side == "sell":
                if not self.paper_trading and self.current_crypto_balance <= 0:
                    self.update_balances()

                available_crypto = self.current_crypto_balance
                if available_crypto <= 0:
                    logging.warning("No crypto balance available to sell.")
                    return

                raw_quantity = available_crypto
                order_notional_value = raw_quantity * current_price
                if (
                    order_notional_value < self.min_quote_trade
                    or raw_quantity < self.min_crypto_trade
                ):
                    if not self.skip_sell_due_to_dust:
                        logging.warning(
                            f"Skipping SELL: Position value "
                            f"({order_notional_value:.2f} {self.quote_symbol}) is "
                            f"below minimum quote trade requirement "
                            f"({self.min_quote_trade})."
                        )
                        self.skip_sell_due_to_dust = True
                    return

                if self.paper_trading:
                    order_id = f"paper_sell_{int(time.time() * 1000)}"
                    self.active_orders[order_id] = {
                        "side": "sell",
                        "price": target_price,
                        "quantity": raw_quantity,
                        "time": datetime.now(),
                    }
                else:
                    try:
                        quantity = float(
                            self.exchange.amount_to_precision(
                                self.kraken_symbol, raw_quantity
                            )
                        )
                        target_price = float(
                            self.exchange.price_to_precision(
                                self.kraken_symbol, target_price
                            )
                        )
                        params = {} if urgent else {"postOnly": True}
                        order = self.create_order_safe(
                            symbol=self.kraken_symbol,
                            type_arg="limit",
                            side="sell",
                            amount=quantity,
                            price=target_price,
                            params=params,
                        )
                        self.active_orders[order["id"]] = {
                            "side": "sell",
                            "price": target_price,
                            "quantity": quantity,
                            "time": datetime.now(),
                        }
                        logging.info(
                            f"Placed SELL limit order ID {order['id']} at target price: {target_price}"
                        )
                    except Exception as order_err:
                        logging.error(f"Kraken rejected SELL order: {order_err}")

        except Exception as e:
            logging.error(f"Error executing {side} trade: {e}")

    def format_quantity(self, quantity):
        try:
            return float(
                self.exchange.amount_to_precision(self.kraken_symbol, quantity)
            )
        except Exception:
            return round(quantity, 8)

    def trade_cycle(self):
        """Continuous loop running every minute to monitor market conditions and trade."""
        while True:
            try:
                self.update_balances()
                self.check_filled_orders()
                self.check_and_cancel_stale_orders()

                price = self.fetch_current_price()
                rsi = self.calculate_rsi()

                bot_state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if price:
                    bot_state["current_price"] = price
                if rsi:
                    bot_state["current_rsi"] = rsi

                logging.info(
                    f"Balances updated -> {self.quote_symbol}: "
                    f"{self.current_quote_balance:.2f}, {self.base_symbol}: "
                    f"{self.current_crypto_balance:.8f} | Price: {price} | RSI: {rsi}"
                )

                if rsi is not None and price:
                    self.prices.append(price)
                    self.rsis.append(rsi)
                    self.timestamps.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    self.prices = self.prices[-self.max_data_points :]
                    self.rsis = self.rsis[-self.max_data_points :]
                    self.timestamps = self.timestamps[-self.max_data_points :]

                    if self.trading_enabled and not self.active_orders:
                        cooldown_ok = (
                            self.last_trade_time is None
                            or (datetime.now() - self.last_trade_time).total_seconds()
                            > self.trade_cooldown_minutes * 60
                        )

                        if cooldown_ok:
                            # --- 1) Stop-loss: RSI-independent, urgent sell ---
                            if self.position_volume > 0 and self.avg_entry_price > 0:
                                stop_price = self.avg_entry_price * (
                                    1.0 - self.stop_loss_pct
                                )
                                if price <= stop_price:
                                    logging.info(
                                        f"[sell] STOP-LOSS | price={price:.2f} "
                                        f"stop={stop_price:.2f} "
                                        f"avg_entry={self.avg_entry_price:.2f}"
                                    )
                                    self.execute_trade(
                                        "sell",
                                        self.current_crypto_balance,
                                        urgent=True,
                                    )
                                    time.sleep(60)
                                    continue

                            # --- 2) Buy: only when flat ---
                            if (
                                rsi < self.rsi_low
                                and self.current_quote_balance >= self.min_quote_trade
                                and self.position_volume == 0
                            ):
                                logging.info(
                                    f"Oversold RSI triggered ({rsi:.2f} < "
                                    f"{self.rsi_low}). Executing BUY..."
                                )
                                budget = (
                                    self.current_quote_balance
                                    if self.position_percentage >= 100
                                    else self.trade_amount_quote
                                )
                                self.execute_trade("buy", budget)

                            # --- 3) Sell: RSI-driven, gated by profit/breakeven ---
                            elif (
                                rsi > self.rsi_high
                                and self.current_crypto_balance >= self.min_crypto_trade
                            ):
                                should_sell, reason = self._evaluate_exit(price)
                                if should_sell:
                                    logging.info(f"[sell] {reason} | RSI={rsi:.2f}")
                                    self.execute_trade(
                                        "sell",
                                        self.current_crypto_balance,
                                        urgent=True,
                                    )
                                else:
                                    logging.info(
                                        f"[risk] Skipping SELL (RSI={rsi:.2f}): "
                                        f"{reason}"
                                    )
                            else:
                                if (
                                    rsi > self.rsi_high
                                    and self.current_crypto_balance
                                    < self.min_crypto_trade
                                    and not self.skip_sell_due_to_dust
                                ):
                                    logging.warning(
                                        f"Skipping SELL: Balance "
                                        f"({self.current_crypto_balance}) is "
                                        f"below minimum requirement."
                                    )
                                    self.skip_sell_due_to_dust = True

                time.sleep(60)
            except Exception as e:
                logging.error(f"Error in trade cycle: {e}")
                time.sleep(10)


# ==========================================
# 5. FLASK WEB DASHBOARD ROUTES
# ==========================================
app = Flask(__name__)
app.static_folder = "static"

trader = RSITrader(
    API_KEY,
    API_SECRET,
    trading_pair=trading_pair_env,
    paper_trading=not live_trading_env,
)


@app.route("/")
def dashboard():
    pnl_quote, pnl_percent = trader.calculate_pnl()
    formatted_trades = []
    for trade_info in trader.trades[-50:]:
        formatted_trades.append(
            {
                "time": trade_info[0].strftime("%Y-%m-%d %H:%M:%S"),
                "type": trade_info[2].upper(),
                "price": f"{trade_info[1]:.2f}",
                "class": "positive" if trade_info[2] == "buy" else "negative",
            }
        )

    trading_data = formatted_trades[::-1]

    return render_template(
        "index-kraken.html",
        trading_data=trading_data,
        last_quote_balance=(
            float(trader.current_quote_balance) if trader.current_quote_balance else 0.0
        ),
        last_crypto_balance=(
            float(trader.current_crypto_balance)
            if trader.current_crypto_balance
            else 0.0
        ),
        prices=trader.prices[::-1],
        rsis=trader.rsis[::-1],
        rsi_values=trader.rsis[::-1],
        timestamps=trader.timestamps[::-1],
        trading_enabled=trader.trading_enabled,
        pnl_quote=float(pnl_quote) if pnl_quote else 0.0,
        pnl_percent=float(pnl_percent) if pnl_percent else 0.0,
        total_fees=float(trader.total_fees_paid) if trader.total_fees_paid else 0.0,
        base_symbol=trader.base_symbol,
        quote_symbol=trader.quote_symbol,
        order_timeout_minutes=trader.order_timeout_minutes,
        maker_fee=trader.maker_fee * 100,
        taker_fee=trader.taker_fee * 100,
        rsi_period=trader.rsi_period,
        overbought=trader.rsi_high,
        oversold=trader.rsi_low,
        interval=trader.interval,
        profit_percentage=trader.profit_percentage,
    )


@app.route("/update_data")
def update_data():
    pnl_quote, pnl_percent = trader.calculate_pnl()
    return jsonify(
        {
            "status": "success",
            "last_quote_balance": f"{trader.current_quote_balance:.2f}",
            "last_crypto_balance": f"{trader.current_crypto_balance:.8f}",
            "pnl_quote": f"{pnl_quote:.2f}",
            "pnl_percent": f"{pnl_percent:.2f}",
            "current_price": (f"{trader.prices[-1]:.2f}" if trader.prices else "0.00"),
            "current_rsi": f"{trader.rsis[-1]:.2f}" if trader.rsis else "0.00",
        }
    )


@app.route("/enable_trading/<int:enable>")
def enable_trading(enable):
    trader.trading_enabled = bool(enable)
    bot_state["trading_enabled"] = trader.trading_enabled
    logging.info(
        f"Trading state changed via web UI: "
        f"{'ENABLED' if trader.trading_enabled else 'DISABLED'}"
    )
    return jsonify({"status": "success", "trading_enabled": trader.trading_enabled})


@app.route("/set_order_timeout/<int:minutes>")
def set_order_timeout(minutes):
    if minutes in [1, 5, 10, 15, 30, 60]:
        trader.order_timeout_minutes = minutes
        bot_state["order_timeout_minutes"] = minutes
        logging.info(f"Order timeout updated via web UI to {minutes} minutes")
    return jsonify(
        {"status": "success", "order_timeout_minutes": trader.order_timeout_minutes}
    )


# ==========================================
# 6. APPLICATION ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not os.path.exists("templates"):
        os.makedirs("templates")

    try:
        quote_balance = trader.current_quote_balance
        crypto_balance = trader.current_crypto_balance
        balance_text = (
            f"**{trader.quote_symbol} Balance:** {quote_balance:.2f}\n"
            f"**{trader.base_symbol} Balance:** {crypto_balance:.8f}\n\n"
            f"⚙️ **Config:**\n"
            f"• Order Timeout: {trader.order_timeout_minutes} min\n"
            f"• RSI Triggers: Buy < {trader.rsi_low} | Sell > {trader.rsi_high}\n"
            f"• Profit Target: {trader.min_profit_pct * 100:.2f}% net\n"
            f"• Stop Loss: {trader.stop_loss_pct * 100:.2f}%"
        )
    except Exception as e:
        balance_text = f"Could not retrieve balances on startup: {e}"

    send_discord_notification(
        "₿ Kraken BTC/USDC Bot Started",
        f"The Kraken T-800 bot is online and monitoring BTC/USDC markets.\n\n{balance_text}",
        color=0x0984E3,
    )

    threading.Thread(target=trader.trade_cycle, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
