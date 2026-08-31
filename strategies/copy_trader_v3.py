"""
Polymarket Copy Trader v3 — ONE BOT, ONE DASHBOARD
===================================================
Merges the whole pipeline and puts everything on a live GUI:

    DISCOVER -> SCORE -> FOLLOW -> PAPER TRADE
       all of it visible at  http://localhost:8787

The dashboard shows, live:
  - which pipeline stage is running right now
  - every action and every error (bugs are shown in red, not hidden)
  - the wallets currently followed and why they passed
  - open paper positions and realised P&L per leader
  - an equity graph: what the paper bankroll is worth over time

PAPER MODE ONLY. There is no order-placing code in this file.

Run:
    pip install requests
    python copy_trader_v3.py
    then open  http://localhost:8787  in your browser
"""

import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ============================ SETTINGS ============================

DASHBOARD_PORT = 8787

# discovery
LEADERBOARD_WINDOWS = ["1m", "all"]
CANDIDATES_PER_WINDOW = 50
TRADE_FEED_PAGES = 2

# scoring gates
MIN_RESOLVED_TRADES = 200
MIN_TOTAL_PNL = 5_000
MAX_PNL_CONCENTRATION = 0.35
MIN_RECENT_TRADES_30D = 10
MIN_CATEGORY_FOCUS = 0.40
MAX_AVG_TRADE_USD = 25_000

FOLLOW_TOP_N = 3
RESCORE_HOURS = 168          # weekly roster refresh

# paper trading
POLL_SECONDS = 2           # check leaders every 2 seconds (was 30)
SLOW_TASK_SECONDS = 60     # equity graph point + file saves, once a minute
COPY_SIZE_USD = 10.0
MIN_LEADER_TRADE_USD = 50
STARTING_BANKROLL = 1000.0
MAX_BUY_PRICE = 0.90      # never buy near-certain outcomes — no room left
MAX_SLIPPAGE = 0.05       # at 2s delay, allow catching a move part-way (was 0.01)

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

PORTFOLIO_FILE = "paper_portfolio.json"
ROSTER_FILE = "followed_wallets.json"
SEEN_FILE = "seen_trades.json"

_LOCAL = threading.local()

def _session():
    """One network connection PER THREAD — sharing one across the live
    feed and the polling loop is what froze the bot."""
    if not hasattr(_LOCAL, "s"):
        _LOCAL.s = requests.Session()
        _LOCAL.s.headers.update({"User-Agent": "copy-trader-research/3.0"})
    return _LOCAL.s

# Live push feed (instant mode). Needs: pip install websocket-client
LIVE_FEED_URL = "wss://ws-live-data.polymarket.com"
try:
    import websocket  # websocket-client
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

TRADE_LOCK = threading.Lock()   # one trade processed at a time

# ======================= SHARED LIVE STATE ========================
# Everything the dashboard shows lives here. The bot thread writes,
# the web server reads. One lock keeps it simple and safe.

LOCK = threading.Lock()
STATE = {
    "stage": "starting",            # discover / score / trade / idle
    "stage_detail": "",
    "started": datetime.now(timezone.utc).isoformat(),
    "events": deque(maxlen=400),    # [{t, level, msg}]
    "errors": 0,
    "roster": [],                   # wallets we follow + their scores
    "portfolio": {"cash": STARTING_BANKROLL, "positions": {}, "closed": []},
    "equity": deque(maxlen=2000),   # [{t, value}] for the graph
    "rejections": {},               # why candidates failed scoring
    "next_rescore": None,
}


def emit(msg, level="info"):
    """Single funnel for every action and error -> GUI + permanent file."""
    now = datetime.now(timezone.utc)
    t = now.strftime("%H:%M:%S")
    with LOCK:
        STATE["events"].appendleft({"t": t, "level": level, "msg": str(msg)[:300]})
        if level == "error":
            STATE["errors"] += 1
    print(f"{t} [{level}] {msg}", flush=True)
    # every line saved to a daily file — survives closing the program
    try:
        with open(f"log_{now.strftime('%Y-%m-%d')}.txt", "a", encoding="utf-8") as f:
            f.write(f"{t} [{level}] {msg}\n")
    except Exception:
        pass


