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
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Monaco', 'Menlo', monospace;
            background: #0a0a0a;
            color: #00ff00;
            padding: 20px;
            line-height: 1.4;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #00ff00; margin-bottom: 20px; font-size: 24px; }
        h2 { color: #00cc00; margin: 20px 0 10px; font-size: 18px; border-bottom: 1px solid #333; padding-bottom: 5px; }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .card {
            background: #111;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 15px;
        }
        .card.highlight { border-color: #00ff00; }
        .card.warning { border-color: #ff6600; }
        .card.danger { border-color: #ff0000; }

        .stat { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #222; }
        .stat:last-child { border-bottom: none; }
        .stat-label { color: #888; }
        .stat-value { color: #00ff00; font-weight: bold; }
        .stat-value.positive { color: #00ff00; }
        .stat-value.negative { color: #ff4444; }

        button {
            background: #00aa00;
            color: #000;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-weight: bold;
            margin: 5px;
        }
        button:hover { background: #00cc00; }
        button.danger { background: #aa0000; color: #fff; }
        button.danger:hover { background: #cc0000; }
        button.secondary { background: #333; color: #fff; }
        button.secondary:hover { background: #444; }

        input, select {
            background: #1a1a1a;
            border: 1px solid #444;
            color: #00ff00;
            padding: 8px 12px;
            border-radius: 4px;
            font-family: inherit;
            width: 100%;
            margin: 5px 0;
        }
        input:focus, select:focus { outline: none; border-color: #00ff00; }

        label { display: block; color: #888; margin-top: 10px; font-size: 12px; }

        .toggle {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 0;
        }
        .toggle input[type="checkbox"] {
            width: 50px;
            height: 26px;
            appearance: none;
            background: #333;
            border-radius: 13px;
            cursor: pointer;
            position: relative;
        }
        .toggle input[type="checkbox"]:checked { background: #00aa00; }
        .toggle input[type="checkbox"]::before {
            content: '';
            position: absolute;
            width: 22px;
            height: 22px;
            background: #fff;
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: 0.2s;
        }
        .toggle input[type="checkbox"]:checked::before { left: 26px; }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        th, td {
            padding: 8px;
            text-align: left;
            border-bottom: 1px solid #222;
        }
        th { color: #888; font-weight: normal; }
        tr:hover { background: #1a1a1a; }

        .status-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }
        .status-dot.online { background: #00ff00; }
        .status-dot.offline { background: #ff0000; }

        .orderbook {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        .orderbook-side { font-size: 11px; }
        .orderbook-side h3 { font-size: 14px; margin-bottom: 10px; }
        .orderbook-level {
            display: flex;
            justify-content: space-between;
            padding: 3px 0;
        }
        .yes-side { color: #00ff00; }
        .no-side { color: #ff6666; }

        .actions { margin: 20px 0; }

        #refresh-indicator {
            position: fixed;
            top: 10px;
            right: 10px;
            color: #666;
            font-size: 11px;
        }

        .slippage { color: #ff9900; }
        .slippage.good { color: #00ff00; }
        .slippage.bad { color: #ff4444; }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <span class="status-dot {{ 'online' if state.connected else 'offline' }}"></span>
            Kalshi Official Trader
            <span style="font-size: 12px; color: #666;">
                {{ 'DEMO' if settings.use_demo else 'PRODUCTION' }}
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
                <div class="grid">
                    <div>
                        <div class="toggle">
                            <input type="checkbox" name="use_demo" {{ 'checked' if settings.use_demo else '' }}>
                            <span>Use Demo Environment</span>
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
                            <input type="checkbox" name="use_martingale" {{ 'checked' if settings.use_martingale else '' }}>
                            <span>Use Martingale</span>
                        </div>

                        <label>Martingale Cap (max doublings)</label>
                        <input type="number" name="martingale_cap" value="{{ settings.martingale_cap }}" min="1" max="10">

                        <label>Bet Mode</label>
                        <select name="bet_mode">
                            <option value="percent" {{ 'selected' if settings.bet_mode == 'percent' else '' }}>Percent of Bankroll</option>
                            <option value="flat" {{ 'selected' if settings.bet_mode == 'flat' else '' }}>Flat Bet Size</option>
                        </select>

                        <label>Bet Percent (%)</label>
                        <input type="number" name="bet_percent" value="{{ settings.bet_percent }}" min="1" max="50" step="0.5">

                        <label>Flat Bet Size ($)</label>
                        <input type="number" name="flat_bet_size" value="{{ settings.flat_bet_size }}" min="1" max="10000">
                    </div>

                    <div>
                        <label>Order Type</label>
                        <select name="order_type">
                            <option value="limit" {{ 'selected' if settings.order_type == 'limit' else '' }}>Limit Order</option>
                            <option value="market" {{ 'selected' if settings.order_type == 'market' else '' }}>Market Order</option>
                        </select>

                        <label>Starting Bankroll ($)</label>
                        <input type="number" name="starting_bankroll" value="{{ settings.starting_bankroll }}" min="100" max="1000000">
                    </div>
                </div>

                <div style="margin-top: 15px;">
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
            formData.forEach((value, key) => {
                if (key === 'use_demo' || key === 'use_martingale') {
                    settings[key] = true;
                } else if (['min_wait_minutes', 'odds_threshold', 'max_entry_price',
                            'martingale_cap', 'bet_percent', 'flat_bet_size', 'starting_bankroll'].includes(key)) {
                    settings[key] = parseFloat(value);
                } else {
                    settings[key] = value;
                }
            });
            // Handle unchecked checkboxes
            if (!formData.has('use_demo')) settings.use_demo = false;
            if (!formData.has('use_martingale')) settings.use_martingale = false;

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
