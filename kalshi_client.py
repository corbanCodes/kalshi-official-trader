"""
Kalshi Official API Client - Full Authentication + WebSocket + REST

This is the REAL client for:
- Authenticated WebSocket (orderbook_delta, fills, positions)
- REST API order placement
- RSA-PSS signing

Based on: https://docs.kalshi.com/getting_started/api_keys
WebSocket: https://docs.kalshi.com/getting_started/quick_start_websockets
"""

import os
import json
import time
import base64
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

import aiohttp
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OrderBookLevel:
    price: int  # cents 1-99
    quantity: int


@dataclass
class OrderBook:
    ticker: str
    yes: List[OrderBookLevel]
    no: List[OrderBookLevel]
    timestamp: str = ""


@dataclass
class Trade:
    trade_id: str
    ticker: str
    yes_price: int
    no_price: int
    count: int
    taker_side: str  # "yes" or "no"
    created_time: str


@dataclass
class Ticker:
    market_ticker: str
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    last_price: int
    volume: int
    open_interest: int
    timestamp: str


@dataclass
class Order:
    order_id: str
    ticker: str
    side: str
    action: str
    type: str
    status: str
    yes_price: int
    no_price: int
    count: int
    remaining_count: int
    created_time: str


@dataclass
class Fill:
    trade_id: str
    order_id: str
    ticker: str
    side: str
    action: str
    yes_price: int
    no_price: int
    count: int
    created_time: str
    is_taker: bool


@dataclass
class Position:
    ticker: str
    market_exposure: int
    realized_pnl: int


@dataclass
class Balance:
    available_balance: int  # cents
    portfolio_value: int


# =============================================================================
# RSA AUTHENTICATION
# =============================================================================

