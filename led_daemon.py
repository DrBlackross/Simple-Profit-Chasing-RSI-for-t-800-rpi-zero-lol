import logging
import os
import math
import time
import random

try:
    from rpi_ws281x import PixelStrip, Color
    import rpi_ws281x as ws

    LED_AVAILABLE = True
except ImportError:
    LED_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - LED_DAEMON - %(levelname)s - %(message)s"
)

# Configuration for SPI mode (Pin 19 / BCM 10 / MOSI)
LED_COUNT = 2
LED_PIN = 10  # SPI MOSI
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 25  # Keep global high, control via intensity ceiling
LED_INVERT = False
LED_CHANNEL = 0
strip_type = (ws.WS2811_STRIP_BRG,)

# KRAKEN_LOG = "rsi_trading-kraken.log"
# COINBASE_LOG = "rsi_trading-coinbase.log"
KRAKEN_LOG = "/home/drblackross/SSRsi/rsi_trading-kraken.log"
COINBASE_LOG = "/home/drblackross/SSRsi/rsi_trading-coinbase.log"


class DualEyeController:
    def __init__(self):
        self.enabled = LED_AVAILABLE
        self.eye_phases = [0.0, 0.0]

        if not self.enabled:
            logging.error("rpi_ws281x library missing. Running in mock mode.")
            return

        try:
            self.strip = PixelStrip(
                LED_COUNT,
                LED_PIN,
                LED_FREQ_HZ,
                LED_DMA,
                LED_INVERT,
                LED_BRIGHTNESS,
                LED_CHANNEL,
            )
            self.strip.begin()
            self._clear()
            logging.info(
                "T-800 Dual-Eye LED hardware initialized successfully via SPI."
            )
        except Exception as e:
            self.enabled = False
            logging.error(f"Failed to initialize LED strip: {e}")

    def _clear(self):
        if not self.enabled:
            return
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()

    def update_eye(self, eye_index, state):
        if not self.enabled:
            return

        phase = self.eye_phases[eye_index]
        state = state.lower()

        if state == "buying":
            # Green breathing
            MAX_INTENSITY = 20
            MIN_INTENSITY = 3
            intensity = int(
                (math.sin(phase) + 1) / 2 * (MAX_INTENSITY - MIN_INTENSITY)
                + MIN_INTENSITY
            )
            self.strip.setPixelColor(eye_index, Color(0, intensity, 0))
            self.eye_phases[eye_index] += 5.85

        elif state == "selling":
            # Red breathing with hardware threshold floor
            MAX_INTENSITY = 20
            MIN_INTENSITY = 15
            intensity = int(
                (math.sin(phase) + 1) / 2 * (MAX_INTENSITY - MIN_INTENSITY)
                + MIN_INTENSITY
            )
            self.strip.setPixelColor(eye_index, Color(intensity, 0, 0))
            self.eye_phases[eye_index] += 5.85

        else:
            if random.random() < 0.85:
                # Random red pulse
                intensity = random.randint(5, 20)
                self.strip.setPixelColor(eye_index, Color(intensity, 0, 0))
            else:
                # Random green pulse
                intensity = random.randint(3, 20)
                self.strip.setPixelColor(eye_index, Color(0, intensity, 0))
        self.strip.show()


def parse_log_state(filepath):
    if not os.path.exists(filepath):
        return "waiting"

    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
            if not lines:
                return "waiting"

            recent_lines = "".join(lines[-10:]).lower()

            if (
                "buying" in recent_lines
                or "oversold rsi triggered" in recent_lines
                or "order successfully placed" in recent_lines
            ):
                return "buying"
            elif (
                "selling" in recent_lines or "overbought rsi triggered" in recent_lines
            ):
                return "selling"
            else:
                return "waiting"
    except Exception:
        return "waiting"


def main():
    controller = DualEyeController()
    logging.info("T-800 LED Daemon started with SPI dual-eye animations...")

    while True:
        kraken_state = parse_log_state(KRAKEN_LOG)
        coinbase_state = parse_log_state(COINBASE_LOG)

        controller.update_eye(0, kraken_state)
        controller.update_eye(1, coinbase_state)

        time.sleep(0.05)


if __name__ == "__main__":
    main()
    # Waiting: Fast, erratic scanning effect between red and green
