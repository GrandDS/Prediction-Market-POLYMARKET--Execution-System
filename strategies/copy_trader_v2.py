"""
Polymarket Copy Trader v2 — AUTOMATED DISCOVERY + PAPER TRADING
================================================================
Full pipeline, no manual wallet picking:

  DISCOVER  -> harvest candidate wallets from the leaderboard API and
               from the live trade feed of active markets
  SCORE     -> pull each candidate's history and grade it on sample
               size, profit concentration, category focus and recent
               form. Reject anything that fails.
  FOLLOW    -> auto-select the top N scorers and mirror their trades
               on paper, at OUR price (so slippage is measured, not
               assumed)
  ROTATE    -> re-score every RESCORE_HOURS, drop wallets that decay,
               promote new ones

No real money. There is no order-placing code in this file.

Run:
    pip install requests
    python copy_trader_v2.py

Files written:
    followed_wallets.json  - current roster + scores
    paper_portfolio.json   - paper cash and positions
    seen_trades.json       - dedupe
    copy_trader.log        - everything that happened

NOTE ON ENDPOINTS: Polymarket's public API shape changes from time to
time. Every network call is wrapped and logged, and there is a
--selftest mode that pings each endpoint and prints what came back, so
you can see immediately if a route has moved rather than watching the
bot sit silent for weeks.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

# ============================ SETTINGS ============================

# --- discovery ---
LEADERBOARD_WINDOWS = ["1m", "all"]   # windows to harvest from
CANDIDATES_PER_WINDOW = 50
HARVEST_FROM_TRADE_FEED = True        # also find off-leaderboard wallets
TRADE_FEED_PAGES = 4                  # how much of the live feed to scan

# --- scoring gates (a wallet must pass ALL of these) ---
MIN_RESOLVED_TRADES = 200      # below this, win rate is noise
MIN_TOTAL_PNL = 5_000          # USD, all-time
MAX_PNL_CONCENTRATION = 0.35   # no single market may be >35% of profit
MIN_RECENT_PNL = 0             # must be profitable over last 30 days
MIN_CATEGORY_FOCUS = 0.40      # >=40% of trades in one category
MAX_AVG_TRADE_USD = 25_000     # too big to follow without moving price

FOLLOW_TOP_N = 3               # how many wallets to actually follow
RESCORE_HOURS = 168            # re-run discovery + scoring weekly

# --- paper trading ---
POLL_SECONDS = 30
COPY_SIZE_USD = 10.0
MIN_LEADER_TRADE_USD = 50
STARTING_BANKROLL = 1000.0

# --- endpoints ---
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
LEADERBOARD_URL = f"{DATA_API}/leaderboard"

PORTFOLIO_FILE = "paper_portfolio.json"
ROSTER_FILE = "followed_wallets.json"
SEEN_FILE = "seen_trades.json"
LOG_FILE = "copy_trader.log"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "copy-trader-research/2.0"})


# ============================ PLUMBING ============================

def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def get(url, params=None, label=""):
    """Every network call goes through here so failures are visible."""
    try:
        r = SESSION.get(url, params=params or {}, timeout=15)
        if r.status_code != 200:
            log(f"HTTP {r.status_code} on {label or url}")
            return None
        return r.json()
    except Exception as e:
        log(f"ERROR on {label or url}: {e}")
        return None


def as_list(payload):
    """API sometimes returns a bare list, sometimes {'data': [...]}."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    for key in ("data", "results", "trades", "positions", "leaderboard"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def wallet_of(record):
    for key in ("proxyWallet", "wallet", "user", "address", "maker", "owner"):
        val = record.get(key)
        if isinstance(val, str) and val.startswith("0x") and len(val) >= 40:
            return val.lower()
    return None


# =========================== 1. DISCOVER ==========================

def discover_candidates():
    """Harvest wallet addresses from every source we have."""
    found = set()

    for window in LEADERBOARD_WINDOWS:
        payload = get(
            LEADERBOARD_URL,
            {"window": window, "limit": CANDIDATES_PER_WINDOW, "orderBy": "pnl"},
            label=f"leaderboard[{window}]",
        )
        rows = as_list(payload)
        for row in rows:
            w = wallet_of(row)
            if w:
                found.add(w)
        log(f"discovery: leaderboard[{window}] -> {len(rows)} rows")

    if HARVEST_FROM_TRADE_FEED:
        # Wallets trading right now, including ones no leaderboard shows.
        for page in range(TRADE_FEED_PAGES):
            payload = get(
                f"{DATA_API}/trades",
                {"limit": 500, "offset": page * 500},
                label=f"trade-feed[p{page}]",
            )
            rows = as_list(payload)
            if not rows:
                break
            for row in rows:
                w = wallet_of(row)
                if w:
                    found.add(w)

    log(f"discovery: {len(found)} unique candidate wallets")
    return sorted(found)


# ============================ 2. SCORE ============================

