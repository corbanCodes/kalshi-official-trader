#!/usr/bin/env python3
"""
Combined Runner - Trader + Web Dashboard

Runs both:
1. trader.py - The actual trading bot (WebSocket + orders)
2. web_dashboard.py - Flask UI for control

The dashboard reads/writes settings files that the trader watches.
"""

import os
import sys
import subprocess
import signal
import time


def main():
    port = os.environ.get("PORT", "8080")

    print("=" * 70)
    print("KALSHI OFFICIAL PAPER TRADER")
    print("=" * 70)
    print(f"Dashboard: http://localhost:{port}")
    print(f"Password: {os.environ.get('DASHBOARD_PASSWORD', 'trader123')}")
    print("=" * 70)

    processes = []

    # Start the trading worker
    trader_cmd = [sys.executable, "trader.py"]
    trader_proc = subprocess.Popen(
        trader_cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    processes.append(("trader", trader_proc, trader_cmd))
    print(f"Started trader.py (PID: {trader_proc.pid})")

    time.sleep(2)

    # Start the web dashboard
    dashboard_cmd = [
        sys.executable, "-m", "gunicorn",
        "--bind", f"0.0.0.0:{port}",
        "--workers", "2",
        "--timeout", "120",
        "web_dashboard:app"
    ]
    dashboard_proc = subprocess.Popen(
        dashboard_cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    processes.append(("dashboard", dashboard_proc, dashboard_cmd))
    print(f"Started dashboard on port {port} (PID: {dashboard_proc.pid})")

    def shutdown(signum, frame):
        print("\nShutting down...")
        for name, proc, _ in processes:
            print(f"Stopping {name}...")
            proc.terminate()
        for name, proc, _ in processes:
            try:
                proc.wait(timeout=5)
            except:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Monitor and restart if needed
    try:
        while True:
            for i, (name, proc, cmd) in enumerate(processes):
                if proc.poll() is not None:
                    print(f"WARNING: {name} exited with code {proc.returncode}")
                    print(f"Restarting {name}...")
                    new_proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
                    processes[i] = (name, new_proc, cmd)
                    print(f"Restarted {name} (PID: {new_proc.pid})")
            time.sleep(5)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