class KalshiAuth:
    """RSA-PSS authentication for Kalshi API"""

    def __init__(self, api_key_id: str, private_key_pem: str):
        self.api_key_id = api_key_id
        self._private_key = self._load_private_key(private_key_pem)

    @staticmethod
    def _load_private_key(pem_str: str) -> rsa.RSAPrivateKey:
        """Load RSA private key from PEM string"""
        # Handle escaped newlines from env vars
        pem_str = pem_str.replace("\\n", "\n")
        return serialization.load_pem_private_key(
            pem_str.encode("utf-8"),
            password=None,
            backend=default_backend()
        )

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Create RSA-PSS signature.

        Signature = sign(timestamp_ms + method + path_without_query)
        """
        path_clean = path.split("?")[0]
        message = (timestamp_ms + method + path_clean).encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def get_headers(self, method: str, path: str) -> Dict[str, str]:
        """Get authentication headers for a request"""
        ts = str(int(time.time() * 1000))
        sig = self.sign(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def get_ws_headers(self) -> Dict[str, str]:
        """Get authentication headers for WebSocket connection"""
        return self.get_headers("GET", "/trade-api/ws/v2")


# =============================================================================
# KALSHI REST CLIENT
# =============================================================================

class KalshiRestClient:
    """
    REST API client for Kalshi.

    Handles:
    - Market data (public, no auth)
    - Order placement (private, auth required)
    - Portfolio (private, auth required)
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        use_demo: bool = True,
    ):
        self.use_demo = use_demo
        self.base_url = (
            "https://demo-api.kalshi.co/trade-api/v2"
            if use_demo
            else "https://api.elections.kalshi.com/trade-api/v2"
        )
        self.auth = KalshiAuth(api_key_id, private_key_pem)
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """Start the client session"""
        self._session = aiohttp.ClientSession()
        logger.info(f"KalshiRestClient started (demo={self.use_demo})")

    async def close(self):
        """Close the client session"""
        if self._session:
            await self._session.close()
        logger.info("KalshiRestClient closed")

    async def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Make an API request"""
        url = f"{self.base_url}{path}"

        headers = {}
        if auth_required:
            headers = self.auth.get_headers(method.upper(), f"/trade-api/v2{path}")

        async with self._session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json()
            if resp.status >= 400:
                logger.error(f"API error {resp.status}: {body}")
                raise Exception(f"API error {resp.status}: {body}")
            return body

    # =========================================================================
    # MARKET DATA (public)
    # =========================================================================

    async def get_markets(
        self,
        series_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
    ) -> List[Dict]:
        """Get markets, optionally filtered by series"""
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self._request("GET", "/markets", params=params, auth_required=False)
        return data.get("markets", [])

    async def get_market(self, ticker: str) -> Dict:
        """Get a specific market"""
        data = await self._request("GET", f"/markets/{ticker}", auth_required=False)
        return data.get("market", {})

    async def get_orderbook(self, ticker: str, depth: int = 0) -> OrderBook:
        """
        Get current orderbook for a market.

        Args:
            ticker: Market ticker
            depth: 0 = all levels, 1-100 for specific depth
        """
        params = {"depth": depth} if depth > 0 else {}
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params=params, auth_required=True
        )
        ob = data.get("orderbook", {})

        return OrderBook(
            ticker=ticker,
            yes=[OrderBookLevel(price=lvl[0], quantity=lvl[1]) for lvl in ob.get("yes", [])],
            no=[OrderBookLevel(price=lvl[0], quantity=lvl[1]) for lvl in ob.get("no", [])],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
    ) -> List[Trade]:
        """
        Get public trade history (NO AUTH REQUIRED).

        This is ALL trades on the market, not just yours.
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts

        data = await self._request("GET", "/markets/trades", params=params, auth_required=False)

        return [
            Trade(
                trade_id=t.get("trade_id", ""),
                ticker=t.get("ticker", ""),
                yes_price=t.get("yes_price", 0),
                no_price=t.get("no_price", 0),
                count=t.get("count", 0),
                taker_side=t.get("taker_side", ""),
                created_time=t.get("created_time", ""),
            )
            for t in data.get("trades", [])
        ]

    # =========================================================================
    # ORDER MANAGEMENT (private)
    # =========================================================================

    async def create_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """
        Place an order.

        Args:
            ticker: Market ticker (e.g., "KXBTC15M-26MAR012300")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            order_type: "limit" or "market"
            yes_price: Price in cents (1-99) for limit orders
            no_price: Alternative price specification
            client_order_id: Idempotency key
        """
        import uuid

        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }

        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price

        data = await self._request("POST", "/portfolio/orders", json_body=body)
        o = data.get("order", {})

        return Order(
            order_id=o.get("order_id", ""),
            ticker=o.get("ticker", ""),
            side=o.get("side", ""),
            action=o.get("action", ""),
            type=o.get("type", ""),
            status=o.get("status", ""),
            yes_price=o.get("yes_price", 0),
            no_price=o.get("no_price", 0),
            count=o.get("count", 0),
            remaining_count=o.get("remaining_count", 0),
            created_time=o.get("created_time", ""),
        )

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an order"""
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        o = data.get("order", {})
        return Order(
            order_id=o.get("order_id", ""),
            ticker=o.get("ticker", ""),
            side=o.get("side", ""),
            action=o.get("action", ""),
            type=o.get("type", ""),
            status=o.get("status", ""),
            yes_price=o.get("yes_price", 0),
            no_price=o.get("no_price", 0),
            count=o.get("count", 0),
            remaining_count=o.get("remaining_count", 0),
            created_time=o.get("created_time", ""),
        )

    async def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> List[Order]:
        """Get your orders"""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [
            Order(
                order_id=o.get("order_id", ""),
                ticker=o.get("ticker", ""),
                side=o.get("side", ""),
                action=o.get("action", ""),
                type=o.get("type", ""),
                status=o.get("status", ""),
                yes_price=o.get("yes_price", 0),
                no_price=o.get("no_price", 0),
                count=o.get("count", 0),
                remaining_count=o.get("remaining_count", 0),
                created_time=o.get("created_time", ""),
            )
            for o in data.get("orders", [])
        ]

    # =========================================================================
    # PORTFOLIO (private)
    # =========================================================================

    async def get_balance(self) -> Balance:
        """Get account balance"""
        data = await self._request("GET", "/portfolio/balance")
        return Balance(
            available_balance=data.get("balance", 0),
            portfolio_value=data.get("portfolio_value", 0),
        )

    async def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> List[Fill]:
        """Get your fills (executed trades)"""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return [
            Fill(
                trade_id=f.get("trade_id", ""),
                order_id=f.get("order_id", ""),
                ticker=f.get("ticker", ""),
                side=f.get("side", ""),
                action=f.get("action", ""),
                yes_price=f.get("yes_price", 0),
                no_price=f.get("no_price", 0),
                count=f.get("count", 0),
                created_time=f.get("created_time", ""),
                is_taker=f.get("is_taker", False),
            )
            for f in data.get("fills", [])
        ]

    async def get_positions(self) -> List[Position]:
        """Get your open positions"""
        data = await self._request("GET", "/portfolio/positions")
        return [
            Position(
                ticker=p.get("ticker", ""),
                market_exposure=p.get("market_exposure", 0),
                realized_pnl=p.get("realized_pnl", 0),
            )
            for p in data.get("market_positions", [])
        ]


# =============================================================================
# KALSHI WEBSOCKET CLIENT (AUTHENTICATED)
# =============================================================================

class KalshiWebSocket:
    """
    Authenticated WebSocket client for Kalshi.

    Private channels (auth required):
    - orderbook_delta: Full orderbook with snapshots + deltas
    - fill: Your fills in real-time
    - market_positions: Your position updates
    - order_group_updates: Your order status updates

    Public channels:
    - ticker: Bid/ask prices
    - trade: All market trades
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        use_demo: bool = True,
    ):
        self.use_demo = use_demo
        self.ws_url = (
            "wss://demo-api.kalshi.co/trade-api/ws/v2"
            if use_demo
            else "wss://api.elections.kalshi.com/trade-api/ws/v2"
        )
        self.auth = KalshiAuth(api_key_id, private_key_pem)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._running = False

        # Callbacks for each message type
        self.on_ticker: Optional[Callable[[Dict], None]] = None
        self.on_trade: Optional[Callable[[Dict], None]] = None
        self.on_orderbook_snapshot: Optional[Callable[[Dict], None]] = None
        self.on_orderbook_delta: Optional[Callable[[Dict], None]] = None
        self.on_fill: Optional[Callable[[Dict], None]] = None
        self.on_position: Optional[Callable[[Dict], None]] = None
        self.on_order_update: Optional[Callable[[Dict], None]] = None
        self.on_error: Optional[Callable[[Dict], None]] = None

        # Data storage
        self.orderbooks: Dict[str, Dict] = {}  # ticker -> {yes: [[price, qty], ...], no: [...]}
        self.tickers: Dict[str, Dict] = {}
        self.trades: List[Dict] = []
        self.fills: List[Dict] = []

    async def connect(self):
        """Establish authenticated WebSocket connection"""
        headers = self.auth.get_ws_headers()

        logger.info(f"Connecting to {self.ws_url} (demo={self.use_demo})")
        self.ws = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )
        self._running = True
        logger.info("WebSocket connected with authentication")

    async def close(self):
        """Close WebSocket connection"""
        self._running = False
        if self.ws:
            await self.ws.close()
        logger.info("WebSocket closed")

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send(self, cmd: str, params: Dict):
        """Send command to WebSocket"""
        msg = {"id": self._next_id(), "cmd": cmd, "params": params}
        await self.ws.send(json.dumps(msg))
        logger.debug(f"Sent: {cmd} {params}")

    # =========================================================================
    # SUBSCRIPTIONS
    # =========================================================================

    async def subscribe_orderbook(self, market_tickers: List[str]):
        """
        Subscribe to FULL orderbook (private, auth required).

        Receives:
        - orderbook_snapshot: Full book on subscribe
        - orderbook_delta: Incremental updates
        """
        await self._send("subscribe", {
            "channels": ["orderbook_delta"],
            "market_tickers": market_tickers,
        })
        logger.info(f"Subscribed to orderbook_delta: {market_tickers}")

    async def subscribe_ticker(self, market_tickers: Optional[List[str]] = None):
        """Subscribe to ticker updates (bid/ask prices)"""
        params = {"channels": ["ticker"]}
        if market_tickers:
            params["market_tickers"] = market_tickers
        await self._send("subscribe", params)
        logger.info(f"Subscribed to ticker: {market_tickers or 'ALL'}")

    async def subscribe_trades(self, market_tickers: List[str]):
        """Subscribe to all market trades"""
        await self._send("subscribe", {
            "channels": ["trade"],
            "market_tickers": market_tickers,
        })
        logger.info(f"Subscribed to trades: {market_tickers}")

    async def subscribe_fills(self):
        """Subscribe to YOUR fills (private)"""
        await self._send("subscribe", {"channels": ["fill"]})
        logger.info("Subscribed to fills")

    async def subscribe_positions(self):
        """Subscribe to YOUR position updates (private)"""
        await self._send("subscribe", {"channels": ["market_positions"]})
        logger.info("Subscribed to positions")

    async def subscribe_orders(self):
        """Subscribe to YOUR order updates (private)"""
        await self._send("subscribe", {"channels": ["order_group_updates"]})
        logger.info("Subscribed to order updates")

    async def subscribe_all(self, market_tickers: List[str]):
        """Subscribe to everything for the given markets"""
        await self.subscribe_orderbook(market_tickers)
        await self.subscribe_ticker(market_tickers)
        await self.subscribe_trades(market_tickers)
        await self.subscribe_fills()
        await self.subscribe_positions()
        await self.subscribe_orders()

    # =========================================================================
    # MESSAGE HANDLING
    # =========================================================================

    async def listen(self):
        """Listen for messages and dispatch to handlers"""
        while self._running:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30)
                data = json.loads(message)
                await self._handle_message(data)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                break
            except Exception as e:
                logger.error(f"Error in listen loop: {e}")

    async def _handle_message(self, data: Dict):
        """Route message to handler"""
        msg_type = data.get("type")

        if msg_type == "orderbook_snapshot":
            self._handle_orderbook_snapshot(data)
        elif msg_type == "orderbook_delta":
            self._handle_orderbook_delta(data)
        elif msg_type == "ticker":
            self._handle_ticker(data)
        elif msg_type == "trade":
            self._handle_trade(data)
        elif msg_type == "fill":
            self._handle_fill(data)
        elif msg_type == "market_positions":
            self._handle_position(data)
        elif msg_type == "order_update":
            self._handle_order_update(data)
        elif msg_type == "subscribed":
            logger.info(f"Subscription confirmed: {data.get('channel')}")
        elif msg_type == "error":
            logger.error(f"WebSocket error: {data}")
            if self.on_error:
                self.on_error(data)
        else:
            logger.debug(f"Unhandled message type: {msg_type}")

    def _handle_orderbook_snapshot(self, data: Dict):
        """
        Full orderbook snapshot.

        Format:
        {
            "type": "orderbook_snapshot",
            "market_ticker": "KXBTC15M-26MAR012300",
            "yes": [[99, 14463], [98, 8917], [97, 190], ...],  # [price, quantity]
            "no": [[96, 1696], [95, 18], [94, 2691], ...],
            "ts": 1234567890
        }
        """
        ticker = data.get("market_ticker") or data.get("ticker")
        if ticker:
            self.orderbooks[ticker] = {
                "yes": data.get("yes", []),
                "no": data.get("no", []),
                "ts": data.get("ts", time.time()),
            }
            logger.info(f"Orderbook snapshot for {ticker}: {len(data.get('yes', []))} yes levels, {len(data.get('no', []))} no levels")

        if self.on_orderbook_snapshot:
            self.on_orderbook_snapshot(data)

    def _handle_orderbook_delta(self, data: Dict):
        """
        Incremental orderbook update.

        Format:
        {
            "type": "orderbook_delta",
            "market_ticker": "...",
            "price": 85,
            "delta": 100,  # positive = add, negative = remove
            "side": "yes"
        }
        """
        ticker = data.get("market_ticker") or data.get("ticker")
        if ticker and ticker in self.orderbooks:
            side = data.get("side", "yes")
            price = data.get("price")
            delta = data.get("delta", 0)

            if price is not None:
                book = self.orderbooks[ticker].get(side, [])
                book_dict = {lvl[0]: lvl[1] for lvl in book}

                if price in book_dict:
                    book_dict[price] += delta
                    if book_dict[price] <= 0:
                        del book_dict[price]
                elif delta > 0:
                    book_dict[price] = delta

                # Rebuild sorted: yes high-to-low, no low-to-high
                self.orderbooks[ticker][side] = sorted(
                    [[p, q] for p, q in book_dict.items()],
                    key=lambda x: x[0],
                    reverse=(side == "yes")
                )

        if self.on_orderbook_delta:
            self.on_orderbook_delta(data)

    def _handle_ticker(self, data: Dict):
        """Handle ticker update (bid/ask prices)"""
        ticker = data.get("market_ticker") or data.get("ticker")
        if ticker:
            self.tickers[ticker] = {
                "yes_bid": data.get("yes_bid"),
                "yes_ask": data.get("yes_ask"),
                "no_bid": data.get("no_bid"),
                "no_ask": data.get("no_ask"),
                "last_price": data.get("last_price"),
                "volume": data.get("volume"),
                "ts": time.time(),
            }

        if self.on_ticker:
            self.on_ticker(data)

    def _handle_trade(self, data: Dict):
        """Handle trade event (someone traded)"""
        trade = {
            "ticker": data.get("market_ticker") or data.get("ticker"),
            "yes_price": data.get("yes_price"),
            "no_price": data.get("no_price"),
            "count": data.get("count"),
            "taker_side": data.get("taker_side"),
            "ts": time.time(),
        }
        self.trades.append(trade)
        if len(self.trades) > 10000:
            self.trades = self.trades[-10000:]

        if self.on_trade:
            self.on_trade(data)

    def _handle_fill(self, data: Dict):
        """Handle YOUR fill (your order got executed)"""
        self.fills.append(data)
        if self.on_fill:
            self.on_fill(data)

    def _handle_position(self, data: Dict):
        """Handle position update"""
        if self.on_position:
            self.on_position(data)

    def _handle_order_update(self, data: Dict):
        """Handle order status update"""
        if self.on_order_update:
            self.on_order_update(data)

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def get_best_bid_ask(self, ticker: str) -> Dict:
        """Get current best bid/ask from stored orderbook"""
        if ticker not in self.orderbooks:
            return {}

        book = self.orderbooks[ticker]
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

        return {
            "yes_bid": yes_levels[0][0] if yes_levels else None,
            "yes_bid_size": yes_levels[0][1] if yes_levels else 0,
            "no_bid": no_levels[0][0] if no_levels else None,
            "no_bid_size": no_levels[0][1] if no_levels else 0,
            "yes_ask": 100 - no_levels[0][0] if no_levels else None,
            "no_ask": 100 - yes_levels[0][0] if yes_levels else None,
        }

    def get_depth_at_price(self, ticker: str, side: str, max_price: int) -> int:
        """Get total quantity available up to a price"""
        if ticker not in self.orderbooks:
            return 0

        book = self.orderbooks[ticker].get(side, [])
        total = 0
        for price, qty in book:
            if side == "yes" and price <= max_price:
                total += qty
            elif side == "no" and price <= max_price:
                total += qty
        return total


