#!/usr/bin/env python3
"""
Kalshi Official Paper Trader - Web Dashboard

Features:
- Start/Stop trading
- Live orderbook view
- Settings: martingale, bet %, flat bet, limit/market orders
- Full orderbook history export
- Track entry price vs actual fill price (slippage)
- Trade history with P&L
"""

import os
import json
import csv
import io
import threading
import subprocess
import sys
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify, request, Response, session, redirect, url_for

app = Flask(__name__)

# Start trader in background thread when app starts
def start_trader_background():
    """Start the trader.py as a subprocess"""
    subprocess.Popen(
        [sys.executable, "trader.py"],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

# Start trader on first request (gunicorn worker)
_trader_started = False

@app.before_request
def ensure_trader_running():
    global _trader_started
    if not _trader_started:
        _trader_started = True
        thread = threading.Thread(target=start_trader_background, daemon=True)
        thread.start()
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

# Global state (shared with trader process)
STATE_FILE = "trader_state.json"
SETTINGS_FILE = "trader_settings.json"
ORDERBOOK_HISTORY_FILE = "orderbook_history.json"
TRADE_HISTORY_FILE = "trade_history.json"

# Default settings
DEFAULT_SETTINGS = {
    "trading_enabled": False,
    "use_demo": True,
    "use_production_prices": True,  # Use real prices even in paper trading mode
    "min_wait_minutes": 10,
    "odds_threshold": 85,
    "max_entry_price": 95,
    "use_martingale": True,
    "martingale_cap": 4,
    "bet_mode": "percent",  # "percent" or "flat"
    "bet_percent": 10,
    "flat_bet_size": 100,
    "order_type": "limit",  # "limit" or "market"
    "starting_bankroll": 10000,
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
        except:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        "bankroll": DEFAULT_SETTINGS["starting_bankroll"],
        "wins": 0,
        "losses": 0,
        "trades": [],
        "current_market": None,
        "connected": False,
        "last_update": None,
    }


def load_orderbook_history():
    if os.path.exists(ORDERBOOK_HISTORY_FILE):
        try:
            with open(ORDERBOOK_HISTORY_FILE) as f:
                return json.load(f)
        except:
            pass
    return []


def load_trade_history():
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE) as f:
                return json.load(f)
        except:
            pass
    return []


