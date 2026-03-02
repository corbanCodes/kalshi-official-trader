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


class OfficialTrader:
    """
    Production-ready trader using Kalshi Sandbox API.

    Connects via authenticated WebSocket for full orderbook,
    places orders via REST API.
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        use_demo: bool = True,
    ):
        self.use_demo = use_demo
        self.api_key_id = api_key_id
        self.private_key_pem = private_key_pem

        # Clients
        self.rest: Optional[KalshiRestClient] = None
        self.ws: Optional[KalshiWebSocket] = None

        # Strategy config
        self.strategy = STRATEGY
        self.min_wait_minutes = self.strategy["min_wait_minutes"]
        self.odds_threshold = self.strategy["odds_threshold"]
        self.max_entry_price = self.strategy["max_entry_price"]
        self.use_martingale = self.strategy["use_martingale"]
        self.martingale_cap = self.strategy["martingale_cap"]
        self.bet_pct = self.strategy["bet_pct_of_bankroll"]

        # State
        self.bankroll = self.strategy["starting_bankroll"]
        self.initial_bankroll = self.bankroll
        self.current_market: Optional[str] = None
        self.pending_order: Optional[Dict] = None
        self.trades: List[Dict] = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.traded_windows = set()

        # Real-time data
        self.current_ticker_data: Dict = {}
        self.orderbook_data: Dict = {}

    async def start(self):
        """Initialize clients and connect"""
        logger.info(f"Starting Official Trader (demo={self.use_demo})")
        logger.info(f"Strategy: {self.strategy['name']}")
        logger.info(f"  - Wait {self.min_wait_minutes} min, threshold {self.odds_threshold}c")
        logger.info(f"  - Martingale: {self.use_martingale}, cap: {self.martingale_cap}")
        logger.info(f"  - Bet %: {self.bet_pct * 100}%")

        # REST client for orders
        self.rest = KalshiRestClient(
            self.api_key_id,
            self.private_key_pem,
            use_demo=self.use_demo,
        )
        await self.rest.start()

        # Check balance
        try:
            balance = await self.rest.get_balance()
            logger.info(f"Account balance: ${balance.available_balance / 100:.2f}")
            if not self.use_demo:
                self.bankroll = balance.available_balance / 100
        except Exception as e:
            logger.warning(f"Could not fetch balance: {e}")

        # WebSocket client for real-time data
        self.ws = KalshiWebSocket(
            self.api_key_id,
            self.private_key_pem,
            use_demo=self.use_demo,
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
        """Calculate bet size with martingale"""
        base_bet = self.bankroll * self.bet_pct

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
        """Process our fill for P&L"""
        if not self.pending_order:
            return

        # TODO: Match fill to pending order and calculate P&L
        # For now, just log it
        logger.info(f"Processing fill: {data}")

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _get_window_id(self, ticker: str) -> str:
        """Extract window ID from ticker"""
        # KXBTC15M-26MAR012300 -> 26MAR012300
        parts = ticker.split("-")
        return parts[-1] if len(parts) > 1 else ticker

    def _get_mins_left(self, ticker: str) -> Optional[float]:
        """Get minutes remaining from ticker data"""
        # This should come from market close_time vs current time
        # For now, estimate from ticker data if available
        data = self.current_ticker_data.get(ticker, {})
        # TODO: Parse close_time and calculate
        return None  # Will implement with actual market data

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
        """Main trading loop"""
        self._load_state()

        # Find current market
        markets = await self.rest.get_markets(series_ticker=SERIES_TICKER, status="open", limit=1)
        if not markets:
            logger.error("No open BTC 15-min markets found")
            return

        self.current_market = markets[0]["ticker"]
        logger.info(f"Current market: {self.current_market}")
        logger.info(f"Close time: {markets[0].get('close_time', '?')}")

        # Subscribe to everything
        await self.ws.subscribe_all([self.current_market])

        # Listen for updates
        logger.info("Listening for trading opportunities...")
        await self.ws.listen()


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

    use_demo = os.environ.get("USE_PRODUCTION", "").lower() != "true"

    trader = OfficialTrader(api_key, private_key, use_demo=use_demo)

    try:
        await trader.start()
        await trader.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await trader.stop()


if __name__ == "__main__":
    asyncio.run(main())
