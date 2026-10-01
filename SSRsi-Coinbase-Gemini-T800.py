# SSRsi-Coinbase-Gemini-T800.py

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

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# ==========================================
# 0. LED CONTROLLER MODULE (T-800 Eyes)
# ==========================================
try:
    from rpi_ws281x import PixelStrip, Color

    LED_AVAILABLE = False
except ImportError:
    LED_AVAILABLE = False


class T800LEDController:

    def __init__(self, led_count=2, pin=18, brightness=10):
        self.enabled = LED_AVAILABLE
        if not self.enabled:
            return
        self.led_count = led_count
        self.pin = pin
        self.brightness = brightness
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
        except Exception:
            self.enabled = False

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
        self.fade_pulse(0, 0, 0, 150, 0, 0, steps=25, delay=0.0001)
        self.fade_pulse(150, 0, 0, 0, 0, 0, steps=25, delay=0.0001)
        self.fade_pulse(0, 0, 0, 0, 150, 0, steps=25, delay=0.0001)
        self.fade_pulse(0, 150, 0, 0, 0, 0, steps=25, delay=0.0001)

    def animation_sell_position(self):
        self.fade_pulse(0, 0, 0, 200, 0, 0, steps=30, delay=0.0005)
        self.fade_pulse(200, 0, 0, 0, 0, 0, steps=30, delay=0.0005)

    def animation_buy_position(self):
        self.fade_pulse(0, 0, 0, 0, 200, 0, steps=30, delay=0.0005)
        self.fade_pulse(0, 200, 0, 0, 0, 0, steps=30, delay=0.0005)


# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
API_KEY = os.getenv("COINBASE_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("COINBASE_API_SECRET", "YOUR_API_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

SYMBOL = "DOGE/USDC"
TIMEFRAME = "5m"
RSI_PERIOD = 14  # was 2 - RSI(2) is noise
RSI_LOW = 30
RSI_HIGH = 70  # was 80 - RSI(14) rarely reaches 80

PROFIT_PERCENTAGE = 0.75  # passive limit-order offset used by execute_trade
PRICE_ADJUSTMENT = 0.006  # legacy, unused
MIN_USDC_TRADE = 5.0
MIN_CRYPTO_TRADE = 1.0
POSITION_PERCENTAGE = 98
ORDER_TIMEOUT_MINUTES = 5

# --- Profit-chasing / risk-management (NEW) ---
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.015"))  # 1.5% net target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))  # 3% stop loss
ESTIMATED_FEE_RATE = float(os.getenv("ESTIMATED_FEE_RATE", "0.004"))  # 0.4% per side

live_trading_env = os.getenv("LIVE_TRADING", "false").lower() == "true"
trading_pair_env = SYMBOL

bot_state = {
    "status": "Running",
    "last_check": "Never",
    "current_price": 0.0,
    "current_rsi": 0.0,
    "balance_usdc": 0.0,
    "balance_asset": 0.0,
    "trading_enabled": True,
    "paper_trading": not live_trading_env,
    "order_timeout_minutes": ORDER_TIMEOUT_MINUTES,
    "active_orders": {},
    "positions": [],
    "recent_logs": [],
}


class NoRecursiveWarningsFilter(logging.Filter):

    def filter(self, record):
        return not record.getMessage().startswith("Skipping malformed log line")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("rsi_trading-coinbase.log", mode="a"),
        StreamHandler(),
    ],
)
logging.getLogger().addFilter(NoRecursiveWarningsFilter())