def snapshot(portfolio, equity):
    """Hourly record of the account, one line per hour, for analysis."""
    try:
        with open("history.csv", "a", encoding="utf-8") as f:
            if f.tell() == 0:
                f.write("time,account_value,cash,open_positions,closed_trades,realised_pnl\n")
            realised = sum(c["pnl"] for c in portfolio.get("closed", []))
            f.write(f"{datetime.now(timezone.utc).isoformat()},{equity},"
                    f"{round(portfolio['cash'], 2)},{len(portfolio['positions'])},"
                    f"{len(portfolio.get('closed', []))},{round(realised, 2)}\n")
    except Exception as e:
        emit(f"snapshot failed: {e}", "error")


def set_stage(stage, detail=""):
    with LOCK:
        STATE["stage"] = stage
        STATE["stage_detail"] = detail


# ============================ PLUMBING ============================

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


def get(url, params=None, label="", quiet=False):
    try:
        r = _session().get(url, params=params or {}, timeout=15)
        if r.status_code != 200:
            if not quiet:
                emit(f"HTTP {r.status_code} from {label or url}", "error")
            return None
        return r.json()
    except Exception as e:
        if not quiet:
            emit(f"{label or url} failed: {e}", "error")
        return None


def as_list(payload):
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


# ====================== DISCOVER + SCORE ==========================

def discover_candidates():
    set_stage("discover", "harvesting wallets")
    found = set()
    for window in LEADERBOARD_WINDOWS:
        rows = as_list(get(f"{DATA_API}/leaderboard",
                           {"window": window, "limit": CANDIDATES_PER_WINDOW, "orderBy": "pnl"},
                           label=f"leaderboard[{window}]"))
        for row in rows:
            w = wallet_of(row)
            if w:
                found.add(w)
        emit(f"discover: leaderboard[{window}] gave {len(rows)} rows")
    for page in range(TRADE_FEED_PAGES):
        rows = as_list(get(f"{DATA_API}/trades", {"limit": 500, "offset": page * 500},
                           label=f"feed[p{page}]"))
        if not rows:
            break
        for row in rows:
            w = wallet_of(row)
            if w:
                found.add(w)
    emit(f"discover: {len(found)} unique candidates")
    return sorted(found)


def fetch_history(wallet, limit=1000):
    return as_list(get(f"{DATA_API}/trades", {"user": wallet, "limit": limit},
                       label=f"history[{wallet[:8]}]"))


def score_wallet(wallet):
    trades = fetch_history(wallet)
    if len(trades) < MIN_RESOLVED_TRADES:
        return False, 0.0, {"reject": "small sample"}

    positions = as_list(get(f"{DATA_API}/positions", {"user": wallet, "limit": 500},
                            label=f"positions[{wallet[:8]}]"))
    pnl_by_market, total_pnl = defaultdict(float), 0.0
    for p in positions:
        try:
            pnl = float(p.get("realizedPnl", p.get("cashPnl", 0)) or 0)
        except (TypeError, ValueError):
            continue
        pnl_by_market[p.get("conditionId") or p.get("title") or "?"] += pnl
        total_pnl += pnl
    if total_pnl < MIN_TOTAL_PNL:
        return False, 0.0, {"reject": "low pnl"}

    wins = [v for v in pnl_by_market.values() if v > 0]
    concentration = (max(wins, default=0.0) / (sum(wins) or 1.0))
    if concentration > MAX_PNL_CONCENTRATION:
        return False, 0.0, {"reject": "one lucky market"}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    recent = [t for t in trades if float(t.get("timestamp", 0) or 0) >= cutoff]
    if len(recent) < MIN_RECENT_TRADES_30D:
        return False, 0.0, {"reject": "gone quiet"}

    cats = defaultdict(int)
    for t in trades:
        cats[(t.get("eventSlug") or t.get("title") or "?").split("-")[0]] += 1
    focus = max(cats.values()) / len(trades) if cats else 0.0
    if focus < MIN_CATEGORY_FOCUS:
        return False, 0.0, {"reject": "generalist"}

    sizes = []
    for t in trades:
        try:
            sizes.append(float(t.get("size", 0)) * float(t.get("price", 0)))
        except (TypeError, ValueError):
            pass
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0
    if avg_size > MAX_AVG_TRADE_USD:
        return False, 0.0, {"reject": "trades too big to follow"}

    score = ((total_pnl ** 0.5) * 0.5 + (1 - concentration) * 100
             + focus * 50 + min(len(recent), 100) * 0.5)
    return True, round(score, 1), {
        "pnl": round(total_pnl), "trades": len(trades), "recent_30d": len(recent),
        "top_market_share": round(concentration, 2), "focus": round(focus, 2),
        "avg_trade_usd": round(avg_size),
    }


