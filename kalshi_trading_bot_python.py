"""
Kalshi Trading Bot
==================
All logic in one file: config, API, data engine, strategies, risk, execution, main loop.

Setup:
  1. cp .env.example .env  →  fill in KALSHI_API_KEY
  2. pip install -r requirements.txt
  3. python main.py

Config (config.json):
  paper_trade   — true = simulate only, no real orders
  trade_size    — contracts per order
  max_trades    — max simultaneous open positions
  stop_loss     — exit loss threshold (dollars per contract, e.g. 0.07 = 7¢)
  take_profit   — exit profit threshold (dollars per contract, e.g. 0.10 = 10¢)
  poll_seconds  — seconds between market scans
  market_limit  — how many markets to scan per cycle
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import os, json, time, signal, logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from dotenv import load_dotenv

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 · CONFIG
# ═════════════════════════════════════════════════════════════════════════════

load_dotenv()

def _load_config() -> dict:
    path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        return json.load(f)

_cfg = _load_config()

API_KEY      = os.environ.get("KALSHI_API_KEY", "")
BASE_URL     = "https://trading-api.kalshi.com/trade-api/v2"
PAPER        = _cfg.get("paper_trade",  True)
TRADE_SIZE   = _cfg.get("trade_size",   1)
MAX_TRADES   = _cfg.get("max_trades",   5)
STOP_LOSS    = _cfg.get("stop_loss",    0.07)
TAKE_PROFIT  = _cfg.get("take_profit",  0.10)
POLL_SECS    = _cfg.get("poll_seconds", 5)
MKT_LIMIT    = _cfg.get("market_limit", 5)

if not API_KEY:
    raise EnvironmentError(
        "KALSHI_API_KEY not set.\n"
        "  → Copy .env.example to .env and add your key, or:\n"
        "  → export KALSHI_API_KEY=your_key_here"
    )

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 · LOGGING
# ═════════════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s [%(levelname)-8s] %(message)s"
    logging.basicConfig(
        level=logging.DEBUG,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/trades.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return logging.getLogger("bot")

log = _setup_logging()

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 · API  (retries, session reuse)
# ═════════════════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type":  "application/json",
})

def _get(url: str, **params) -> dict:
    for attempt in range(3):
        try:
            r = _session.get(url, params=params or None, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            if r.status_code < 500:
                log.error("API client error %s → %s", r.status_code, url)
                return {}
            log.warning("API server error %s (attempt %d/3)", r.status_code, attempt + 1)
        except requests.RequestException as e:
            log.warning("Request failed (attempt %d/3): %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(2 ** attempt)
    log.error("All retries exhausted: GET %s", url)
    return {}

def _post(url: str, body: dict) -> dict:
    for attempt in range(2):
        try:
            r = _session.post(url, json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            if r.status_code < 500:
                log.error("Order rejected %s: %s", r.status_code, r.text)
                return {}
            log.warning("Server error placing order (attempt %d/2)", attempt + 1)
        except requests.RequestException as e:
            log.warning("POST failed (attempt %d/2): %s", attempt + 1, e)
        if attempt == 0:
            time.sleep(1)
    return {}

def get_markets(limit: int = 5) -> list[dict]:
    return _get(f"{BASE_URL}/markets", limit=limit, status="open").get("markets", [])

def get_orderbook(ticker: str) -> dict:
    return _get(f"{BASE_URL}/markets/{ticker}/orderbook")

def place_order(ticker: str, side: str, price: int, size: int) -> dict:
    if PAPER:
        log.info("[PAPER] %-6s %-30s @ %3d × %d", side.upper(), ticker, price, size)
        return {"paper": True, "ticker": ticker, "side": side, "price": price}
    body = {"ticker": ticker, "side": side, "price": price, "count": size,
            "type": "limit", "action": "buy"}
    log.info("[LIVE]  %-6s %-30s @ %3d × %d", side.upper(), ticker, price, size)
    return _post(f"{BASE_URL}/orders", body)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 · DATA ENGINE  (rolling 15-min price history)
# ═════════════════════════════════════════════════════════════════════════════

_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
_WINDOW  = timedelta(minutes=15)

def record_price(ticker: str, price: float) -> None:
    now    = datetime.now(timezone.utc)
    cutoff = now - _WINDOW
    buf    = _history[ticker]
    buf.append((now, price))
    _history[ticker] = [p for p in buf if p[0] > cutoff]

def prices(ticker: str) -> list[float]:
    return [p[1] for p in _history.get(ticker, [])]

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 · STRATEGIES
# ═════════════════════════════════════════════════════════════════════════════

_MIN_PTS = 5   # minimum history points required

def mean_reversion(ticker: str, mid: float) -> str | None:
    pts = prices(ticker)
    if len(pts) < _MIN_PTS:
        return None
    trend = pts[-1] - pts[0]
    fair  = 55 if trend > 2 else 45 if trend < -2 else 50
    if mid < fair - 10:
        return "buy_yes"
    if mid > fair + 10:
        return "buy_no"
    return None

def range_trade(ticker: str, mid: float) -> str | None:
    pts = prices(ticker)
    if len(pts) < _MIN_PTS:
        return None
    lo, hi = min(pts), max(pts)
    if hi - lo < 5:           # range too narrow → skip (illiquid / stale)
        return None
    if mid <= lo + 2:
        return "buy_yes"
    if mid >= hi - 2:
        return "buy_no"
    return None

def time_decay(ticker: str, mid: float, expiry: datetime) -> str | None:
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry - datetime.now(timezone.utc) > timedelta(minutes=15):
        return None
    pts = prices(ticker)
    if len(pts) < _MIN_PTS:
        return None
    trend = pts[-1] - pts[0]
    if trend >  3:
        return "buy_yes"
    if trend < -3:
        return "buy_no"
    return None

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 · RISK MANAGER
# ═════════════════════════════════════════════════════════════════════════════

# { ticker: {"side": str, "entry": float, "size": int} }
_positions: dict[str, dict] = {}

def _can_trade(ticker: str) -> bool:
    if ticker in _positions:
        return False                             # already in this market
    if len(_positions) >= MAX_TRADES:
        log.warning("Max positions (%d) reached — skipping %s", MAX_TRADES, ticker)
        return False
    return True

def _open(ticker: str, side: str, entry: float, size: int) -> None:
    _positions[ticker] = {"side": side, "entry": entry, "size": size}
    log.info("Position opened › %s %s @ %.1f", ticker, side, entry)

def _close(ticker: str, reason: str) -> None:
    _positions.pop(ticker, None)
    log.info("Position closed › %s [%s]", ticker, reason)

def check_exits(ticker: str, mid: float) -> bool:
    """Returns True if a position was closed (skip re-entry this cycle)."""
    pos = _positions.get(ticker)
    if not pos:
        return False
    pnl = (mid - pos["entry"]) / 100 if pos["side"] == "yes" \
          else (pos["entry"] - mid) / 100
    if pnl <= -STOP_LOSS:
        _close(ticker, f"stop-loss @ {mid:.1f}  P&L={pnl:.4f}")
        return True
    if pnl >= TAKE_PROFIT:
        _close(ticker, f"take-profit @ {mid:.1f}  P&L={pnl:.4f}")
        return True
    return False

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 · EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

def execute(ticker: str, action: str, bid: float, ask: float, mid: float) -> None:
    if not _can_trade(ticker):
        return
    if action == "buy_yes":
        side, price = "yes", int(round(ask))          # lift the ask
    elif action == "buy_no":
        side, price = "no",  int(round(100 - bid))    # NO ask = 100 − YES bid
    else:
        return

    res = place_order(ticker, side, price, TRADE_SIZE)
    if not res:
        return

    _open(ticker, side, mid, TRADE_SIZE)

    # Append to trade log
    with open("logs/trades.log", "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now(timezone.utc).isoformat()} | {ticker} | {action} | "
            f"bid:{bid:.1f} ask:{ask:.1f} mid:{mid:.1f}\n"
        )

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 · MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════════

_running = True

def _shutdown(sig, _frame):
    global _running
    log.info("Signal %s received — shutting down after this cycle.", sig)
    _running = False

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _parse_expiry(close_time: str) -> datetime | None:
    try:
        return datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _mid_from_book(ob: dict) -> tuple[float, float, float] | None:
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask:
        return None          # crossed or locked book — skip
    return bid, ask, (bid + ask) / 2


def main():
    mode = "PAPER" if PAPER else "⚠ LIVE"
    log.info("━" * 52)
    log.info("  Kalshi Bot  |  mode=%-6s  |  max_pos=%d", mode, MAX_TRADES)
    log.info("  stop_loss=%.2f  take_profit=%.2f  size=%d", STOP_LOSS, TAKE_PROFIT, TRADE_SIZE)
    log.info("━" * 52)

    while _running:
        try:
            markets = get_markets(limit=MKT_LIMIT)
            if not markets:
                log.warning("No open markets returned.")

            for m in markets:
                if not _running:
                    break

                ticker = m.get("ticker", "")
                expiry = _parse_expiry(m.get("close_time", ""))
                if not ticker or expiry is None:
                    continue

                ob = get_orderbook(ticker)
                book = _mid_from_book(ob)
                if book is None:
                    log.debug("No usable book for %s", ticker)
                    continue

                bid, ask, mid = book
                record_price(ticker, mid)

                # Exit check before new entry
                if check_exits(ticker, mid):
                    continue

                # Strategy cascade (priority: time_decay → mean_reversion → range)
                action = (
                    time_decay(ticker, mid, expiry)
                    or mean_reversion(ticker, mid)
                    or range_trade(ticker, mid)
                )

                if action:
                    log.info("Signal %-8s → %s  mid=%.1f  spread=%.1f",
                             action, ticker, mid, ask - bid)
                    execute(ticker, action, bid, ask, mid)

        except Exception:
            log.exception("Unhandled error — loop continues.")

        time.sleep(POLL_SECS)

    log.info("Bot stopped cleanly. Open positions: %s", list(_positions.keys()) or "none")


if __name__ == "__main__":
    main()