# ==========================================
# 2. ERROR RESILIENCE: RETRY DECORATOR
# ==========================================
def retry_api_call(retries=3, delay=2, backoff=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries:
                        raise e
                    time.sleep(current_delay)
                    current_delay *= backoff

        return wrapper

    return decorator


def send_discord_notification(title, description, color=0x00B894, fields=None):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": "RSI Trading Bot - Coinbase T-800"},
        }
        if fields:
            embed["fields"] = fields
        payload = {
            "embeds": [embed],
            "username": "RSI Trading Bot",
            "avatar_url": "https://cryptologos.cc/logos/dogecoin-doge-logo.png",
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        logging.error(f"Failed to send Discord notification: {e}")


# ==========================================
# 3. CORE TRADING CLASS
# ==========================================
class RSITrader:

    def __init__(self, api_key, secret_key, trading_pair=SYMBOL, paper_trading=True):
        self.paper_trading = paper_trading
        bot_state["paper_trading"] = self.paper_trading

        self.leds = T800LEDController()
        self.active_orders = {}
        self.start_led_thread()

        self.exchange = ccxt.coinbase(
            {
                "apiKey": api_key,
                "secret": secret_key,
                "enableRateLimit": True,
                "timeout": 30000,
            }
        )
        self.load_markets_safe()

        cleaned_pair = trading_pair.upper().strip()
        base_symbol = (
            cleaned_pair.split("/")[0] if "/" in cleaned_pair else cleaned_pair[:-4]
        )
        quote_symbol = (
            cleaned_pair.split("/")[1] if "/" in cleaned_pair else cleaned_pair[-4:]
        )

        self.display_symbol = base_symbol
        potential_symbols = [f"{base_symbol}/{quote_symbol}", cleaned_pair]

        self.coinbase_symbol = None
        for sym in potential_symbols:
            if sym in self.exchange.markets:
                self.coinbase_symbol = sym
                break
        if not self.coinbase_symbol:
            self.coinbase_symbol = "DOGE/USDC"

        try:
            if not self.paper_trading:
                all_fees = self.fetch_trading_fees_safe()
                market_fees = all_fees.get(self.coinbase_symbol, {})
                self.maker_fee = market_fees.get("maker", 0.0040)
                self.taker_fee = market_fees.get("taker", 0.0060)
            else:
                self.maker_fee = 0.0040
                self.taker_fee = 0.0060
        except Exception:
            self.maker_fee = 0.0040
            self.taker_fee = 0.0060

        self.rsi_period = RSI_PERIOD
        self.rsi_low = RSI_LOW
        self.rsi_high = RSI_HIGH
        self.interval = TIMEFRAME
        self.trades = []
        self.log_file = "rsi_trading-coinbase.log"
        self.profit_percentage = PROFIT_PERCENTAGE
        self.price_adjustment = self.profit_percentage / 100.0

        self.min_usdc_trade = MIN_USDC_TRADE
        self.min_crypto_trade = MIN_CRYPTO_TRADE
        self.position_percentage = POSITION_PERCENTAGE

        self.skip_sell_due_to_dust = False
        self.initial_usdc_only_balance = 0.0
        self.trading_enabled = True
        bot_state["trading_enabled"] = self.trading_enabled
        self.order_timeout_minutes = ORDER_TIMEOUT_MINUTES

        bot_state["active_orders"] = self.active_orders

        self.candle_buffer = deque(maxlen=150)
        self.fetch_initial_candles()

        self.current_usdc_balance = 100.0 if self.paper_trading else 0.0
        self.current_crypto_balance = 0.0 if self.paper_trading else 0.0
        self.total_fees_paid = 0.0

        if not self.paper_trading:
            self.update_balances()
        else:
            bot_state["balance_usdc"] = self.current_usdc_balance
            bot_state["balance_asset"] = self.current_crypto_balance

        self.initial_usdc_balance = self.current_usdc_balance
        self.initial_crypto_balance = self.current_crypto_balance
        self.last_trade_time = None
        self.trade_cooldown_minutes = 5  # was 1 - too short

        # --- Profit-chasing / cost basis state (NEW) ---
        self.estimated_fee_rate = ESTIMATED_FEE_RATE
        self.min_profit_pct = MIN_PROFIT_PCT
        self.stop_loss_pct = STOP_LOSS_PCT

        self.avg_entry_price = 0.0
        self.position_cost_usdc = 0.0
        self.position_volume = 0.0
        self.highest_price_since_entry = 0.0

        # Seed cost basis if we already hold crypto
        if self.current_crypto_balance > 0:
            seed_price = self.fetch_current_price()
            if seed_price and seed_price > 0:
                self.position_volume = self.current_crypto_balance
                self.position_cost_usdc = self.current_crypto_balance * seed_price
                self.avg_entry_price = seed_price
                self.highest_price_since_entry = seed_price
                logging.info(
                    f"[init] Seeded position: {self.position_volume:.4f} "
                    f"{self.display_symbol} @ {seed_price:.5f}"
                )

        # PnL baseline (no more 0.10 fallback)
        current_price = self.fetch_current_price()
        if current_price and current_price > 0:
            self.initial_usdc_only_balance = self.current_usdc_balance + (
                self.current_crypto_balance * current_price
            )
        else:
            self.initial_usdc_only_balance = self.current_usdc_balance
            logging.warning(
                "[init] Could not fetch initial price; PnL baseline uses USDC only"
            )

        self.prices = []
        self.rsis = []
        self.timestamps = []
        self.max_data_points = 100

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
                except Exception:
                    time.sleep(2)

        threading.Thread(target=led_worker, daemon=True).start()

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
                self.coinbase_symbol, timeframe=self.interval, limit=150
            )
            for c in candles:
                self.candle_buffer.append(c)
        except Exception as e:
            logging.error(f"Error fetching initial candles: {e}")

    @retry_api_call(retries=3, delay=2, backoff=2)
    def calculate_rsi(self):
        try:
            candles = self.exchange.fetch_ohlcv(
                self.coinbase_symbol, timeframe=self.interval, limit=10
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
        if self.paper_trading:
            bot_state["balance_usdc"] = self.current_usdc_balance
            bot_state["balance_asset"] = self.current_crypto_balance
            return
        try:
            params = {"v3": True, "limit": 250}
            balance = self.exchange.fetch_balance(params)
            crypto_key = self.display_symbol.upper()
            free = balance.get("free", {})
            crypto_val = float(free.get(crypto_key, 0.0) or 0.0)
            usdc_val = float(free.get("USDC", 0.0) or 0.0)

            if crypto_val != self.current_crypto_balance:
                if crypto_val >= self.min_crypto_trade:
                    self.skip_sell_due_to_dust = False

            self.current_crypto_balance = crypto_val
            self.current_usdc_balance = usdc_val
            bot_state["balance_usdc"] = self.current_usdc_balance
            bot_state["balance_asset"] = self.current_crypto_balance
        except Exception as e:
            logging.error(f"Failed to update balances: {e}")

    @retry_api_call(retries=3, delay=2, backoff=2)
    def cancel_order_safe(self, order_id, symbol):
        return self.exchange.cancel_order(order_id, symbol)

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_order_safe(self, order_id, symbol):
        return self.exchange.fetch_order(order_id, symbol)

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

    def check_filled_orders(self):
        for order_id, order_info in list(self.active_orders.items()):
            # --- PAPER MODE: simulate a fill ---
            if self.paper_trading:
                side = order_info["side"]
                filled_price = order_info["price"]
                filled_quantity = order_info["quantity"]
                fee = filled_price * filled_quantity * self.estimated_fee_rate

                if side == "buy":
                    self.current_usdc_balance -= (filled_quantity * filled_price) + fee
                    self.current_crypto_balance += filled_quantity
                else:
                    self.current_usdc_balance += (filled_quantity * filled_price) - fee
                    self.current_crypto_balance -= filled_quantity

                self._update_cost_basis_on_fill(
                    side, filled_quantity, filled_price, fee
                )
                self.total_fees_paid += fee
                self.trades.append((pd.Timestamp.now(), filled_price, side))
                self.last_trade_time = datetime.now()
                emoji = "🟢" if side == "buy" else "🔴"
                logging.info(
                    f"[PAPER FILL] {emoji} {side.upper()} {filled_quantity:.4f} "
                    f"{self.display_symbol} @ {filled_price:.5f} (fee {fee:.4f})"
                )
                del self.active_orders[order_id]
                continue

            # --- LIVE MODE: check real order status ---
            try:
                order = self.fetch_order_safe(order_id, self.coinbase_symbol)
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
                            "value": f"{filled_price:.4f} USDC",
                            "inline": True,
                        },
                        {
                            "name": "Quantity",
                            "value": f"{filled_quantity:.4f} {self.display_symbol}",
                            "inline": True,
                        },
                        {"name": "Fee", "value": f"{fee:.4f} USDC", "inline": True},
                    ]
                    send_discord_notification(
                        f"{emoji} {side.upper()} Filled",
                        f"**{side.upper()}** {filled_quantity:.4f} "
                        f"{self.display_symbol} @ {filled_price:.4f} USDC",
                        color=color,
                        fields=fields,
                    )
                    del self.active_orders[order_id]
            except Exception as e:
                logging.warning(f"Could not check order {order_id}: {e}")

    def check_and_cancel_stale_orders(self):
        current_time = datetime.now()
        for order_id, order_info in list(self.active_orders.items()):
            if (current_time - order_info["time"]) > timedelta(
                minutes=self.order_timeout_minutes
            ):
                try:
                    if not self.paper_trading:
                        self.cancel_order_safe(order_id, self.coinbase_symbol)
                    logging.info(f"Successfully canceled stale order {order_id}.")
                except Exception as e:
                    logging.error(f"Failed to cancel order {order_id}: {e}")

                if order_id in self.active_orders:
                    del self.active_orders[order_id]

    def load_previous_trades(self):
        try:
            if not os.path.exists(self.log_file):
                return
            with open(self.log_file, "r") as f:
                for line in f:
                    if "Executed" in line and "order" in line:
                        try:
                            parts = line.split(" - ")
                            timestamp = datetime.strptime(
                                parts[0], "%Y-%m-%d %H:%M:%S,%f"
                            )
                            side = line.split("Executed ")[1].split(" order")[0].strip()
                            price = float(line.split("price: ")[1].split()[0])
                            self.trades.append((pd.Timestamp(timestamp), price, side))
                        except Exception:
                            continue
        except Exception as e:
            logging.error(f"Error loading trades: {e}")

    def calculate_pnl(self):
        current_price = self.fetch_current_price()
        if current_price is None:
            return 0.0, 0.0
        current_value = self.current_usdc_balance + (
            self.current_crypto_balance * current_price
        )
        initial_value = (
            self.initial_usdc_only_balance
            if self.initial_usdc_only_balance > 0
            else self.initial_usdc_balance
        )
        pnl_usdc = current_value - initial_value
        pnl_percent = (pnl_usdc / initial_value * 100) if initial_value > 0 else 0.0
        return pnl_usdc, pnl_percent

    @retry_api_call(retries=3, delay=2, backoff=2)
    def fetch_current_price(self):
        ticker = self.exchange.fetch_ticker(self.coinbase_symbol)
        return float(ticker["last"]) if ticker and "last" in ticker else None

    @retry_api_call(retries=3, delay=2, backoff=2)
    def create_order_safe(self, symbol, type_arg, side, amount, price):
        return self.exchange.create_order(
            symbol=symbol, type=type_arg, side=side, amount=amount, price=price
        )

    def execute_trade(self, side, amount_to_use, urgent=False):
        """
        Place a limit order. When `urgent=True`, cross the spread aggressively
        so the order fills on the next tick (used for profit-taking & stop-loss).
        """
        try:
            current_price = self.fetch_current_price()
            if not current_price:
                return

            # --- Determine limit price ---
            if urgent:
                # Cross spread for a near-guaranteed fill
                if side == "sell":
                    target_price = current_price * 0.998
                else:
                    target_price = current_price * 1.002
            else:
                # Passive limit orders (existing behaviour)
                if side == "sell":
                    target_price = current_price * (1.0 + self.price_adjustment)
                else:
                    target_price = current_price * (1.0 - self.price_adjustment)

            if side == "buy":
                budget = (
                    min(amount_to_use, self.current_usdc_balance)
                    if self.paper_trading
                    else self.current_usdc_balance
                )
                if budget < self.min_usdc_trade:
                    logging.warning(
                        f"USDC balance ({budget:.2f}) below minimum trade requirement."
                    )
                    return

                raw_quantity = (
                    budget * (self.position_percentage / 100.0)
                ) / target_price
                quantity = self.format_quantity(raw_quantity)

                if (quantity * target_price) < self.min_usdc_trade:
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
                        order = self.create_order_safe(
                            symbol=self.coinbase_symbol,
                            type_arg="limit",
                            side="buy",
                            amount=quantity,
                            price=target_price,
                        )
                        self.active_orders[order["id"]] = {
                            "side": "buy",
                            "price": target_price,
                            "quantity": quantity,
                            "time": datetime.now(),
                        }
                    except Exception as order_err:
                        logging.error(f"Coinbase rejected BUY order: {order_err}")

            elif side == "sell":
                if not self.paper_trading and self.current_crypto_balance <= 0:
                    self.update_balances()

                available_crypto = self.current_crypto_balance
                if available_crypto <= 0:
                    return

                raw_quantity = available_crypto * (self.position_percentage / 100.0)
                quantity = self.format_quantity(raw_quantity)

                if quantity < self.min_crypto_trade:
                    if not self.skip_sell_due_to_dust:
                        self.skip_sell_due_to_dust = True
                    return

                if self.paper_trading:
                    order_id = f"paper_sell_{int(time.time() * 1000)}"
                    self.active_orders[order_id] = {
                        "side": "sell",
                        "price": target_price,
                        "quantity": quantity,
                        "time": datetime.now(),
                    }
                else:
                    try:
                        order = self.create_order_safe(
                            symbol=self.coinbase_symbol,
                            type_arg="limit",
                            side="sell",
                            amount=quantity,
                            price=target_price,
                        )
                        self.active_orders[order["id"]] = {
                            "side": "sell",
                            "price": target_price,
                            "quantity": quantity,
                            "time": datetime.now(),
                        }
                    except Exception as order_err:
                        logging.error(f"Coinbase rejected SELL order: {order_err}")
        except Exception as e:
            logging.error(f"Error executing {side} trade: {e}")

    def format_quantity(self, quantity):
        try:
            return float(
                self.exchange.amount_to_precision(self.coinbase_symbol, quantity)
            )
        except Exception:
            return round(quantity, 2)

    def _evaluate_exit(self, price):
        """
        Returns (should_sell, reason). Applies the profit-chasing / stop-loss
        gate that the original script was missing.
        """
        if self.position_volume <= 0 or self.avg_entry_price <= 0:
            # No tracked position -> fall back to a plain breakeven-less sell
            return True, "no-tracked-position"

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
                f"stop-loss (price {price:.5f} <= stop {stop_price:.5f}; "
                f"avg entry {self.avg_entry_price:.5f})"
            )
        if price >= target_sell_price:
            return True, (
                f"take-profit (price {price:.5f} >= target {target_sell_price:.5f}; "
                f"avg entry {self.avg_entry_price:.5f})"
            )
        if price >= breakeven_price:
            return True, (
                f"above-breakeven (price {price:.5f} >= breakeven "
                f"{breakeven_price:.5f})"
            )
        return False, (
            f"below-breakeven (price {price:.5f} < breakeven "
            f"{breakeven_price:.5f}; target {target_sell_price:.5f}; "
            f"stop {stop_price:.5f})"
        )

    def trade_cycle(self):
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
                                        f"[sell] STOP-LOSS | price={price:.5f} "
                                        f"stop={stop_price:.5f} "
                                        f"avg_entry={self.avg_entry_price:.5f}"
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
                                and self.current_usdc_balance >= self.min_usdc_trade
                                and self.position_volume == 0
                            ):
                                logging.info(
                                    f"[buy] RSI={rsi:.1f} < {self.rsi_low} | "
                                    f"budget=${self.current_usdc_balance:.2f}"
                                )
                                self.execute_trade("buy", self.current_usdc_balance)

                            # --- 3) Sell: RSI-driven, gated by profit/breakeven ---
                            elif (
                                rsi > self.rsi_high
                                and self.current_crypto_balance >= self.min_crypto_trade
                            ):
                                should_sell, reason = self._evaluate_exit(price)
                                if should_sell:
                                    logging.info(f"[sell] {reason} | RSI={rsi:.1f}")
                                    self.execute_trade(
                                        "sell",
                                        self.current_crypto_balance,
                                        urgent=True,
                                    )
                                else:
                                    logging.info(
                                        f"[risk] Skipping SELL (RSI={rsi:.1f}): "
                                        f"{reason}"
                                    )

                time.sleep(60)
            except Exception as e:
                logging.error(f"Error in trade cycle: {e}")
                time.sleep(10)