def build_roster():
    candidates = discover_candidates()
    if not candidates:
        emit("discovery empty — check endpoints (red errors above tell you which)", "error")
        return []
    passed, rejections = [], defaultdict(int)
    for i, w in enumerate(candidates, 1):
        set_stage("score", f"wallet {i}/{len(candidates)}")
        ok, score, detail = score_wallet(w)
        if ok:
            passed.append({"wallet": w, "score": score, **detail})
            emit(f"PASS {w[:10]}… score {score} (pnl {detail['pnl']}, focus {detail['focus']})", "good")
        else:
            rejections[detail["reject"]] += 1
        time.sleep(0.25)
    with LOCK:
        STATE["rejections"] = dict(rejections)
    emit(f"scoring done: {len(passed)} passed, rejections {dict(rejections)}")
    passed.sort(key=lambda x: x["score"], reverse=True)
    roster = passed[:FOLLOW_TOP_N]
    save_json(ROSTER_FILE, {"updated": datetime.now(timezone.utc).isoformat(), "wallets": roster})
    with LOCK:
        STATE["roster"] = roster
    for r in roster:
        emit(f"FOLLOWING {r['wallet'][:12]}… score {r['score']}", "good")
    if not roster:
        emit("nobody passed the gates — loosen thresholds in SETTINGS", "error")
    return roster


# ========================= PAPER TRADING ==========================

def our_price(token_id, side, quiet=False):
    book = get(f"{CLOB_API}/book", {"token_id": token_id}, label="book", quiet=quiet)
    if not book:
        return None
    levels = book.get("asks" if side == "BUY" else "bids") or []
    try:
        return float(levels[0]["price"]) if levels else None
    except (KeyError, TypeError, ValueError):
        return None


_SETTLE_NOTES = {}

def _settle_note(token_id, msg):
    """Explain a stuck position at most once per hour, not every minute."""
    now = time.time()
    if now - _SETTLE_NOTES.get(token_id, 0) > 3600:
        _SETTLE_NOTES[token_id] = now
        emit(msg)


def try_settle(portfolio, token_id):
    """Market finished? Pay out $1/share if right, $0 if wrong,
    and record the result in Closed trades. Returns True if settled."""
    pos = portfolio["positions"].get(token_id)
    if not pos:
        return False
    rows = as_list(get(f"{GAMMA_API}/markets", {"clob_token_ids": token_id},
                       label="resolution", quiet=True))
    if not rows:
        _settle_note(token_id, f"waiting: no result data yet for '{pos['title']}'")
        return False
    m = rows[0]
    if not (m.get("closed") or m.get("resolved")):
        _settle_note(token_id, f"waiting: '{pos['title']}' not officially finished yet")
        return False
    try:
        toks = m.get("clobTokenIds")
        finals = m.get("outcomePrices")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(finals, str):
            finals = json.loads(finals)
        idx = [t.lower() for t in toks].index(token_id.lower())
        final = float(finals[idx])
    except Exception as e:
        emit(f"could not read result for '{pos['title']}': {e}", "error")
        return False
    proceeds = pos["shares"] * final
    pnl = proceeds - pos["cost"]
    portfolio["cash"] += proceeds
    portfolio["closed"].append({
        "t": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
        "title": pos["title"], "leader": pos.get("leader", "?"),
        "pnl": round(pnl, 2)})
    emit(f"SETTLED '{pos['title']}' — result {'WIN' if final > 0.5 else 'LOSS'} "
         f"→ P&L {pnl:+.2f}", "trade")
    del portfolio["positions"][token_id]
    return True