# =============================================================================
# STANDALONE TEST
# =============================================================================

async def main():
    """Test with your credentials"""
    import os
    logging.basicConfig(level=logging.INFO)

    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not api_key or not private_key:
        print("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY environment variables")
        print("\nTo test public data only, we can use REST without auth:")

        # Test public endpoint without auth
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params={"series_ticker": "KXBTC15M", "status": "open", "limit": 1}
            ) as resp:
                data = await resp.json()
                if data.get("markets"):
                    market = data["markets"][0]
                    print(f"\nCurrent BTC market: {market['ticker']}")
                    print(f"  Status: {market['status']}")
                    print(f"  Close: {market['close_time']}")
        return

    # Full authenticated test
    ws = KalshiWebSocket(api_key, private_key, use_demo=True)

    def on_orderbook(data):
        ticker = data.get("market_ticker", "?")
        print(f"[ORDERBOOK] {ticker}")
        if "yes" in data:
            print(f"  YES levels: {len(data['yes'])}")
            for lvl in data["yes"][:5]:
                print(f"    {lvl[0]}c: {lvl[1]} contracts")

    def on_trade(data):
        print(f"[TRADE] {data.get('taker_side', '?').upper()} @ {data.get('yes_price', '?')}c x{data.get('count', '?')}")

    ws.on_orderbook_snapshot = on_orderbook
    ws.on_trade = on_trade

    await ws.connect()

    # Get current market
    rest = KalshiRestClient(api_key, private_key, use_demo=True)
    await rest.start()
    markets = await rest.get_markets(series_ticker="KXBTC15M", status="open", limit=1)
    await rest.close()

    if markets:
        ticker = markets[0]["ticker"]
        print(f"\nSubscribing to: {ticker}")
        await ws.subscribe_all([ticker])
        await ws.listen()


if __name__ == "__main__":
    asyncio.run(main())