# ==========================================
# 4. FLASK WEB DASHBOARD ROUTES
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
    pnl_usdc, pnl_percent = trader.calculate_pnl()
    formatted_trades = []
    for trade_info in trader.trades[-50:]:
        formatted_trades.append(
            {
                "time": trade_info[0].strftime("%Y-%m-%d %H:%M:%S"),
                "type": trade_info[2].upper(),
                "price": f"{trade_info[1]:.4f}",
                "class": "positive" if trade_info[2] == "buy" else "negative",
            }
        )

    trading_data = formatted_trades[::-1]

    return render_template(
        "index-coinbase.html",
        trading_data=trading_data,
        last_quote_balance=(
            float(trader.current_usdc_balance) if trader.current_usdc_balance else 0.0
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
        pnl_quote=float(pnl_usdc) if pnl_usdc else 0.0,
        pnl_percent=float(pnl_percent) if pnl_percent else 0.0,
        total_fees=float(trader.total_fees_paid) if trader.total_fees_paid else 0.0,
        base_symbol=trader.display_symbol,
        quote_symbol="USDC",
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
    pnl_usdc, pnl_percent = trader.calculate_pnl()
    return jsonify(
        {
            "status": "success",
            "last_quote_balance": f"{trader.current_usdc_balance:.2f}",
            "last_crypto_balance": f"{trader.current_crypto_balance:.4f}",
            "pnl_quote": f"{pnl_usdc:.2f}",
            "pnl_percent": f"{pnl_percent:.2f}",
            "current_price": (
                f"{trader.prices[-1]:.4f}" if trader.prices else "0.0000"
            ),
            "current_rsi": f"{trader.rsis[-1]:.2f}" if trader.rsis else "0.00",
        }
    )


@app.route("/enable_trading/<int:enable>")
def enable_trading(enable):
    trader.trading_enabled = bool(enable)
    bot_state["trading_enabled"] = trader.trading_enabled
    return jsonify({"status": "success", "trading_enabled": trader.trading_enabled})


@app.route("/set_order_timeout/<int:minutes>")
def set_order_timeout(minutes):
    if minutes in [1, 5, 10, 15, 30, 60]:
        trader.order_timeout_minutes = minutes
        bot_state["order_timeout_minutes"] = minutes
    return jsonify(
        {"status": "success", "order_timeout_minutes": trader.order_timeout_minutes}
    )


if __name__ == "__main__":
    if not os.path.exists("templates"):
        os.makedirs("templates")

    try:
        usdc_balance = trader.current_usdc_balance
        doge_balance = trader.current_crypto_balance
        balance_text = (
            f"**USDC Balance:** ${usdc_balance:.2f}\n"
            f"**DOGE Balance:** {doge_balance:.4f}\n\n"
            f"⚙️ **Config:**\n"
            f"• Order Timeout: {trader.order_timeout_minutes} min\n"
            f"• RSI Triggers: Buy < {trader.rsi_low} | Sell > {trader.rsi_high}\n"
            f"• Profit Target: {trader.min_profit_pct * 100:.2f}% net\n"
            f"• Stop Loss: {trader.stop_loss_pct * 100:.2f}%"
        )
    except Exception as e:
        balance_text = f"Could not retrieve balances on startup: {e}"

    send_discord_notification(
        "Ð Coinbase Bot Started (DOGE/USDC)",
        f"The Coinbase T-800 bot is online and monitoring DOGE/USDC markets.\n\n{balance_text}",
        color=0x0984E3,
    )

    threading.Thread(target=trader.trade_cycle, daemon=True).start()
    app.run(host="0.0.0.0", port=5010, debug=False)