def mark_to_market(portfolio):
    """Value the account honestly: settle finished markets first,
    then value the rest at what they'd sell for right now."""
    for token_id in list(portfolio["positions"]):
        if our_price(token_id, "SELL", quiet=True) is None:
            try_settle(portfolio, token_id)
    value = portfolio["cash"]
    for token_id, pos in portfolio["positions"].items():
        bid = our_price(token_id, "SELL", quiet=True)
        value += pos["shares"] * bid if bid else pos["cost"]
    return round(value, 2)


def copy_trade(portfolio, trade, leader):
    token_id = trade.get("asset")
    side = (trade.get("side") or "").upper()
    title = (trade.get("title") or "?")[:60]
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

    if side == "BUY":
        # Guard 1: don't buy the top of nearly-decided markets
        if price > MAX_BUY_PRICE:
            emit(f"skip: price too high ({price:.3f} > {MAX_BUY_PRICE}) '{title}'")
            return
        # Guard 2: if the move already happened, we missed it — don't chase
        slippage = price - leader_price
        if slippage > MAX_SLIPPAGE:
            emit(f"skip: slippage too big ({slippage:+.3f}) '{title}'")
            return
        if portfolio["cash"] < COPY_SIZE_USD:
            emit(f"skip (no paper cash left): {title}")
            return
        shares = COPY_SIZE_USD / price
        portfolio["cash"] -= COPY_SIZE_USD
        pos = portfolio["positions"].setdefault(
            token_id, {"title": title, "outcome": outcome, "shares": 0.0,
                       "cost": 0.0, "leader": leader})
        pos["shares"] += shares
        pos["cost"] += COPY_SIZE_USD
        emit(f"BUY {shares:.1f}sh '{title}' [{outcome}] @ {price:.3f} "
             f"(leader {leader_price:.3f}, slippage {price - leader_price:+.3f})", "trade")
    elif side == "SELL":
        pos = portfolio["positions"].get(token_id)
        if not pos or pos["shares"] <= 0:
            return
        proceeds = pos["shares"] * price
        pnl = proceeds - pos["cost"]
        portfolio["cash"] += proceeds
        portfolio["closed"].append({
            "t": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
            "title": pos["title"], "leader": pos.get("leader", "?"),
            "pnl": round(pnl, 2)})
        emit(f"SELL '{title}' @ {price:.3f} → P&L {pnl:+.2f}", "trade")
        del portfolio["positions"][token_id]


# =========================== BOT THREAD ===========================

