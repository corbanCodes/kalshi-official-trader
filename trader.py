#!/usr/bin/env python3
"""
Kalshi Official Paper Trader

YOUR STRATEGY:
- Wait 10 minutes into the 15-minute window (5 min remaining)
- When YES or NO hits 85c, bet WITH the favorite
- Don't buy above 95c
- Martingale on losses (with cap)
- Bet % of bankroll for compounding

This connects to:
- Demo API for order placement (paper trading)
- Real-time WebSocket for full orderbook data
"""

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List

from kalshi_client import KalshiRestClient, KalshiWebSocket
from config import STRATEGY, SERIES_TICKER, STATE_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Settings file shared with dashboard
SETTINGS_FILE = "trader_settings.json"
TRADE_HISTORY_FILE = "trade_history.json"
ORDERBOOK_HISTORY_FILE = "orderbook_history.json"

DEFAULT_SETTINGS = {
    "trading_enabled": False,
    "use_demo": True,
    "use_production_prices": True,  # Use real orderbook even in paper trading
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
    """Load settings from file (shared with dashboard)"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
        except:
            pass
    return DEFAULT_SETTINGS.copy()


def save_trade_history(trades: list):
    """Save trade history for dashboard"""
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(trades[-500:], f, indent=2)  # Keep last 500


def save_orderbook_snapshot(ticker: str, yes_levels: list, no_levels: list):
    """Save orderbook snapshot for history export"""
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

    # Keep last 1000 snapshots
    with open(ORDERBOOK_HISTORY_FILE, "w") as f:
        json.dump(history[-1000:], f)


class OfficialTrader:
    """
    Production-ready trader using Kalshi Sandbox API.

    Connects via authenticated WebSocket for full orderbook,
    places orders via REST API.

    Reads settings from dashboard file so changes take effect live.
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
    ):
        self.api_key_id = api_key_id
        self.private_key_pem = private_key_pem

        # Clients
        self.rest: Optional[KalshiRestClient] = None
        self.ws: Optional[KalshiWebSocket] = None

        # Load settings from dashboard file
        self._reload_settings()

        # State
        self.current_market: Optional[str] = None
        self.current_close_time: str = ""
        self.pending_order: Optional[Dict] = None
        self.trades: List[Dict] = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.traded_windows = set()
        self.last_settings_check = datetime.now(timezone.utc)

        # Real-time data
        self.current_ticker_data: Dict = {}
        self.orderbook_data: Dict = {}

    def _reload_settings(self):
        """Load settings from dashboard file"""
        settings = load_settings()
        self.use_demo = settings.get("use_demo", True)
        self.use_production_prices = settings.get("use_production_prices", True)
        self.trading_enabled = settings.get("trading_enabled", False)
        self.min_wait_minutes = settings.get("min_wait_minutes", 10)
        self.odds_threshold = settings.get("odds_threshold", 85)
        self.max_entry_price = settings.get("max_entry_price", 95)
        self.use_martingale = settings.get("use_martingale", True)
        self.martingale_cap = settings.get("martingale_cap", 4)
        self.bet_mode = settings.get("bet_mode", "percent")
        self.bet_percent = settings.get("bet_percent", 10) / 100  # Convert to decimal
        self.flat_bet_size = settings.get("flat_bet_size", 100)
        self.order_type = settings.get("order_type", "limit")
        self.bankroll = settings.get("starting_bankroll", 10000)
        self.initial_bankroll = self.bankroll

    async def start(self):
        """Initialize clients and connect"""
        self._reload_settings()

        # Determine which API to use for prices
        # use_production_prices=True means get REAL orderbook from production
        # use_demo=True means DON'T place real orders (paper trade)
        price_source = "PRODUCTION" if self.use_production_prices else "DEMO"
        order_target = "DEMO (paper)" if self.use_demo else "PRODUCTION (real $$$)"

        logger.info(f"Starting Official Trader")
        logger.info(f"  Price source: {price_source}")
        logger.info(f"  Order target: {order_target}")
        logger.info(f"Settings:")
        logger.info(f"  - Wait {self.min_wait_minutes} min, threshold {self.odds_threshold}c")
        logger.info(f"  - Martingale: {self.use_martingale}, cap: {self.martingale_cap}")
        logger.info(f"  - Bet mode: {self.bet_mode}, percent: {self.bet_percent*100}%, flat: ${self.flat_bet_size}")
        logger.info(f"  - Trading enabled: {self.trading_enabled}")

        # REST client for PRICES (use production if use_production_prices=True)
        self.rest = KalshiRestClient(
            self.api_key_id,
            self.private_key_pem,
            use_demo=not self.use_production_prices,  # False = production
        )
        await self.rest.start()
        logger.info(f"REST client for prices: {'PRODUCTION' if self.use_production_prices else 'DEMO'}")

        # Check balance (from demo account if paper trading)
        if self.use_demo:
            try:
                demo_rest = KalshiRestClient(
                    self.api_key_id,
                    self.private_key_pem,
                    use_demo=True,
                )
                await demo_rest.start()
                balance = await demo_rest.get_balance()
                await demo_rest.close()
                logger.info(f"Demo account balance: ${balance.available_balance / 100:.2f}")
            except Exception as e:
                logger.warning(f"Could not fetch demo balance: {e}")

        # WebSocket client for real-time data (same source as REST)
        self.ws = KalshiWebSocket(
            self.api_key_id,
            self.private_key_pem,
            use_demo=not self.use_production_prices,
        )

        # Set up handlers
        self.ws.on_ticker = self._on_ticker
        self.ws.on_trade = self._on_trade
        self.ws.on_orderbook_snapshot = self._on_orderbook_snapshot
        self.ws.on_orderbook_delta = self._on_orderbook_delta
        self.ws.on_fill = self._on_fill

        await self.ws.connect()
        logger.info("Trader started")

    async def stop(self):
        """Shutdown"""
        if self.ws:
            await self.ws.close()
        if self.rest:
            await self.rest.close()
        self._save_state()
        logger.info("Trader stopped")

    # =========================================================================
    # REAL-TIME DATA HANDLERS
    # =========================================================================

    def _on_ticker(self, data: Dict):
        """Handle ticker update"""
        ticker = data.get("market_ticker")
        if ticker:
            self.current_ticker_data[ticker] = data
            # Check for trade opportunity
            asyncio.create_task(self._check_entry(ticker, data))

    def _on_trade(self, data: Dict):
        """Handle public trade"""
        # Log significant trades
        count = data.get("count", 0)
        if count >= 100:
            logger.info(
                f"[TRADE] {data.get('taker_side', '?').upper()} "
                f"@ {data.get('yes_price', '?')}c x{count}"
            )

    def _on_orderbook_snapshot(self, data: Dict):
        """Handle full orderbook"""
        ticker = data.get("market_ticker")
        if ticker:
            yes_levels = data.get("yes", [])
            no_levels = data.get("no", [])
            logger.info(
                f"[ORDERBOOK] {ticker}: "
                f"{len(yes_levels)} YES levels, {len(no_levels)} NO levels"
            )

            # Save to history for export
            save_orderbook_snapshot(ticker, yes_levels, no_levels)

            # Update state for dashboard
            self._save_state_for_dashboard(yes_levels, no_levels)

            # Log top 3 levels
            if yes_levels:
                logger.info("  YES bids (top 3):")
                for lvl in yes_levels[:3]:
                    logger.info(f"    {lvl[0]}c: {lvl[1]:,} contracts")
            if no_levels:
                logger.info("  NO bids (top 3):")
                for lvl in no_levels[:3]:
                    logger.info(f"    {lvl[0]}c: {lvl[1]:,} contracts")

    def _on_orderbook_delta(self, data: Dict):
        """Handle orderbook update"""
        pass  # Deltas are handled internally by the WS client

    def _on_fill(self, data: Dict):
        """Handle our order fill"""
        logger.info(f"[FILL] Order filled: {data}")
        # Process the fill for P&L tracking
        asyncio.create_task(self._process_fill(data))

    # =========================================================================
    # STRATEGY LOGIC
    # =========================================================================

    async def _check_entry(self, ticker: str, data: Dict):
        """Check if we should enter a trade"""
        # Reload settings every 5 seconds to pick up dashboard changes
        now = datetime.now(timezone.utc)
        if (now - self.last_settings_check).total_seconds() > 5:
            self._reload_settings()
            self.last_settings_check = now

        # Skip if trading is disabled
        if not self.trading_enabled:
            return

        # Skip if we have a pending order
        if self.pending_order:
            return

        # Skip if already traded this window
        window_id = self._get_window_id(ticker)
        if window_id in self.traded_windows:
            return

        # Check timing (need to be past min_wait_minutes)
        mins_left = self._get_mins_left(ticker)
        if mins_left is None:
            return

        mins_elapsed = 15 - mins_left
        if mins_elapsed < self.min_wait_minutes:
            return

        if mins_left < 0.5:
            return  # Too close to expiry

        # Get prices
        yes_ask = data.get("yes_ask", 0)
        no_ask = data.get("no_ask", 0)

        # Check if either side hits threshold
        direction = None
        entry_price = None

        if yes_ask >= self.odds_threshold and yes_ask <= self.max_entry_price:
            direction = "yes"
            entry_price = yes_ask
        elif no_ask >= self.odds_threshold and no_ask <= self.max_entry_price:
            direction = "no"
            entry_price = no_ask
        else:
            return  # No entry signal

        # Calculate bet size
        bet_size = self._calculate_bet_size()

        # Calculate contracts
        contracts = int(bet_size * 100 / entry_price)
        if contracts < 1:
            logger.warning(f"Bet size too small for {contracts} contracts")
            return

        # Check liquidity from orderbook
        available = self._check_liquidity(ticker, direction, entry_price)
        if available < contracts:
            logger.warning(
                f"Insufficient liquidity: need {contracts}, available {available}"
            )
            contracts = min(contracts, available)
            if contracts < 1:
                return

        logger.info(
            f"ENTRY SIGNAL: {direction.upper()} @ {entry_price}c x{contracts} "
            f"(bet ${bet_size:.2f}, {mins_left:.1f} min left)"
        )

        # Place the order
        await self._place_order(ticker, direction, entry_price, contracts, bet_size)

    def _calculate_bet_size(self) -> float:
        """Calculate bet size based on mode (percent or flat) with martingale"""
        # Base bet depends on mode
        if self.bet_mode == "percent":
            base_bet = self.bankroll * self.bet_percent
        else:
            base_bet = self.flat_bet_size

        # Apply martingale if enabled and we have losses
        if self.use_martingale and self.consecutive_losses > 0:
            multiplier = min(2 ** self.consecutive_losses, 2 ** self.martingale_cap)
            bet = base_bet * multiplier
            # Don't bet more than we have
            bet = min(bet, self.bankroll * 0.5)
            logger.info(
                f"Martingale: {self.consecutive_losses} losses, "
                f"multiplier {multiplier}x, bet ${bet:.2f}"
            )
            return bet

        return base_bet

    def _check_liquidity(self, ticker: str, side: str, max_price: int) -> int:
        """Check available liquidity from orderbook"""
        if ticker not in self.ws.orderbooks:
            return 0

        book = self.ws.orderbooks[ticker].get(side, [])
        total = 0
        for price, qty in book:
            if price <= max_price:
                total += qty
        return total

    async def _place_order(
        self,
        ticker: str,
        side: str,
        price: int,
        contracts: int,
        bet_size: float,
    ):
        """Place a limit order"""
        try:
            if side == "yes":
                order = await self.rest.create_order(
                    ticker=ticker,
                    side="yes",
                    action="buy",
                    count=contracts,
                    order_type="limit",
                    yes_price=price,
                )
            else:
                order = await self.rest.create_order(
                    ticker=ticker,
                    side="no",
                    action="buy",
                    count=contracts,
                    order_type="limit",
                    no_price=price,
                )

            logger.info(f"Order placed: {order.order_id} - {order.status}")

            self.pending_order = {
                "order_id": order.order_id,
                "ticker": ticker,
                "side": side,
                "price": price,
                "contracts": contracts,
                "bet_size": bet_size,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            window_id = self._get_window_id(ticker)
            self.traded_windows.add(window_id)

        except Exception as e:
            logger.error(f"Failed to place order: {e}")

    async def _process_fill(self, data: Dict):
        """Process our fill for P&L and slippage tracking"""
        if not self.pending_order:
            return

        # Extract fill details
        fill_price = data.get("yes_price") or data.get("no_price") or 0
        fill_count = data.get("count", 0)

        order = self.pending_order
        entry_price = order.get("price", 0)
        slippage = fill_price - entry_price

        logger.info(
            f"FILL: Entry {entry_price}c -> Fill {fill_price}c "
            f"(slippage: {slippage:+d}c) x{fill_count}"
        )

        # Record the trade with slippage
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": order.get("ticker"),
            "window_id": self._get_window_id(order.get("ticker", "")),
            "side": order.get("side"),
            "entry_price": entry_price,
            "fill_price": fill_price,
            "slippage": slippage,
            "contracts": fill_count,
            "bet_size": order.get("bet_size"),
            "outcome": None,  # Will be set when market settles
            "profit": None,
            "bankroll_after": None,
        }

        self.trades.append(trade)
        save_trade_history(self.trades)

        # Clear pending order
        self.pending_order = None
        logger.info(f"Trade recorded, awaiting settlement")

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _get_window_id(self, ticker: str) -> str:
        """Extract window ID from ticker"""
        # KXBTC15M-26MAR012300 -> 26MAR012300
        parts = ticker.split("-")
        return parts[-1] if len(parts) > 1 else ticker

    def _get_mins_left(self, ticker: str) -> Optional[float]:
        """Get minutes remaining until market close"""
        if not self.current_close_time:
            return None

        try:
            close_time = datetime.fromisoformat(self.current_close_time.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff = (close_time - now).total_seconds()
            return diff / 60  # Convert to minutes
        except Exception as e:
            logger.warning(f"Could not calculate mins left: {e}")
            return None

    def _save_state(self):
        """Save state to file"""
        state = {
            "bankroll": self.bankroll,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "trades": self.trades[-100:],  # Keep last 100
            "traded_windows": list(self.traded_windows)[-50:],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"State saved to {STATE_FILE}")

    def _save_state_for_dashboard(self, yes_levels: list = None, no_levels: list = None):
        """Save state for dashboard display"""
        state = {
            "bankroll": self.bankroll,
            "wins": self.wins,
            "losses": self.losses,
            "current_market": self.current_market,
            "connected": self.ws.connected if self.ws else False,
            "last_update": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "orderbook": {
                "yes": yes_levels or [],
                "no": no_levels or [],
            }
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def _load_state(self):
        """Load state from file"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self.bankroll = state.get("bankroll", self.bankroll)
                self.wins = state.get("wins", 0)
                self.losses = state.get("losses", 0)
                self.consecutive_losses = state.get("consecutive_losses", 0)
                self.trades = state.get("trades", [])
                self.traded_windows = set(state.get("traded_windows", []))
                logger.info(f"Loaded state: ${self.bankroll:.2f}, {self.wins}W/{self.losses}L")
            except Exception as e:
                logger.warning(f"Could not load state: {e}")

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    async def run(self):
        """Main trading loop with REST polling fallback"""
        self._load_state()

        # Find current market
        markets = await self.rest.get_markets(series_ticker=SERIES_TICKER, status="open", limit=1)
        if not markets:
            logger.error("No open BTC 15-min markets found")
            return

        self.current_market = markets[0]["ticker"]
        self.current_close_time = markets[0].get("close_time", "")
        logger.info(f"Current market: {self.current_market}")
        logger.info(f"Close time: {self.current_close_time}")

        # Subscribe to WebSocket channels
        await self.ws.subscribe_all([self.current_market])

        # Run both WebSocket listener AND REST polling in parallel
        logger.info("Starting WebSocket listener + REST polling (1s interval)...")
        await asyncio.gather(
            self.ws.listen(),
            self._rest_polling_loop(),
        )

    async def _rest_polling_loop(self):
        """Poll REST API every second for orderbook data (fallback for empty WebSocket)"""
        logger.info("REST polling loop started")
        poll_count = 0

        while True:
            try:
                # Reload settings
                self._reload_settings()

                # Check if market is still valid (hasn't expired)
                await self._check_market_expiry()

                # Get orderbook via REST
                try:
                    orderbook = await self.rest.get_orderbook(self.current_market, depth=20)
                    yes_levels = [[lvl.price, lvl.quantity] for lvl in orderbook.yes]
                    no_levels = [[lvl.price, lvl.quantity] for lvl in orderbook.no]

                    # Log every 10 polls
                    poll_count += 1
                    if poll_count % 10 == 0:
                        top_yes = yes_levels[0] if yes_levels else [0, 0]
                        top_no = no_levels[0] if no_levels else [0, 0]
                        logger.info(
                            f"[POLL #{poll_count}] {self.current_market}: "
                            f"YES {top_yes[0]}c ({top_yes[1]:,}), NO {top_no[0]}c ({top_no[1]:,})"
                        )

                    # Save for dashboard
                    save_orderbook_snapshot(self.current_market, yes_levels, no_levels)
                    self._save_state_for_dashboard(yes_levels, no_levels)

                    # Store in WebSocket's orderbook dict so strategy can use it
                    self.ws.orderbooks[self.current_market] = {
                        "yes": yes_levels,
                        "no": no_levels,
                    }

                    # Build ticker-like data from orderbook for strategy check
                    if yes_levels and no_levels:
                        ticker_data = {
                            "market_ticker": self.current_market,
                            "yes_bid": yes_levels[0][0] if yes_levels else 0,
                            "no_bid": no_levels[0][0] if no_levels else 0,
                            "yes_ask": 100 - no_levels[0][0] if no_levels else 0,
                            "no_ask": 100 - yes_levels[0][0] if yes_levels else 0,
                        }
                        self.current_ticker_data[self.current_market] = ticker_data

                        # Check for entry
                        await self._check_entry(self.current_market, ticker_data)

                except Exception as e:
                    logger.warning(f"REST poll error: {e}")

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                await asyncio.sleep(5)

    async def _check_market_expiry(self):
        """Check if current market has expired and switch to next one"""
        try:
            from datetime import datetime
            import re

            # Parse close time
            if self.current_close_time:
                close_time = datetime.fromisoformat(self.current_close_time.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)

                # If market expired or will expire in 30s, get next market
                if (close_time - now).total_seconds() < 30:
                    logger.info("Market expiring, fetching next market...")
                    markets = await self.rest.get_markets(
                        series_ticker=SERIES_TICKER, status="open", limit=1
                    )
                    if markets and markets[0]["ticker"] != self.current_market:
                        self.current_market = markets[0]["ticker"]
                        self.current_close_time = markets[0].get("close_time", "")
                        self.traded_windows = set()  # Reset for new market
                        logger.info(f"Switched to new market: {self.current_market}")

                        # Resubscribe WebSocket
                        await self.ws.subscribe_all([self.current_market])

        except Exception as e:
            logger.warning(f"Market expiry check failed: {e}")


async def main():
    """Entry point"""
    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not api_key or not private_key:
        logger.error("Missing credentials!")
        logger.error("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY")
        logger.error("")
        logger.error("To get credentials:")
        logger.error("1. Go to https://demo.kalshi.co/ for demo")
        logger.error("2. Account Settings -> API Keys -> Create New")
        logger.error("3. Save the private key immediately (shown only once)")
        sys.exit(1)

    # Settings (including demo/production) loaded from dashboard settings file
    trader = OfficialTrader(api_key, private_key)

    try:
        await trader.start()
        await trader.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await trader.stop()


if __name__ == "__main__":
    asyncio.run(main())