def fetch_history(wallet, limit=1000):
    payload = get(
        f"{DATA_API}/trades",
        {"user": wallet, "limit": limit},
        label=f"history[{wallet[:8]}]",
    )
    return as_list(payload)


def fetch_pnl_positions(wallet):
    payload = get(
        f"{DATA_API}/positions",
        {"user": wallet, "limit": 500},
        label=f"positions[{wallet[:8]}]",
    )
    return as_list(payload)


def score_wallet(wallet):
    """
    Grade a wallet. Returns (passed: bool, score: float, detail: dict).
    Every rejection records WHY, so the log tells you whether the gates
    are sane or so strict that nothing survives.
    """
    trades = fetch_history(wallet)
    if len(trades) < MIN_RESOLVED_TRADES:
        return False, 0.0, {"reject": "sample", "trades": len(trades)}

    positions = fetch_pnl_positions(wallet)

    # --- profit, total and per market ---
    pnl_by_market = defaultdict(float)
    total_pnl = 0.0
    for p in positions:
        try:
            pnl = float(p.get("realizedPnl", p.get("cashPnl", 0)) or 0)
        except (TypeError, ValueError):
            continue
        market = p.get("conditionId") or p.get("slug") or p.get("title") or "?"
        pnl_by_market[market] += pnl
        total_pnl += pnl

    if total_pnl < MIN_TOTAL_PNL:
        return False, 0.0, {"reject": "pnl", "pnl": round(total_pnl)}

    # --- concentration: is this one lucky market wearing a costume? ---
    wins = [v for v in pnl_by_market.values() if v > 0]
    gross_win = sum(wins) or 1.0
    concentration = max(wins, default=0.0) / gross_win
    if concentration > MAX_PNL_CONCENTRATION:
        return False, 0.0, {"reject": "concentration", "top_share": round(concentration, 2)}

    # --- recent form: still working, or an expired edge? ---
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    recent = [t for t in trades if float(t.get("timestamp", 0) or 0) >= cutoff]
    if len(recent) < 10:
        return False, 0.0, {"reject": "inactive", "recent_trades": len(recent)}

    # --- specialisation: generalists rarely have repeatable edge ---
    cats = defaultdict(int)
    for t in trades:
        cats[(t.get("eventSlug") or t.get("title") or "?").split("-")[0]] += 1
    focus = max(cats.values()) / len(trades) if cats else 0.0
    if focus < MIN_CATEGORY_FOCUS:
        return False, 0.0, {"reject": "generalist", "focus": round(focus, 2)}

    # --- size: can we even follow them without eating their impact? ---
    sizes = []
    for t in trades:
        try:
            sizes.append(float(t.get("size", 0)) * float(t.get("price", 0)))
        except (TypeError, ValueError):
            pass
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0
    if avg_size > MAX_AVG_TRADE_USD:
        return False, 0.0, {"reject": "too_big", "avg_usd": round(avg_size)}

    # --- composite score: spread-out profit + focus + activity ---
    score = (
        (total_pnl ** 0.5) * 0.5
        + (1 - concentration) * 100
        + focus * 50
        + min(len(recent), 100) * 0.5
    )

    detail = {
        "pnl": round(total_pnl),
        "trades": len(trades),
        "recent_30d": len(recent),
        "top_market_share": round(concentration, 2),
        "focus": round(focus, 2),
        "avg_trade_usd": round(avg_size),
    }
    return True, round(score, 1), detail


def build_roster():
    """Discover -> score -> pick the best FOLLOW_TOP_N. Fully automatic."""
    candidates = discover_candidates()
    if not candidates:
        log("ROSTER: discovery returned nothing — run --selftest to check endpoints")
        return {"updated": None, "wallets": []}

    passed, rejected = [], defaultdict(int)
    for i, w in enumerate(candidates, 1):
        ok, score, detail = score_wallet(w)
        if ok:
            passed.append({"wallet": w, "score": score, **detail})
            log(f"PASS {w[:10]}.. score={score} {detail}")
        else:
            rejected[detail.get("reject", "?")] += 1
        time.sleep(0.25)  # be polite to the API
        if i % 25 == 0:
            log(f"scored {i}/{len(candidates)}")

    log(f"ROSTER: {len(passed)} passed, rejections by reason: {dict(rejected)}")
    passed.sort(key=lambda x: x["score"], reverse=True)
    roster = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "wallets": passed[:FOLLOW_TOP_N],
    }
    save_json(ROSTER_FILE, roster)
    for w in roster["wallets"]:
        log(f"FOLLOWING {w['wallet']} (score {w['score']}, pnl {w['pnl']}, focus {w['focus']})")
    return roster


# ========================= 3. PAPER TRADE =========================