def live_feed_listener(ctx):
    """INSTANT MODE: Polymarket pushes every trade to us the moment it
    happens. We react in ~0.5s instead of up to 2s. If this feed is
    down or its format changes, the 2s polling loop still catches
    everything — this is a speed boost, not a single point of failure."""
    shown_sample = False
    while True:
        try:
            ws = websocket.create_connection(LIVE_FEED_URL, timeout=10)
            ws.send(json.dumps({"action": "subscribe", "subscriptions":
                                [{"topic": "activity", "type": "trades"}]}))
            emit("instant mode ON — live feed connected", "good")
            ws.settimeout(30)
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    try:
                        ws.ping()
                    except Exception:
                        break
                    continue
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                payloads = msg if isinstance(msg, list) else [msg]
                for p in payloads:
                    body = p.get("payload", p) if isinstance(p, dict) else None
                    if not isinstance(body, dict):
                        continue
                    if not shown_sample:
                        shown_sample = True
                        emit(f"live feed sample keys: {sorted(body.keys())}")
                    w = wallet_of(body)
                    followed = {e["wallet"] for e in STATE["roster"]}
                    if not w or w not in followed:
                        continue
                    tid = body.get("transactionHash") or body.get("id")
                    with TRADE_LOCK:
                        if not tid or tid in ctx["seen"]:
                            continue
                        ctx["seen"].add(tid)
                        copy_trade(ctx["portfolio"], body, w)
        except Exception as e:
            emit(f"live feed dropped ({e}) — reconnecting in 5s, polling still on")
        time.sleep(5)


def bot_loop():
    portfolio = load_json(PORTFOLIO_FILE,
                          {"cash": STARTING_BANKROLL, "positions": {}, "closed": []})
    portfolio.setdefault("closed", [])
    seen = set(load_json(SEEN_FILE, []))
    saved = load_json(ROSTER_FILE, {"wallets": []})
    roster = saved.get("wallets", [])
    with LOCK:
        STATE["portfolio"] = portfolio
        STATE["roster"] = roster

    emit("bot started — PAPER MODE, no real money anywhere")
    if not roster:
        roster = build_roster()
    ctx = {"portfolio": portfolio, "seen": seen}
    if HAVE_WS:
        threading.Thread(target=live_feed_listener, args=(ctx,), daemon=True).start()
    else:
        emit("instant mode OFF — run 'pip install websocket-client' to enable", "error")
    emit(f"backup polling every {POLL_SECONDS}s. PAPER MODE.")
    last_rescore = time.time()
    last_slow = 0.0
    last_snapshot = 0.0
    last_heartbeat = time.time()
    checks = 0
    first_pass = True

    while True:
        try:
            if time.time() - last_rescore > RESCORE_HOURS * 3600:
                emit("weekly re-score starting")
                roster = build_roster()
                last_rescore = time.time()
            with LOCK:
                STATE["next_rescore"] = round(
                    (last_rescore + RESCORE_HOURS * 3600 - time.time()) / 3600, 1)

            # FAST LANE — every 2s: any new trades from the leaders?
            set_stage("trade", f"watching {len(roster)} wallets (2s)")
            for entry in roster:
                wallet = entry["wallet"]
                for trade in fetch_history(wallet, limit=10):
                    tid = trade.get("transactionHash") or trade.get("id")
                    with TRADE_LOCK:
                        if not tid or tid in seen:
                            continue
                        seen.add(tid)
                        if not first_pass:
                            copy_trade(portfolio, trade, wallet)
            first_pass = False
            checks += 1

            # HEARTBEAT — every 5 min, proof of life even when nothing trades
            if time.time() - last_heartbeat > 300:
                last_heartbeat = time.time()
                emit(f"heartbeat: alive, {checks} leader checks done, "
                     f"{len(portfolio['positions'])} open positions", "good")

            # SLOW LANE — once a minute: settle, graph, save
            if time.time() - last_slow > SLOW_TASK_SECONDS:
                last_slow = time.time()
                equity = mark_to_market(portfolio)
                with LOCK:
                    STATE["equity"].append(
                        {"t": datetime.now(timezone.utc).strftime("%H:%M"), "v": equity})
                    STATE["portfolio"] = portfolio
                save_json(PORTFOLIO_FILE, portfolio)
                save_json(SEEN_FILE, list(seen)[-20000:])
                if time.time() - last_snapshot > 3600:   # hourly account record
                    last_snapshot = time.time()
                    snapshot(portfolio, equity)
        except Exception as e:
            emit(f"bot loop crashed and recovered: {e}", "error")
        time.sleep(POLL_SECONDS)


