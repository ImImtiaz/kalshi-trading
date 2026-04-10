"""
Kalshi Trading Bot
==================
All logic in one file: config, API, data engine, strategies, risk, execution, main loop.

Setup:
  1. Generate an API key in your Kalshi account settings
     → You get two things: an API Key ID and a Private Key (.pem file)
  2. Put your private key file next to this script, e.g. kalshi_private_key.pem
  3. Set config.json:  "api_key_id": "your-key-id",  "private_key_path": "kalshi_private_key.pem"
  4. pip install -r requirements.txt
  5. python main.py

Kalshi orderbook facts (critical):
  - API returns BIDS ONLY — both YES bids and NO bids
  - Arrays are sorted lowest→highest, so last element is best bid
  - YES ask (implied) = 100 - best_NO_bid   (in cents)
  - NO  ask (implied) = 100 - best_YES_bid  (in cents)
  - Prices are integers in cents (0-100)

Order endpoint: POST /trade-api/v2/portfolio/orders
Auth: KALSHI-ACCESS-KEY + KALSHI-ACCESS-TIMESTAMP + KALSHI-ACCESS-SIGNATURE (RSA-SHA256)
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import os, json, time, signal, logging, base64, hashlib, hmac
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

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

BASE_URL        = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID      = os.environ.get("KALSHI_API_KEY_ID")  or _cfg.get("api_key_id",     "")
PRIVATE_KEY_PATH= os.environ.get("KALSHI_KEY_PATH")    or _cfg.get("private_key_path","kalshi_private_key.pem")
PAPER           = _cfg.get("paper_trade",   True)
TRADE_SIZE      = _cfg.get("trade_size",    1)
MAX_TRADES      = _cfg.get("max_trades",    5)
STOP_LOSS       = _cfg.get("stop_loss",     0.07)
TAKE_PROFIT     = _cfg.get("take_profit",   0.10)
POLL_SECS       = _cfg.get("poll_seconds",  5)
SCAN_POOL       = _cfg.get("scan_pool",     25)
WHITELIST       = [s.upper() for s in _cfg.get("series_whitelist", [])]
BLACKLIST       = [s.upper() for s in _cfg.get("series_blacklist", [])]
DEAD_LIMIT      = _cfg.get("dead_miss_limit", 3)

if not API_KEY_ID:
    raise EnvironmentError("api_key_id not set in config.json (or KALSHI_API_KEY_ID env var).")

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 · LOGGING
# ═════════════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s [%(levelname)-8s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
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
# SECTION 3 · AUTH  (RSA-SHA256 signature per request)
# ═════════════════════════════════════════════════════════════════════════════

def _load_private_key():
    path = os.path.join(os.path.dirname(__file__), PRIVATE_KEY_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Private key not found at '{path}'.\n"
            f"Download it from Kalshi account settings and place it next to main.py."
        )
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

_private_key = _load_private_key()


def _auth_headers(method: str, path: str) -> dict:
    """
    Generate the three Kalshi auth headers for one request.
    Signature = base64( RSA-SHA256( timestamp_ms + METHOD + /path ) )
    """
    ts_ms    = str(int(time.time() * 1000))
    message  = (ts_ms + method.upper() + path).encode()
    signature = _private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type":            "application/json",
    }

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 · API  (retries, correct endpoints, correct auth)
# ═════════════════════════════════════════════════════════════════════════════

_session = requests.Session()

def _get(path: str, **params) -> dict:
    url = BASE_URL + path
    for attempt in range(3):
        try:
            r = _session.get(url, headers=_auth_headers("GET", path),
                             params=params or None, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            if r.status_code < 500:
                log.error("API %s %s → %s", r.status_code, path, r.text[:200])
                return {}
            log.warning("Server error %s (attempt %d/3)", r.status_code, attempt + 1)
        except requests.RequestException as e:
            log.warning("Request failed (attempt %d/3): %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(2 ** attempt)
    log.error("All retries exhausted: GET %s", path)
    return {}

def _post(path: str, body: dict) -> dict:
    url = BASE_URL + path
    for attempt in range(2):
        try:
            r = _session.post(url, headers=_auth_headers("POST", path),
                              json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            if r.status_code < 500:
                log.error("Order rejected %s: %s", r.status_code, r.text[:300])
                return {}
            log.warning("Server error placing order (attempt %d/2)", attempt + 1)
        except requests.RequestException as e:
            log.warning("POST failed (attempt %d/2): %s", attempt + 1, e)
        if attempt == 0:
            time.sleep(1)
    return {}

def get_markets(limit: int = 25) -> list[dict]:
    return _get("/markets", limit=limit, status="open").get("markets", [])

def get_orderbook(ticker: str) -> dict:
    """
    Returns: { "orderbook": { "yes": [[price_cents, qty], ...], "no": [...] } }
    Arrays are sorted low→high; last element is the best (highest) bid.
    """
    return _get(f"/markets/{ticker}/orderbook")

def place_order(ticker: str, side: str, price_cents: int, size: int) -> dict:
    """
    side: "yes" or "no"
    price_cents: limit price in cents (1-99)
    Correct endpoint: POST /trade-api/v2/portfolio/orders
    """
    if PAPER:
        log.info("[PAPER] %-6s %-44s @ %2d¢ x%d", side.upper(), ticker, price_cents, size)
        return {"paper": True, "ticker": ticker, "side": side, "price": price_cents}

    body = {
        "ticker":    ticker,
        "side":      side,
        "action":    "buy",
        "type":      "limit",
        "count":     size,
        "yes_price": price_cents if side == "yes" else (100 - price_cents),
        "no_price":  price_cents if side == "no"  else (100 - price_cents),
    }
    log.info("[LIVE]  %-6s %-44s @ %2d¢ x%d", side.upper(), ticker, price_cents, size)
    return _post("/portfolio/orders", body)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 · MARKET FILTER
# ═════════════════════════════════════════════════════════════════════════════

_miss_count:   dict[str, int] = defaultdict(int)
_dead_markets: set[str]       = set()
_dead_logged:  set[str]       = set()

def _is_allowed(ticker: str) -> bool:
    t = ticker.upper()
    if BLACKLIST and any(t.startswith(b) for b in BLACKLIST):
        return False
    if WHITELIST and not any(t.startswith(w) for w in WHITELIST):
        return False
    return True

def _mark_miss(ticker: str) -> None:
    _miss_count[ticker] += 1
    if _miss_count[ticker] >= DEAD_LIMIT:
        _dead_markets.add(ticker)
        if ticker not in _dead_logged:
            log.info("Silencing illiquid market: %s", ticker)
            _dead_logged.add(ticker)

def _mark_live(ticker: str) -> None:
    if ticker in _dead_markets:
        log.info("Market back online: %s", ticker)
        _dead_markets.discard(ticker)
    _miss_count[ticker] = 0

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 · DATA ENGINE  (rolling 15-min price history)
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
# SECTION 7 · ORDERBOOK PARSER
# (Kalshi only returns bids; asks are implied from the opposite side)
#
#   best YES bid  = orderbook["yes"][-1][0]   (cents)
#   YES ask       = 100 - best NO bid          (cents)  ← price to BUY yes
#   best NO bid   = orderbook["no"][-1][0]    (cents)
#   NO ask        = 100 - best YES bid         (cents)  ← price to BUY no
# ═════════════════════════════════════════════════════════════════════════════

def _parse_book(ob: dict) -> tuple[float, float, float] | None:
    """
    Returns (yes_ask, no_ask, mid) in cents, or None if book is unusable.
    yes_ask = price to buy YES = 100 - best NO bid
    no_ask  = price to buy NO  = 100 - best YES bid
    mid     = midpoint of yes_bid and yes_ask
    """
    book     = ob.get("orderbook", {})
    yes_bids = book.get("yes", [])
    no_bids  = book.get("no",  [])

    if not yes_bids and not no_bids:
        return None

    # Best bid = last element (arrays sorted low→high)
    best_yes_bid = yes_bids[-1][0] if yes_bids else None
    best_no_bid  = no_bids[-1][0]  if no_bids  else None

    # Implied asks
    yes_ask = (100 - best_no_bid)  if best_no_bid  is not None else None
    no_ask  = (100 - best_yes_bid) if best_yes_bid is not None else None

    if yes_ask is None or best_yes_bid is None:
        return None

    # Sanity: best yes bid must be below yes ask
    if best_yes_bid >= yes_ask:
        return None

    mid = (best_yes_bid + yes_ask) / 2
    return yes_ask, no_ask, mid

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 · STRATEGIES
# ═════════════════════════════════════════════════════════════════════════════

_MIN_PTS = 5

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
    if hi - lo < 5:
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
# SECTION 9 · RISK MANAGER
# ═════════════════════════════════════════════════════════════════════════════

_positions: dict[str, dict] = {}

def _can_trade(ticker: str) -> bool:
    if ticker in _positions:
        return False
    if len(_positions) >= MAX_TRADES:
        log.warning("Max positions (%d) reached — skipping %s", MAX_TRADES, ticker)
        return False
    return True

def _open(ticker: str, side: str, entry: float, size: int) -> None:
    _positions[ticker] = {"side": side, "entry": entry, "size": size}
    log.info("Position opened › %s  %s @ %.1f¢", ticker, side, entry)

def _close(ticker: str, reason: str) -> None:
    _positions.pop(ticker, None)
    log.info("Position closed › %s  [%s]", ticker, reason)

def check_exits(ticker: str, mid: float) -> bool:
    pos = _positions.get(ticker)
    if not pos:
        return False
    # P&L in dollars per contract (prices in cents, divide by 100)
    pnl = (mid - pos["entry"]) / 100 if pos["side"] == "yes" \
          else (pos["entry"] - mid) / 100
    if pnl <= -STOP_LOSS:
        _close(ticker, f"stop-loss @ {mid:.0f}¢  P&L=${pnl:.4f}")
        return True
    if pnl >= TAKE_PROFIT:
        _close(ticker, f"take-profit @ {mid:.0f}¢  P&L=${pnl:.4f}")
        return True
    return False

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 · EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

def execute(ticker: str, action: str, yes_ask: float, no_ask: float, mid: float) -> None:
    if not _can_trade(ticker):
        return

    if action == "buy_yes":
        side, price = "yes", int(round(yes_ask))
    elif action == "buy_no":
        side, price = "no",  int(round(no_ask))
    else:
        return

    # Don't trade at extreme prices (likely stale or illiquid)
    if not (2 <= price <= 98):
        log.warning("Price %d¢ out of safe range for %s — skipping", price, ticker)
        return

    res = place_order(ticker, side, price, TRADE_SIZE)
    if not res:
        return

    _open(ticker, side, mid, TRADE_SIZE)
    with open("logs/trades.log", "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now(timezone.utc).isoformat()} | {ticker} | {action} | "
            f"yes_ask:{yes_ask:.0f}¢ no_ask:{no_ask:.0f}¢ mid:{mid:.1f}¢\n"
        )

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11 · MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════════

_running = True

def _shutdown(sig, _frame):
    global _running
    log.info("Signal %s — shutting down after this cycle.", sig)
    _running = False

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _parse_expiry(close_time: str) -> datetime | None:
    try:
        return datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def main():
    mode = "PAPER" if PAPER else "LIVE"
    log.info("━" * 60)
    log.info("  Kalshi Bot  |  mode=%-8s |  max_pos=%d", mode, MAX_TRADES)
    log.info("  stop_loss=$%.2f  take_profit=$%.2f  size=%d  pool=%d",
             STOP_LOSS, TAKE_PROFIT, TRADE_SIZE, SCAN_POOL)
    if WHITELIST:
        log.info("  Whitelist: %s", WHITELIST)
    if BLACKLIST:
        log.info("  Blacklist: %s", BLACKLIST)
    log.info("━" * 60)

    cycle = 0
    while _running:
        try:
            cycle += 1
            markets = get_markets(limit=SCAN_POOL)
            if not markets:
                log.warning("No open markets returned.")

            active = 0
            for m in markets:
                if not _running:
                    break

                ticker = m.get("ticker", "")
                expiry = _parse_expiry(m.get("close_time", ""))
                if not ticker or expiry is None:
                    continue

                if not _is_allowed(ticker):
                    continue

                if ticker in _dead_markets:
                    continue

                ob   = get_orderbook(ticker)
                book = _parse_book(ob)

                if book is None:
                    _mark_miss(ticker)
                    continue

                _mark_live(ticker)
                active += 1
                yes_ask, no_ask, mid = book
                record_price(ticker, mid)

                if check_exits(ticker, mid):
                    continue

                action = (
                    time_decay(ticker, mid, expiry)
                    or mean_reversion(ticker, mid)
                    or range_trade(ticker, mid)
                )

                if action:
                    spread = yes_ask + (no_ask or 0) - 100  # total cost of both sides - $1
                    log.info("Signal %-8s → %-44s mid=%.1f¢  spread=%.0f¢",
                             action, ticker, mid, yes_ask - (100 - yes_ask))
                    execute(ticker, action, yes_ask, no_ask, mid)

            if cycle % 12 == 0:
                log.info(
                    "-- Cycle %d | scanned=%d active=%d silenced=%d positions=%d",
                    cycle, len(markets), active, len(_dead_markets), len(_positions)
                )

        except Exception:
            log.exception("Unhandled error — loop continues.")

        time.sleep(POLL_SECS)

    log.info("Bot stopped. Open positions: %s", list(_positions.keys()) or "none")


if __name__ == "__main__":
    main()