# =============================================================================
# HTML TEMPLATE
# =============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Official Trader</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 100%);
            color: #e0e0e0;
            min-height: 100vh;
            padding: 24px;
            line-height: 1.5;
        }
        .container { max-width: 1400px; margin: 0 auto; }

        h1 {
            color: #fff;
            margin-bottom: 24px;
            font-size: 28px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .env-badge {
            font-size: 11px;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 12px;
            background: rgba(99, 102, 241, 0.2);
            color: #818cf8;
            letter-spacing: 0.5px;
        }
        .env-badge.production {
            background: rgba(239, 68, 68, 0.2);
            color: #f87171;
        }

        h2 {
            color: #fff;
            margin: 0 0 16px;
            font-size: 16px;
            font-weight: 600;
            letter-spacing: -0.3px;
        }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }

        .card {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 20px;
            backdrop-filter: blur(10px);
        }
        .card.highlight {
            border-color: rgba(34, 197, 94, 0.4);
            box-shadow: 0 0 20px rgba(34, 197, 94, 0.1);
        }

        .stat {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        }
        .stat:last-child { border-bottom: none; }
        .stat-label { color: #9ca3af; font-size: 14px; }
        .stat-value {
            color: #fff;
            font-weight: 600;
            font-size: 15px;
        }
        .stat-value.positive { color: #22c55e; }
        .stat-value.negative { color: #ef4444; }

        button {
            background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
            color: #fff;
            border: none;
            padding: 12px 24px;
            border-radius: 10px;
            cursor: pointer;
            font-family: inherit;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.2s;
            box-shadow: 0 4px 12px rgba(34, 197, 94, 0.3);
        }
        button:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(34, 197, 94, 0.4);
        }
        button.danger {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);
        }
        button.danger:hover {
            box-shadow: 0 6px 16px rgba(239, 68, 68, 0.4);
        }
        button.secondary {
            background: rgba(255, 255, 255, 0.1);
            box-shadow: none;
        }
        button.secondary:hover {
            background: rgba(255, 255, 255, 0.15);
            box-shadow: none;
        }

        input, select {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #fff;
            padding: 12px 16px;
            border-radius: 10px;
            font-family: inherit;
            font-size: 14px;
            width: 100%;
            margin: 6px 0;
            transition: all 0.2s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #6366f1;
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2);
        }
        input:disabled, select:disabled {
            opacity: 0.4;
            cursor: not-allowed;
            background: rgba(255, 255, 255, 0.02);
        }
        select {
            cursor: pointer;
        }
        select option {
            background: #1a1a2e;
            color: #fff;
        }

        label {
            display: block;
            color: #9ca3af;
            margin-top: 16px;
            margin-bottom: 4px;
            font-size: 13px;
            font-weight: 500;
        }

        .toggle {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 0;
        }
        .toggle span {
            font-size: 14px;
            font-weight: 500;
        }
        .toggle input[type="checkbox"] {
            width: 48px;
            height: 26px;
            appearance: none;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 13px;
            cursor: pointer;
            position: relative;
            transition: background 0.2s;
            margin: 0;
            padding: 0;
        }
        .toggle input[type="checkbox"]:checked { background: #22c55e; }
        .toggle input[type="checkbox"]::before {
            content: '';
            position: absolute;
            width: 20px;
            height: 20px;
            background: #fff;
            border-radius: 50%;
            top: 3px;
            left: 3px;
            transition: 0.2s;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        .toggle input[type="checkbox"]:checked::before { left: 25px; }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th, td {
            padding: 12px 8px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        }
        th {
            color: #6b7280;
            font-weight: 500;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        tr:hover { background: rgba(255, 255, 255, 0.03); }

        .status-dot {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
        }
        .status-dot.online {
            background: #22c55e;
            box-shadow: 0 0 8px rgba(34, 197, 94, 0.6);
        }
        .status-dot.offline {
            background: #ef4444;
            box-shadow: 0 0 8px rgba(239, 68, 68, 0.6);
        }

        .orderbook {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        .orderbook-side { font-size: 13px; }
        .orderbook-side h3 {
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 12px;
        }
        .orderbook-level {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }
        .yes-side h3 { color: #22c55e; }
        .no-side h3 { color: #ef4444; }
        .yes-side .orderbook-level { color: #86efac; }
        .no-side .orderbook-level { color: #fca5a5; }

        .actions { margin-top: 20px; }

        #refresh-indicator {
            position: fixed;
            top: 20px;
            right: 24px;
            color: #6b7280;
            font-size: 12px;
            background: rgba(0,0,0,0.3);
            padding: 6px 12px;
            border-radius: 20px;
        }

        .slippage { color: #f59e0b; }
        .slippage.good { color: #22c55e; }
        .slippage.bad { color: #ef4444; }

        .settings-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 24px;
        }
        @media (max-width: 900px) {
            .settings-grid { grid-template-columns: 1fr; }
        }

        .bet-input-group {
            transition: opacity 0.2s;
        }
        .bet-input-group.disabled {
            opacity: 0.4;
            pointer-events: none;
        }

        .button-group {
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <span class="status-dot {{ 'online' if state.connected else 'offline' }}"></span>
            Kalshi Official Trader
            <span class="env-badge {{ 'production' if not settings.use_demo else '' }}">
                {{ 'DEMO' if settings.use_demo else 'LIVE' }}
            </span>
        </h1>

        <div id="refresh-indicator">Auto-refresh: 5s</div>

        <!-- Status & Controls -->
        <div class="grid">
            <div class="card {{ 'highlight' if settings.trading_enabled else '' }}">
                <h2>Trading Status</h2>
                <div class="stat">
                    <span class="stat-label">Status</span>
                    <span class="stat-value">{{ 'TRADING' if settings.trading_enabled else 'STOPPED' }}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Current Market</span>
                    <span class="stat-value">{{ state.current_market or 'None' }}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">WebSocket</span>
                    <span class="stat-value">{{ 'Connected' if state.connected else 'Disconnected' }}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Last Update</span>
                    <span class="stat-value">{{ state.last_update or 'Never' }}</span>
                </div>
                <div class="actions">
                    {% if settings.trading_enabled %}
                    <button class="danger" onclick="toggleTrading(false)">STOP TRADING</button>
                    {% else %}
                    <button onclick="toggleTrading(true)">START TRADING</button>
                    {% endif %}
                </div>
            </div>

            <div class="card">
                <h2>Performance</h2>
                <div class="stat">
                    <span class="stat-label">Bankroll</span>
                    <span class="stat-value">${{ "%.2f"|format(state.bankroll) }}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">P&L</span>
                    <span class="stat-value {{ 'positive' if (state.bankroll - settings.starting_bankroll) >= 0 else 'negative' }}">
                        ${{ "%.2f"|format(state.bankroll - settings.starting_bankroll) }}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">ROI</span>
                    <span class="stat-value {{ 'positive' if (state.bankroll - settings.starting_bankroll) >= 0 else 'negative' }}">
                        {{ "%.2f"|format(((state.bankroll - settings.starting_bankroll) / settings.starting_bankroll) * 100) }}%
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">Win/Loss</span>
                    <span class="stat-value">{{ state.wins }}/{{ state.losses }}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Win Rate</span>
                    <span class="stat-value">
                        {% if (state.wins + state.losses) > 0 %}
                        {{ "%.1f"|format((state.wins / (state.wins + state.losses)) * 100) }}%
                        {% else %}
                        -
                        {% endif %}
                    </span>
                </div>
            </div>
        </div>

        <!-- Settings -->
        <div class="card" style="margin-top: 20px;">
            <h2>Strategy Settings</h2>
            <form id="settings-form">
                <div class="settings-grid">
                    <div>
                        <div class="toggle">
                            <input type="checkbox" name="use_demo" id="use_demo" {{ 'checked' if settings.use_demo else '' }}>
                            <span>Paper Trading (no real orders)</span>
                        </div>

                        <div class="toggle">
                            <input type="checkbox" name="use_production_prices" id="use_production_prices" {{ 'checked' if settings.use_production_prices else '' }}>
                            <span>Use Real Prices (production orderbook)</span>
                        </div>

                        <label>Min Wait (minutes into window)</label>
                        <input type="number" name="min_wait_minutes" value="{{ settings.min_wait_minutes }}" min="0" max="14">

                        <label>Odds Threshold (cents)</label>
                        <input type="number" name="odds_threshold" value="{{ settings.odds_threshold }}" min="50" max="99">

                        <label>Max Entry Price (cents)</label>
                        <input type="number" name="max_entry_price" value="{{ settings.max_entry_price }}" min="50" max="99">
                    </div>

                    <div>
                        <div class="toggle">
                            <input type="checkbox" name="use_martingale" id="use_martingale" {{ 'checked' if settings.use_martingale else '' }}>
                            <span>Use Martingale</span>
                        </div>

                        <label>Martingale Cap (max doublings)</label>
                        <input type="number" name="martingale_cap" id="martingale_cap" value="{{ settings.martingale_cap }}" min="1" max="10" {{ 'disabled' if not settings.use_martingale else '' }}>

                        <label>Bet Mode</label>
                        <select name="bet_mode" id="bet_mode">
                            <option value="percent" {{ 'selected' if settings.bet_mode == 'percent' else '' }}>Percent of Bankroll</option>
                            <option value="flat" {{ 'selected' if settings.bet_mode == 'flat' else '' }}>Flat Bet Size</option>
                        </select>

                        <div id="bet-percent-group" class="bet-input-group {{ 'disabled' if settings.bet_mode == 'flat' else '' }}">
                            <label>Bet Percent (%)</label>
                            <input type="number" name="bet_percent" id="bet_percent" value="{{ settings.bet_percent }}" min="1" max="50" step="0.5" {{ 'disabled' if settings.bet_mode == 'flat' else '' }}>
                        </div>

                        <div id="flat-bet-group" class="bet-input-group {{ 'disabled' if settings.bet_mode == 'percent' else '' }}">
                            <label>Flat Bet Size ($)</label>
                            <input type="number" name="flat_bet_size" id="flat_bet_size" value="{{ settings.flat_bet_size }}" min="1" max="10000" {{ 'disabled' if settings.bet_mode == 'percent' else '' }}>
                        </div>
                    </div>

                    <div>
                        <label style="margin-top: 0;">Order Type</label>
                        <select name="order_type">
                            <option value="limit" {{ 'selected' if settings.order_type == 'limit' else '' }}>Limit Order</option>
                            <option value="market" {{ 'selected' if settings.order_type == 'market' else '' }}>Market Order</option>
                        </select>

                        <label>Starting Bankroll ($)</label>
                        <input type="number" name="starting_bankroll" value="{{ settings.starting_bankroll }}" min="100" max="1000000">
                    </div>
                </div>

                <div class="button-group">
                    <button type="submit">Save Settings</button>
                    <button type="button" class="secondary" onclick="resetSettings()">Reset to Defaults</button>
                </div>
            </form>
        </div>

        <!-- Live Orderbook -->
        <div class="card" style="margin-top: 20px;">
            <h2>Live Orderbook</h2>
            <div class="orderbook">
                <div class="orderbook-side yes-side">
                    <h3>YES Bids</h3>
                    {% for level in orderbook.yes[:10] %}
                    <div class="orderbook-level">
                        <span>{{ level[0] }}¢</span>
                        <span>{{ "{:,}".format(level[1]) }}</span>
                    </div>
                    {% else %}
                    <div style="color: #666;">No data</div>
                    {% endfor %}
                </div>
                <div class="orderbook-side no-side">
                    <h3>NO Bids</h3>
                    {% for level in orderbook.no[:10] %}
                    <div class="orderbook-level">
                        <span>{{ level[0] }}¢</span>
                        <span>{{ "{:,}".format(level[1]) }}</span>
                    </div>
                    {% else %}
                    <div style="color: #666;">No data</div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <!-- Trade History -->
        <div class="card" style="margin-top: 20px;">
            <h2>Trade History (Entry vs Fill)</h2>
            <div style="margin-bottom: 10px;">
                <button class="secondary" onclick="exportTrades()">Export Trades CSV</button>
                <button class="secondary" onclick="exportOrderbook()">Export Orderbook History</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Time</th>
                        <th>Window</th>
                        <th>Side</th>
                        <th>Entry Price</th>
                        <th>Fill Price</th>
                        <th>Slippage</th>
                        <th>Contracts</th>
                        <th>Result</th>
                        <th>P&L</th>
                    </tr>
                </thead>
                <tbody>
                    {% for trade in trades[:50] %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>{{ trade.timestamp[:19] if trade.timestamp else '-' }}</td>
                        <td>{{ trade.window_id or '-' }}</td>
                        <td>{{ trade.side or '-' }}</td>
                        <td>{{ trade.entry_price or '-' }}¢</td>
                        <td>{{ trade.fill_price or trade.entry_price or '-' }}¢</td>
                        <td class="slippage {{ 'good' if (trade.fill_price or trade.entry_price or 0) <= (trade.entry_price or 0) else 'bad' }}">
                            {% if trade.fill_price and trade.entry_price %}
                            {{ trade.fill_price - trade.entry_price }}¢
                            {% else %}
                            -
                            {% endif %}
                        </td>
                        <td>{{ trade.contracts or '-' }}</td>
                        <td style="color: {{ '#00ff00' if trade.outcome == 'win' else '#ff4444' }}">
                            {{ (trade.outcome or '-')|upper }}
                        </td>
                        <td class="{{ 'positive' if (trade.profit or 0) >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(trade.profit or 0) }}
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="10" style="color: #666;">No trades yet</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        // Bet mode toggle logic
        function updateBetModeInputs() {
            const betMode = document.getElementById('bet_mode').value;
            const percentGroup = document.getElementById('bet-percent-group');
            const flatGroup = document.getElementById('flat-bet-group');
            const percentInput = document.getElementById('bet_percent');
            const flatInput = document.getElementById('flat_bet_size');

            if (betMode === 'percent') {
                percentGroup.classList.remove('disabled');
                flatGroup.classList.add('disabled');
                percentInput.disabled = false;
                flatInput.disabled = true;
            } else {
                percentGroup.classList.add('disabled');
                flatGroup.classList.remove('disabled');
                percentInput.disabled = true;
                flatInput.disabled = false;
            }
        }

        // Martingale toggle logic
        function updateMartingaleInputs() {
            const useMartingale = document.getElementById('use_martingale').checked;
            const martingaleCap = document.getElementById('martingale_cap');
            martingaleCap.disabled = !useMartingale;
        }

        // Initialize on page load
        document.getElementById('bet_mode').addEventListener('change', updateBetModeInputs);
        document.getElementById('use_martingale').addEventListener('change', updateMartingaleInputs);

        function toggleTrading(enabled) {
            fetch('/api/trading', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: enabled})
            }).then(() => location.reload());
        }

        document.getElementById('settings-form').addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(this);
            const settings = {};

            // Get all values including disabled inputs
            settings.use_demo = document.getElementById('use_demo').checked;
            settings.use_martingale = document.getElementById('use_martingale').checked;
            settings.min_wait_minutes = parseFloat(document.querySelector('[name="min_wait_minutes"]').value);
            settings.odds_threshold = parseFloat(document.querySelector('[name="odds_threshold"]').value);
            settings.max_entry_price = parseFloat(document.querySelector('[name="max_entry_price"]').value);
            settings.martingale_cap = parseFloat(document.getElementById('martingale_cap').value);
            settings.bet_mode = document.getElementById('bet_mode').value;
            settings.bet_percent = parseFloat(document.getElementById('bet_percent').value);
            settings.flat_bet_size = parseFloat(document.getElementById('flat_bet_size').value);
            settings.order_type = document.querySelector('[name="order_type"]').value;
            settings.starting_bankroll = parseFloat(document.querySelector('[name="starting_bankroll"]').value);

            fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settings)
            }).then(() => location.reload());
        });

        function resetSettings() {
            fetch('/api/settings/reset', {method: 'POST'}).then(() => location.reload());
        }

        function exportTrades() {
            window.location.href = '/export/trades';
        }

        function exportOrderbook() {
            window.location.href = '/export/orderbook';
        }

        // Auto-refresh every 5 seconds
        setTimeout(() => location.reload(), 5000);
    </script>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Login - Kalshi Trader</title>
    <style>
        body {
            font-family: monospace;
            background: #0a0a0a;
            color: #00ff00;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }
        .login-box {
            background: #111;
            border: 1px solid #333;
            padding: 30px;
            border-radius: 8px;
            width: 300px;
        }
        h1 { margin-bottom: 20px; font-size: 18px; }
        input {
            width: 100%;
            padding: 10px;
            margin: 10px 0;
            background: #1a1a1a;
            border: 1px solid #444;
            color: #00ff00;
            border-radius: 4px;
        }
        button {
            width: 100%;
            padding: 10px;
            background: #00aa00;
            border: none;
            color: #000;
            font-weight: bold;
            cursor: pointer;
            border-radius: 4px;
        }
        button:hover { background: #00cc00; }
        .error { color: #ff4444; margin: 10px 0; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Kalshi Official Trader</h1>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
"""


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        correct = os.environ.get("DASHBOARD_PASSWORD", "trader123")
        if password == correct:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_HTML, error="Invalid password")
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    settings = load_settings()
    state = load_state()
    trades = load_trade_history()

    # Get latest orderbook from state
    orderbook = state.get("orderbook", {"yes": [], "no": []})

    return render_template_string(
        DASHBOARD_HTML,
        settings=settings,
        state=state,
        trades=trades,
        orderbook=orderbook,
    )


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "POST":
        new_settings = request.get_json()
        current = load_settings()
        current.update(new_settings)
        save_settings(current)
        return jsonify({"status": "ok"})

    return jsonify(load_settings())


@app.route("/api/settings/reset", methods=["POST"])
def reset_settings():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthorized"}), 401

    save_settings(DEFAULT_SETTINGS)
    return jsonify({"status": "ok"})


@app.route("/api/trading", methods=["POST"])
def toggle_trading():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    settings = load_settings()
    settings["trading_enabled"] = data.get("enabled", False)
    save_settings(settings)
    return jsonify({"status": "ok"})


@app.route("/api/state")
def api_state():
    return jsonify(load_state())


@app.route("/export/trades")
def export_trades():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    trades = load_trade_history()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "timestamp", "window_id", "ticker", "side", "entry_price", "fill_price",
        "slippage", "contracts", "bet_size", "outcome", "profit", "bankroll_after"
    ])

    for t in trades:
        slippage = (t.get("fill_price") or 0) - (t.get("entry_price") or 0)
        writer.writerow([
            t.get("timestamp", ""),
            t.get("window_id", ""),
            t.get("ticker", ""),
            t.get("side", ""),
            t.get("entry_price", ""),
            t.get("fill_price", ""),
            slippage,
            t.get("contracts", ""),
            t.get("bet_size", ""),
            t.get("outcome", ""),
            t.get("profit", ""),
            t.get("bankroll_after", ""),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_history.csv"}
    )


@app.route("/export/orderbook")
def export_orderbook():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    history = load_orderbook_history()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "timestamp", "ticker", "side", "price", "quantity"
    ])

    for snapshot in history:
        ts = snapshot.get("timestamp", "")
        ticker = snapshot.get("ticker", "")
        for level in snapshot.get("yes", []):
            writer.writerow([ts, ticker, "YES", level[0], level[1]])
        for level in snapshot.get("no", []):
            writer.writerow([ts, ticker, "NO", level[0], level[1]])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=orderbook_history.csv"}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