# =========================== DASHBOARD ============================

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copy Trader — paper desk</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root{
    --ink:#0d1117; --panel:#161b22; --line:#21262d;
    --text:#e6edf3; --dim:#8b949e;
    --up:#3fb950; --down:#f85149; --amber:#d29922; --blue:#58a6ff;
  }
  *{box-sizing:border-box;margin:0}
  body{background:var(--ink);color:var(--text);
       font:14px/1.5 "SF Mono","Cascadia Mono",Consolas,monospace;padding:16px}
  h1{font-size:15px;font-weight:600;letter-spacing:.04em}
  .paper{color:var(--amber);border:1px solid var(--amber);border-radius:3px;
         padding:1px 7px;font-size:11px;margin-left:10px;vertical-align:2px}
  /* pipeline strip — the one signature element */
  .pipe{display:flex;gap:0;margin:14px 0 18px;border:1px solid var(--line);
        border-radius:6px;overflow:hidden}
  .pipe div{flex:1;padding:8px 12px;color:var(--dim);border-right:1px solid var(--line);
            transition:all .3s}
  .pipe div:last-child{border-right:0}
  .pipe div.on{background:var(--panel);color:var(--text);box-shadow:inset 0 -2px 0 var(--blue)}
  .pipe small{display:block;font-size:11px;color:var(--dim)}
  .grid{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}
  .card h2{font-size:11px;font-weight:600;color:var(--dim);text-transform:uppercase;
           letter-spacing:.1em;margin-bottom:10px}
  .stats{display:flex;gap:26px;flex-wrap:wrap}
  .stats b{display:block;font-size:20px;font-weight:600}
  .stats span{font-size:11px;color:var(--dim)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  td,th{padding:4px 6px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500;font-size:11px}
  td.num{text-align:right}
  .up{color:var(--up)} .down{color:var(--down)}
  #log{height:340px;overflow-y:auto;font-size:12px}
  #log p{padding:2px 0;border-bottom:1px solid var(--line)}
  #log .error{color:var(--down)} #log .good{color:var(--up)}
  #log .trade{color:var(--blue)} #log time{color:var(--dim);margin-right:8px}
  #chartbox{height:220px}
  .empty{color:var(--dim);font-size:12.5px;padding:8px 0}
</style></head><body>
<h1>COPY TRADER<span class="paper">PAPER MONEY</span>
    <span id="err" style="float:right;color:var(--down);font-size:12px"></span></h1>

<div class="pipe" id="pipe">
  <div data-s="discover">1 · Discover<small>harvest wallets</small></div>
  <div data-s="score">2 · Score<small>filter the lucky</small></div>
  <div data-s="trade">3 · Trade<small>mirror on paper</small></div>
</div>

<div class="grid">
 <div>
  <div class="card"><h2>Paper account</h2>
    <div class="stats">
      <div><b id="equity">—</b><span>account value $</span></div>
      <div><b id="cash">—</b><span>cash $</span></div>
      <div><b id="openn">—</b><span>open positions</span></div>
      <div><b id="realised">—</b><span>realised P&L $</span></div>
    </div>
    <div id="chartbox"><canvas id="chart"></canvas></div>
  </div>
  <div class="card" style="margin-top:14px"><h2>Live actions & bugs</h2>
    <div id="log"></div>
  </div>
 </div>
 <div>
  <div class="card"><h2>Following</h2><div id="roster"></div>
    <div id="rescore" class="empty"></div></div>
  <div class="card" style="margin-top:14px"><h2>Open positions</h2><div id="pos"></div></div>
  <div class="card" style="margin-top:14px"><h2>Closed trades</h2><div id="closed"></div></div>
 </div>
</div>

<script>
let chart;
function money(x){return (x>=0?"+":"")+x.toFixed(2)}
async function tick(){
  let s;
  try{ s = await (await fetch("/status")).json() }catch(e){ return }

  document.querySelectorAll("#pipe div").forEach(d=>
    d.classList.toggle("on", d.dataset.s===s.stage));
  document.getElementById("err").textContent =
    s.errors ? s.errors+" errors — see log" : "";

  const p=s.portfolio, open=Object.values(p.positions||{});
  const realised=(p.closed||[]).reduce((a,c)=>a+c.pnl,0);
  const eq=s.equity.length? s.equity[s.equity.length-1].v :
           p.cash+open.reduce((a,o)=>a+o.cost,0);
  equity.textContent=eq.toFixed(2);
  cash.textContent=p.cash.toFixed(2);
  openn.textContent=open.length;
  realised>=0 ? realisedEl(realised,"up") : realisedEl(realised,"down");
  function realisedEl(v,cls){const e=document.getElementById("realised");
    e.textContent=money(v); e.className=cls}

  // roster
  roster.innerHTML = s.roster.length ? "<table><tr><th>wallet</th><th class=num>score</th><th class=num>pnl</th><th class=num>focus</th></tr>"+
    s.roster.map(r=>`<tr><td>${r.wallet.slice(0,10)}…</td><td class=num>${r.score}</td><td class=num>${r.pnl}</td><td class=num>${r.focus}</td></tr>`).join("")+"</table>"
    : "<div class=empty>No wallets yet — pipeline is still selecting.</div>";
  rescore.textContent = s.next_rescore!=null ? "next re-score in "+s.next_rescore+"h" : "";

  pos.innerHTML = open.length ? "<table><tr><th>market</th><th class=num>cost $</th></tr>"+
    open.map(o=>`<tr><td>${o.title} <span style="color:var(--dim)">[${o.outcome}]</span></td><td class=num>${o.cost.toFixed(2)}</td></tr>`).join("")+"</table>"
    : "<div class=empty>None open. Waiting for a leader to trade.</div>";

  closed.innerHTML = (p.closed||[]).length ? "<table><tr><th>market</th><th class=num>P&L $</th></tr>"+
    p.closed.slice(-12).reverse().map(c=>`<tr><td>${c.title}</td><td class="num ${c.pnl>=0?'up':'down'}">${money(c.pnl)}</td></tr>`).join("")+"</table>"
    : "<div class=empty>Nothing closed yet.</div>";

  log.innerHTML = s.events.map(e=>
    `<p class="${e.level}"><time>${e.t}</time>${e.msg}</p>`).join("");

  const labels=s.equity.map(e=>e.t), values=s.equity.map(e=>e.v);
  if(!chart){
    chart=new Chart(document.getElementById("chart"),{type:"line",
      data:{labels,datasets:[{data:values,borderColor:"#58a6ff",borderWidth:1.5,
        pointRadius:0,fill:true,backgroundColor:"rgba(88,166,255,.08)",tension:.25}]},
      options:{responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:"#8b949e",maxTicksLimit:8},grid:{color:"#21262d"}},
                y:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}}}}});
  } else { chart.data.labels=labels; chart.data.datasets[0].data=values; chart.update("none") }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence per-request console spam
        pass

    def do_GET(self):
        if self.path == "/status":
            with LOCK:
                body = json.dumps({
                    "stage": STATE["stage"],
                    "stage_detail": STATE["stage_detail"],
                    "errors": STATE["errors"],
                    "events": list(STATE["events"]),
                    "roster": STATE["roster"],
                    "portfolio": STATE["portfolio"],
                    "equity": list(STATE["equity"]),
                    "rejections": STATE["rejections"],
                    "next_rescore": STATE["next_rescore"],
                }).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=bot_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), Handler)
    emit(f"dashboard live at http://localhost:{DASHBOARD_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            pf = STATE["portfolio"]
        save_json(PORTFOLIO_FILE, pf)
        snapshot(pf, pf["cash"] + sum(p["cost"] for p in pf["positions"].values()))
        emit("closed by user — final snapshot saved")


if __name__ == "__main__":
    main()