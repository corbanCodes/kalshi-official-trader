#!/usr/bin/env python3
"""
Kalshi Official Paper Trader - Using PyKalshi

YOUR STRATEGY:
- Wait 10 minutes into the 15-minute window (5 min remaining)
- When YES or NO hits 85c, bet WITH the favorite
- Don't buy above 95c
- Martingale on losses (with cap)
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List

from pykalshi import KalshiClient, MarketStatus, Action, Side, OrderType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Files shared with dashboard
SETTINGS_FILE = "trader_settings.json"
STATE_FILE = "trader_state.json"
TRADE_HISTORY_FILE = "trade_history.json"
ORDERBOOK_HISTORY_FILE = "orderbook_history.json"

SERIES_TICKER = "KXBTC15M"

DEFAULT_SETTINGS = {
    "trading_enabled": False,
    "use_demo": True,
    "min_wait_minutes": 10,
    "odds_threshold": 85,
    "max_entry_price": 95,
    "use_martingale": True,
    "martingale_cap": 4,
    "bet_mode": "percent",
    "bet_percent": 10,
    "flat_bet_size": 100,
    "order_type": "limit",
    "starting_bankroll": 10000,
}


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
        except:
            pass
    return DEFAULT_SETTINGS.copy()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def save_trade_history(trades: list):
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(trades[-500:], f, indent=2)


def save_orderbook_snapshot(ticker: str, yes_levels: list, no_levels: list):
    history = []
    if os.path.exists(ORDERBOOK_HISTORY_FILE):
        try:
            with open(ORDERBOOK_HISTORY_FILE) as f:
                history = json.load(f)
        except:
            pass

    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "yes": yes_levels[:20],
        "no": no_levels[:20],
    })

    with open(ORDERBOOK_HISTORY_FILE, "w") as f:
        json.dump(history[-1000:], f)


class PyKalshiTrader:
    """
    Trading bot using PyKalshi client.
    Much simpler and more reliable than hand-rolled WebSocket.
    """

    def __init__(self):
        self.client: Optional[KalshiClient] = None
        self.settings = load_settings()

        # State
        self.bankroll = self.settings.get("starting_bankroll", 10000)
        self.current_market = None
        self.current_close_time = None
        self.trades: List[Dict] = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.traded_windows = set()

    def _reload_settings(self):
        self.settings = load_settings()

    def start(self):
        """Initialize PyKalshi client"""
        self._reload_settings()
        use_demo = self.settings.get("use_demo", True)

        logger.info(f"Starting PyKalshi Trader")
        logger.info(f"  Environment: {'DEMO' if use_demo else 'PRODUCTION'}")
        logger.info(f"  Strategy: Wait {self.settings['min_wait_minutes']} min, threshold {self.settings['odds_threshold']}c")
        logger.info(f"  Martingale: {self.settings['use_martingale']}, cap: {self.settings['martingale_cap']}")
        logger.info(f"  Bet mode: {self.settings['bet_mode']}")

        # Initialize client
        self.client = KalshiClient(demo=use_demo)

        # Check connection
        status = self.client.exchange.get_status()
        logger.info(f"Exchange status: {status}")

        # Check balance
        try:
            balance = self.client.portfolio.get_balance()
            logger.info(f"Account balance: ${balance.balance / 100:.2f}")
            self.bankroll = balance.balance / 100
        except Exception as e:
            logger.warning(f"Could not get balance: {e}")

    def get_current_market(self) -> Optional[str]:
        """Find current BTC 15-min market"""
        try:
            markets = self.client.get_markets(
                series_ticker=SERIES_TICKER,
                status=MarketStatus.OPEN,
                limit=1
            )
            if markets:
                market = markets[0]
                self.current_market = market.ticker
                self.current_close_time = market.close_time
                return market.ticker
        except Exception as e:
            logger.error(f"Error getting market: {e}")
        return None

    def get_orderbook(self, ticker: str) -> dict:
        """Get orderbook with analytics"""
        try:
            market = self.client.get_market(ticker)
            ob_response = market.get_orderbook(depth=20)

            # OrderbookResponse has .orderbook with .yes and .no as list[tuple[int, int]]
            # Also has convenience properties like best_yes_bid, best_no_bid, spread, mid
            raw_yes = ob_response.orderbook.yes if ob_response.orderbook and ob_response.orderbook.yes else []
            raw_no = ob_response.orderbook.no if ob_response.orderbook and ob_response.orderbook.no else []

            # Convert tuples to lists for JSON serialization
            yes_levels = [[price, qty] for price, qty in raw_yes]
            no_levels = [[price, qty] for price, qty in raw_no]

            # Save for history export
            save_orderbook_snapshot(ticker, yes_levels, no_levels)

            # Use OrderbookResponse convenience properties
            return {
                "yes": yes_levels,
                "no": no_levels,
                "spread": ob_response.spread,
                "mid": ob_response.mid,
                "yes_bid": ob_response.best_yes_bid or 0,
                "no_bid": ob_response.best_no_bid or 0,
                "yes_ask": ob_response.best_yes_ask or 0,
                "no_ask": 100 - (ob_response.best_yes_bid or 0) if ob_response.best_yes_bid else 0,
            }
        except Exception as e:
            logger.error(f"Error getting orderbook: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"yes": [], "no": []}

    def get_mins_left(self) -> Optional[float]:
        """Get minutes remaining until market close"""
        if not self.current_close_time:
            return None
        try:
            # Handle different close_time formats
            if isinstance(self.current_close_time, str):
                close_time = datetime.fromisoformat(self.current_close_time.replace("Z", "+00:00"))
            else:
                close_time = self.current_close_time

            now = datetime.now(timezone.utc)
            diff = (close_time - now).total_seconds()
            return diff / 60
        except Exception as e:
            logger.warning(f"Could not calculate mins left: {e}")
            return None

    def calculate_bet_size(self) -> float:
        """Calculate bet size based on mode and martingale"""
        if self.settings["bet_mode"] == "percent":
            base_bet = self.bankroll * (self.settings["bet_percent"] / 100)
        else:
            base_bet = self.settings["flat_bet_size"]

        if self.settings["use_martingale"] and self.consecutive_losses > 0:
            multiplier = min(2 ** self.consecutive_losses, 2 ** self.settings["martingale_cap"])
            bet = base_bet * multiplier
            bet = min(bet, self.bankroll * 0.5)
            logger.info(f"Martingale: {self.consecutive_losses} losses, {multiplier}x, bet ${bet:.2f}")
            return bet

        return base_bet

    def check_entry(self, ob: dict) -> Optional[dict]:
        """Check if we should enter a trade"""
        if not self.settings.get("trading_enabled", False):
            return None

        mins_left = self.get_mins_left()
        if mins_left is None:
            return None

        mins_elapsed = 15 - mins_left
        if mins_elapsed < self.settings["min_wait_minutes"]:
            return None

        if mins_left < 0.5:
            return None  # Too close to expiry

        # Check window already traded
        window_id = self.current_market.split("-")[-1] if self.current_market else ""
        if window_id in self.traded_windows:
            return None

        threshold = self.settings["odds_threshold"]
        max_price = self.settings["max_entry_price"]

        yes_ask = ob.get("yes_ask", 0)
        no_ask = ob.get("no_ask", 0)

        direction = None
        entry_price = None

        # YES is favorite (high yes_bid means YES likely to win)
        if yes_ask >= threshold and yes_ask <= max_price:
            direction = "yes"
            entry_price = yes_ask
        elif no_ask >= threshold and no_ask <= max_price:
            direction = "no"
            entry_price = no_ask
        else:
            return None

        bet_size = self.calculate_bet_size()
        contracts = int(bet_size * 100 / entry_price)

        if contracts < 1:
            return None

        return {
            "direction": direction,
            "entry_price": entry_price,
            "contracts": contracts,
            "bet_size": bet_size,
            "mins_left": mins_left,
            "window_id": window_id,
        }

    def place_order(self, signal: dict) -> bool:
        """Place an order"""
        try:
            logger.info(
                f"PLACING ORDER: {signal['direction'].upper()} @ {signal['entry_price']}c "
                f"x{signal['contracts']} (${signal['bet_size']:.2f})"
            )

            order = self.client.portfolio.place_order(
                ticker=self.current_market,
                action=Action.BUY,
                side=Side.YES if signal["direction"] == "yes" else Side.NO,
                count=signal["contracts"],
                yes_price=signal["entry_price"] if signal["direction"] == "yes" else None,
                no_price=signal["entry_price"] if signal["direction"] == "no" else None,
                type=OrderType.LIMIT if self.settings["order_type"] == "limit" else OrderType.MARKET,
            )

            logger.info(f"Order placed: {order.order_id} - {order.status}")

            # Record trade
            trade = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ticker": self.current_market,
                "window_id": signal["window_id"],
                "side": signal["direction"],
                "entry_price": signal["entry_price"],
                "fill_price": None,  # Will be updated on fill
                "contracts": signal["contracts"],
                "bet_size": signal["bet_size"],
                "outcome": None,
                "profit": None,
            }
            self.trades.append(trade)
            save_trade_history(self.trades)

            self.traded_windows.add(signal["window_id"])
            return True

        except Exception as e:
            logger.error(f"Order failed: {e}")
            return False

    def update_dashboard_state(self, ob: dict):
        """Save state for dashboard display"""
        mins_left = self.get_mins_left()
        state = {
            "bankroll": self.bankroll,
            "wins": self.wins,
            "losses": self.losses,
            "current_market": self.current_market,
            "connected": True,
            "last_update": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "mins_left": round(mins_left, 1) if mins_left else None,
            "spread": ob.get("spread"),
            "mid": ob.get("mid"),
            "yes_bid": ob.get("yes_bid"),
            "no_bid": ob.get("no_bid"),
            "yes_ask": ob.get("yes_ask"),
            "no_ask": ob.get("no_ask"),
            "orderbook": {
                "yes": ob.get("yes", []),
                "no": ob.get("no", []),
            }
        }
        save_state(state)

    def run_loop(self):
        """Main polling loop"""
        logger.info("Starting main trading loop...")
        poll_count = 0

        while True:
            try:
                # Reload settings
                self._reload_settings()

                # Get current market
                ticker = self.get_current_market()
                if not ticker:
                    logger.warning("No open market found, waiting...")
                    time.sleep(10)
                    continue

                # Get orderbook
                ob = self.get_orderbook(ticker)

                # Log every 10 polls
                poll_count += 1
                if poll_count % 10 == 0:
                    mins_left = self.get_mins_left()
                    logger.info(
                        f"[POLL #{poll_count}] {ticker}: "
                        f"YES bid={ob.get('yes_bid', 0)}c, NO bid={ob.get('no_bid', 0)}c, "
                        f"spread={ob.get('spread', '?')}c, {mins_left:.1f}min left"
                    )

                # Update dashboard
                self.update_dashboard_state(ob)

                # Check for entry
                signal = self.check_entry(ob)
                if signal:
                    self.place_order(signal)

                time.sleep(1)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Loop error: {e}")
                time.sleep(5)


def main():
    """Entry point"""
    # Check credentials
    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not api_key:
        logger.error("Missing KALSHI_API_KEY_ID!")
        logger.error("Set environment variables:")
        logger.error("  KALSHI_API_KEY_ID=your-key-id")
        logger.error("  KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem")
        logger.error("  OR KALSHI_PRIVATE_KEY=<key contents>")
        return

    # If private key is provided as content (not path), write to temp file
    if private_key and not private_key_path:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
            # Handle escaped newlines
            key_content = private_key.replace("\\n", "\n")
            f.write(key_content)
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = f.name
            logger.info(f"Wrote private key to temp file: {f.name}")

    trader = PyKalshiTrader()
    trader.start()
    trader.run_loop()


if __name__ == "__main__":
    main()
