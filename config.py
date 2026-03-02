"""
Kalshi Official Paper Trader - Configuration

Two environments:
- PRODUCTION API: Read-only market data (real BTC prices, real order books)
- DEMO API: Order placement with fake money (test execution)

When ready for real trading, just flip USE_DEMO_FOR_ORDERS = False
"""

import os

# =============================================================================
# API ENDPOINTS
# =============================================================================

# Production API - REAL market data (no auth needed for reading)
PROD_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Demo API - Paper trading with fake money (auth required)
DEMO_API_BASE = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

# Toggle: Use demo for orders, production for market data
USE_DEMO_FOR_ORDERS = True  # Set False when ready for real money

# =============================================================================
# MARKET CONFIG
# =============================================================================

SERIES_TICKER = "KXBTC15M"  # Bitcoin 15-minute Up/Down

# BTC Price sources (Kraken is closest to CF Benchmarks settlement)
KRAKEN_API = "https://api.kraken.com/0/public/Ticker"
BINANCE_US_API = "https://api.binance.us/api/v3/ticker/price"

# =============================================================================
# STRATEGY CONFIG - Your winning strategy
# =============================================================================

STRATEGY = {
    "name": "sentiment_odds85_wait10",
    "min_wait_minutes": 10,       # Wait 10 min into 15-min window
    "odds_threshold": 85,         # Enter when favorite hits 85c
    "max_entry_price": 95,        # Don't buy above 95c
    "use_martingale": True,       # Double after loss
    "martingale_cap": 4,          # Max 4 doublings (16x base)
    "bet_pct_of_bankroll": 0.10,  # Bet 10% of bankroll each trade
    "starting_bankroll": 10000,   # $10,000 starting
}

# =============================================================================
# API CREDENTIALS (from environment)
# =============================================================================

def get_credentials():
    """Get API credentials from environment variables"""
    return {
        "api_key_id": os.environ.get("KALSHI_API_KEY_ID", ""),
        "private_key": os.environ.get("KALSHI_PRIVATE_KEY", ""),
    }

# =============================================================================
# OPERATIONAL CONFIG
# =============================================================================

TICK_INTERVAL = 1.0  # Check market every 1 second
STATE_FILE = "trader_state.json"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