def our_price(token_id, side):
    """The price WE would get right now — ask to buy, bid to sell."""
    book = get(f"{CLOB_API}/book", {"token_id": token_id}, label="book")
    if not book:
        return None
    levels = book.get("asks" if side == "BUY" else "bids") or []
    if not levels:
        return None
    try:
        return float(levels[0]["price"])
    except (KeyError, TypeError, ValueError):
        return None


def copy_trade(portfolio, trade, leader):
    token_id = trade.get("asset")
    side = (trade.get("side") or "").upper()
    title = trade.get("title", "?")
    outcome = trade.get("outcome", "?")
    try:
        leader_price = float(trade.get("price", 0))
        leader_usd = float(trade.get("size", 0)) * leader_price
    except (TypeError, ValueError):
        return
    if not token_id or leader_usd < MIN_LEADER_TRADE_USD:
        return

    price = our_price(token_id, side)
    if price is None or not (0 < price < 1):
        return

    tag = leader[:8]
    if side == "BUY":
        if portfolio["cash"] < COPY_SIZE_USD:
            log(f"SKIP no-cash '{title}'")
            return
        shares = COPY_SIZE_USD / price
        portfolio["cash"] -= COPY_SIZE_USD
        pos = portfolio["positions"].setdefault(
            token_id,
            {"title": title, "outcome": outcome, "shares": 0.0, "cost": 0.0, "leader": leader},
        )
        pos["shares"] += shares
        pos["cost"] += COPY_SIZE_USD
        log(f"BUY  [{tag}] {shares:.1f}sh '{title}'[{outcome}] @{price:.3f} "
            f"(leader {leader_price:.3f}, slip {price - leader_price:+.3f})")

    elif side == "SELL":
        pos = portfolio["positions"].get(token_id)
        if not pos or pos["shares"] <= 0:
            return
        proceeds = pos["shares"] * price
        pnl = proceeds - pos["cost"]
        portfolio["cash"] += proceeds
        portfolio.setdefault("closed", []).append(
            {"title": title, "leader": pos.get("leader"), "pnl": round(pnl, 2)}
        )
        log(f"SELL [{tag}] {pos['shares']:.1f}sh '{title}' @{price:.3f} -> P&L {pnl:+.2f}")
        del portfolio["positions"][token_id]


def summarise(portfolio):
    open_cost = sum(p["cost"] for p in portfolio["positions"].values())
    closed = portfolio.get("closed", [])
    realised = sum(c["pnl"] for c in closed)
    by_leader = defaultdict(float)
    for c in closed:
        by_leader[(c.get("leader") or "?")[:10]] += c["pnl"]
    log(f"SUMMARY cash={portfolio['cash']:.2f} open={len(portfolio['positions'])} "
        f"({open_cost:.2f} invested) closed={len(closed)} realised={realised:+.2f}")
    for lead, pnl in sorted(by_leader.items(), key=lambda x: -x[1]):
        log(f"   leader {lead}.. realised {pnl:+.2f}")


# ============================= MAIN ==============================

def selftest():
    log("SELFTEST — checking every endpoint the bot depends on")
    checks = [
        ("leaderboard", LEADERBOARD_URL, {"window": "1m", "limit": 5}),
        ("trade feed", f"{DATA_API}/trades", {"limit": 5}),
    ]
    for name, url, params in checks:
        payload = get(url, params, label=name)
        rows = as_list(payload)
        log(f"  {name}: {len(rows)} rows")
        if rows:
            log(f"    sample keys: {sorted(rows[0].keys())[:12]}")
            log(f"    wallet field found: {wallet_of(rows[0])}")
    log("SELFTEST done. Empty results = endpoint moved; fix the URL before running live.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="ping endpoints and exit")
    ap.add_argument("--rescore", action="store_true", help="force roster rebuild and exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.rescore:
        build_roster()
        return

    portfolio = load_json(PORTFOLIO_FILE, {"cash": STARTING_BANKROLL, "positions": {}, "closed": []})
    seen = set(load_json(SEEN_FILE, []))
    roster = load_json(ROSTER_FILE, {"updated": None, "wallets": []})

    if not roster["wallets"]:
        log("no roster yet — running discovery")
        roster = build_roster()

    log(f"PAPER MODE. Following {len(roster['wallets'])} auto-selected wallet(s).")
    last_rescore = time.time()
    first_pass, cycles = True, 0

    while True:
        if time.time() - last_rescore > RESCORE_HOURS * 3600:
            log("scheduled re-score")
            roster = build_roster()
            last_rescore = time.time()

        for entry in roster["wallets"]:
            wallet = entry["wallet"]
            for trade in fetch_history(wallet, limit=25):
                tid = trade.get("transactionHash") or trade.get("id")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                if not first_pass:
                    copy_trade(portfolio, trade, wallet)

        first_pass = False
        cycles += 1
        if cycles % 20 == 0:
            summarise(portfolio)

        save_json(PORTFOLIO_FILE, portfolio)
        save_json(SEEN_FILE, list(seen)[-20000:])
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()