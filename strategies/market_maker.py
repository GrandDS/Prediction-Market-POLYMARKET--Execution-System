"""
Polymarket Market Maker v1 — PAPER TRADING + DASHBOARD
=======================================================
The shopkeeper strategy. For each chosen market the bot:

  1. Looks at the current best bid and best ask
  2. Posts its own paper BUY offer just above the best bid, and its
     own paper SELL offer just below the best ask (inside the spread)
  3. When real traders cross our prices, we count our offer as
     filled — buy low, sell high, pocket the gap, repeat
  4. If we end up holding too much of one side (inventory), we shift
     our prices to offload it, and we stop quoting markets that get
     too close to being decided (that's where news kills shopkeepers)

Everything is on the dashboard:  http://localhost:8788
(different port from the copy trader — both can run at once)

PAPER MODE ONLY. No order-placing code exists in this file.

Run:
    pip install requests websocket-client
    python market_maker_v1.py
"""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

try:
    import websocket
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

# ============================ SETTINGS ============================

DASHBOARD_PORT = 8788

# which markets to trade
N_MARKETS = 5              # how many markets to make at once
MIN_PRICE, MAX_PRICE = 0.15, 0.85   # only mid-range markets — no 99c graveyards
MIN_SPREAD = 0.02          # gap must be at least 2c wide to be worth standing in
MAX_SPREAD = 0.10          # gap wider than 10c = abandoned market, not opportunity
REFRESH_MARKETS_MIN = 60   # re-pick markets every hour

# quoting
QUOTE_SIZE_USD = 10.0      # size of each paper offer
TICK = 0.001               # how far inside the spread we stand
REQUOTE_SECONDS = 5        # how often we reprice
MAX_INVENTORY_USD = 30.0   # max exposure per market before we lean to offload
INVENTORY_LEAN = 0.01      # price shift per full inventory unit
PANIC_PRICE = 0.12         # if mid goes beyond 0.12/0.88, dump and stop quoting

STARTING_BANKROLL = 1000.0

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
LIVE_FEED_URL = "wss://ws-live-data.polymarket.com"

PORTFOLIO_FILE = "mm_portfolio.json"

# ======================= SHARED LIVE STATE ========================

LOCK = threading.Lock()
TRADE_LOCK = threading.Lock()
STATE = {
    "stage": "starting", "started": datetime.now(timezone.utc).isoformat(),
    "events": deque(maxlen=400), "errors": 0,
    "markets": [],            # what we're quoting + our current prices
    "portfolio": {"cash": STARTING_BANKROLL, "inventory": {}, "fills": []},
    "equity": deque(maxlen=2000),
}

_LOCAL = threading.local()

def _session():
    if not hasattr(_LOCAL, "s"):
        _LOCAL.s = requests.Session()
        _LOCAL.s.headers.update({"User-Agent": "market-maker-research/1.0"})
    return _LOCAL.s


def emit(msg, level="info"):
    now = datetime.now(timezone.utc)
    t = now.strftime("%H:%M:%S")
    with LOCK:
        STATE["events"].appendleft({"t": t, "level": level, "msg": str(msg)[:300]})
        if level == "error":
            STATE["errors"] += 1
    print(f"{t} [{level}] {msg}", flush=True)
    try:
        with open(f"mm_log_{now.strftime('%Y-%m-%d')}.txt", "a", encoding="utf-8") as f:
            f.write(f"{t} [{level}] {msg}\n")
    except Exception:
        pass


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
    for key in ("data", "results", "markets"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


# ======================== MARKET SELECTION ========================

def pick_markets():
    """Find busy, mid-priced markets with a spread worth standing in."""
    with LOCK:
        STATE["stage"] = "select"
    rows = as_list(get(f"{GAMMA_API}/markets",
                       {"active": "true", "closed": "false",
                        "order": "volume24hr", "ascending": "false", "limit": 100},
                       label="market list"))
    chosen = []
    for m in rows:
        try:
            toks = m.get("clobTokenIds")
            if isinstance(toks, str):
                toks = json.loads(toks)
            if not toks:
                continue
            token = toks[0]                     # quote the YES side
            title = (m.get("question") or m.get("title") or "?")[:70]
        except Exception:
            continue
        book = get(f"{CLOB_API}/book", {"token_id": token}, label="book", quiet=True)
        if not book:
            continue
        try:
            bid = float(book["bids"][0]["price"])
            ask = float(book["asks"][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        mid = (bid + ask) / 2
        spread = ask - bid
        # BOTH sides must be real prices — an empty book fakes a 0.50 mid
        if (MIN_PRICE < bid and ask < MAX_PRICE
                and MIN_SPREAD <= spread <= MAX_SPREAD):
            chosen.append({"token": token, "title": title,
                           "bid": bid, "ask": ask, "spread": round(spread, 3)})
            emit(f"quoting '{title}' (mid {mid:.2f}, spread {spread:.3f})", "good")
        if len(chosen) >= N_MARKETS:
            break
        time.sleep(0.2)
    if not chosen:
        emit("no suitable markets found — will retry in an hour", "error")
    return chosen


# ========================= QUOTING ENGINE =========================

def make_quotes(mkt, inv_usd):
    """Our paper prices: inside the spread, leaning against inventory."""
    lean = (inv_usd / MAX_INVENTORY_USD) * INVENTORY_LEAN
    our_bid = round(mkt["bid"] + TICK - lean, 3)
    our_ask = round(mkt["ask"] - TICK - lean, 3)
    if our_ask - our_bid < TICK:              # never cross ourselves
        return None, None
    return our_bid, our_ask


def on_market_trade(portfolio, mkt, trade_price, trade_size_usd):
    """A real trade happened in a market we quote. Did it hit our offer?"""
    inv = portfolio["inventory"].setdefault(
        mkt["token"], {"title": mkt["title"], "shares": 0.0, "cost": 0.0})
    inv_usd = inv["shares"] * ((mkt["bid"] + mkt["ask"]) / 2)
    our_bid, our_ask = make_quotes(mkt, inv_usd)
    if our_bid is None:
        return
    size = min(QUOTE_SIZE_USD, trade_size_usd)

    if trade_price <= our_bid and inv_usd < MAX_INVENTORY_USD and portfolio["cash"] >= size:
        shares = size / our_bid
        portfolio["cash"] -= size
        inv["shares"] += shares
        inv["cost"] += size
        portfolio["fills"].append({"t": _now(), "title": mkt["title"],
                                   "side": "BUY", "price": our_bid, "usd": size})
        emit(f"FILL buy {shares:.1f}sh '{mkt['title']}' @ {our_bid:.3f}", "trade")

    elif trade_price >= our_ask and inv["shares"] > 0:
        shares = min(inv["shares"], size / our_ask)
        proceeds = shares * our_ask
        avg_cost = inv["cost"] / inv["shares"]
        pnl = proceeds - shares * avg_cost
        inv["cost"] -= shares * avg_cost
        inv["shares"] -= shares
        portfolio["cash"] += proceeds
        portfolio["fills"].append({"t": _now(), "title": mkt["title"],
                                   "side": "SELL", "price": our_ask,
                                   "usd": round(proceeds, 2), "pnl": round(pnl, 2)})
        emit(f"FILL sell {shares:.1f}sh '{mkt['title']}' @ {our_ask:.3f} "
             f"→ P&L {pnl:+.2f}", "trade")


def _now():
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M")


def panic_check(portfolio, mkt):
    """Market nearly decided? Dump inventory at the bid and stop quoting."""
    mid = (mkt["bid"] + mkt["ask"]) / 2
    if PANIC_PRICE < mid < 1 - PANIC_PRICE:
        return False
    inv = portfolio["inventory"].get(mkt["token"])
    if inv and inv["shares"] > 0:
        proceeds = inv["shares"] * mkt["bid"]
        pnl = proceeds - inv["cost"]
        portfolio["cash"] += proceeds
        portfolio["fills"].append({"t": _now(), "title": mkt["title"],
                                   "side": "PANIC SELL", "price": mkt["bid"],
                                   "usd": round(proceeds, 2), "pnl": round(pnl, 2)})
        emit(f"PANIC: '{mkt['title']}' nearly decided — dumped at "
             f"{mkt['bid']:.3f}, P&L {pnl:+.2f}", "error")
        inv["shares"] = inv["cost"] = 0.0
    return True


# =========================== LIVE FEED ============================

def live_feed_listener(ctx):
    """Real trades pushed to us instantly — this is what fills our quotes."""
    while True:
        try:
            ws = websocket.create_connection(LIVE_FEED_URL, timeout=10)
            ws.send(json.dumps({"action": "subscribe", "subscriptions":
                                [{"topic": "activity", "type": "trades"}]}))
            emit("live feed connected — quotes can now be filled", "good")
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
                for p in (msg if isinstance(msg, list) else [msg]):
                    body = p.get("payload", p) if isinstance(p, dict) else None
                    if not isinstance(body, dict):
                        continue
                    token = body.get("asset")
                    with LOCK:
                        mkts = {m["token"]: m for m in STATE["markets"]}
                    mkt = mkts.get(token)
                    if not mkt:
                        continue
                    try:
                        price = float(body.get("price", 0))
                        usd = float(body.get("size", 0)) * price
                    except (TypeError, ValueError):
                        continue
                    if not (0 < price < 1) or usd <= 0:
                        continue
                    with TRADE_LOCK:
                        on_market_trade(ctx["portfolio"], mkt, price, usd)
        except Exception as e:
            emit(f"live feed dropped ({e}) — reconnecting in 5s")
        time.sleep(5)


# =========================== BOT THREAD ===========================

def bot_loop():
    portfolio = load_json(PORTFOLIO_FILE,
                          {"cash": STARTING_BANKROLL, "inventory": {}, "fills": []})
    with LOCK:
        STATE["portfolio"] = portfolio

    emit("market maker started — PAPER MODE, no real money anywhere")
    if not HAVE_WS:
        emit("live feed missing — run 'pip install websocket-client'", "error")
        return

    markets = pick_markets()
    with LOCK:
        STATE["markets"] = markets
    ctx = {"portfolio": portfolio}
    threading.Thread(target=live_feed_listener, args=(ctx,), daemon=True).start()

    last_refresh = time.time()
    last_beat = time.time()
    last_save = 0.0

    while True:
        try:
            with LOCK:
                STATE["stage"] = "quote"

            # hourly: re-pick which markets to stand in
            if time.time() - last_refresh > REFRESH_MARKETS_MIN * 60:
                last_refresh = time.time()
                markets = pick_markets()

            # every few seconds: refresh books, requote, panic-check
            still = []
            for mkt in markets:
                book = get(f"{CLOB_API}/book", {"token_id": mkt["token"]},
                           label="book", quiet=True)
                if book:
                    try:
                        mkt["bid"] = float(book["bids"][0]["price"])
                        mkt["ask"] = float(book["asks"][0]["price"])
                        mkt["spread"] = round(mkt["ask"] - mkt["bid"], 3)
                    except (KeyError, IndexError, TypeError, ValueError):
                        pass
                with TRADE_LOCK:
                    dropped = panic_check(portfolio, mkt)
                if not dropped:
                    inv = portfolio["inventory"].get(mkt["token"], {"shares": 0})
                    mid = (mkt["bid"] + mkt["ask"]) / 2
                    b, a = make_quotes(mkt, inv["shares"] * mid)
                    mkt["our_bid"], mkt["our_ask"] = b, a
                    still.append(mkt)
            markets = still

            with LOCK:
                STATE["markets"] = markets

            if time.time() - last_beat > 300:
                last_beat = time.time()
                fills = len(portfolio["fills"])
                emit(f"heartbeat: alive, quoting {len(markets)} markets, "
                     f"{fills} fills so far", "good")

            if time.time() - last_save > 60:
                last_save = time.time()
                # account value = cash + inventory at current mid
                value = portfolio["cash"]
                for mkt in markets:
                    inv = portfolio["inventory"].get(mkt["token"])
                    if inv:
                        value += inv["shares"] * (mkt["bid"] + mkt["ask"]) / 2
                for tok, inv in portfolio["inventory"].items():
                    if tok not in {m["token"] for m in markets} and inv["shares"] > 0:
                        value += inv["cost"]   # market we no longer see: value at cost
                with LOCK:
                    STATE["equity"].append(
                        {"t": datetime.now(timezone.utc).strftime("%H:%M"),
                         "v": round(value, 2)})
                    STATE["portfolio"] = portfolio
                save_json(PORTFOLIO_FILE, portfolio)
        except Exception as e:
            emit(f"bot loop crashed and recovered: {e}", "error")
        time.sleep(REQUOTE_SECONDS)


# =========================== DASHBOARD ============================

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Maker — paper desk</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root{--ink:#0d1117;--panel:#161b22;--line:#21262d;--text:#e6edf3;
        --dim:#8b949e;--up:#3fb950;--down:#f85149;--amber:#d29922;--blue:#58a6ff}
  *{box-sizing:border-box;margin:0}
  body{background:var(--ink);color:var(--text);
       font:14px/1.5 "SF Mono","Cascadia Mono",Consolas,monospace;padding:16px}
  h1{font-size:15px;font-weight:600;letter-spacing:.04em}
  .paper{color:var(--amber);border:1px solid var(--amber);border-radius:3px;
         padding:1px 7px;font-size:11px;margin-left:10px;vertical-align:2px}
  .grid{display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-top:14px}
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
  .up{color:var(--up)}.down{color:var(--down)}
  #log{height:300px;overflow-y:auto;font-size:12px}
  #log p{padding:2px 0;border-bottom:1px solid var(--line)}
  #log .error{color:var(--down)}#log .good{color:var(--up)}#log .trade{color:var(--blue)}
  #log time{color:var(--dim);margin-right:8px}
  #chartbox{height:200px}
  .empty{color:var(--dim);font-size:12.5px;padding:8px 0}
</style></head><body>
<h1>MARKET MAKER<span class="paper">PAPER MONEY</span>
    <span id="err" style="float:right;color:var(--down);font-size:12px"></span></h1>
<div class="grid">
 <div>
  <div class="card"><h2>Paper account</h2>
    <div class="stats">
      <div><b id="equity">—</b><span>account value $</span></div>
      <div><b id="cash">—</b><span>cash $</span></div>
      <div><b id="fills">—</b><span>fills</span></div>
      <div><b id="pnl">—</b><span>realised P&L $</span></div>
    </div>
    <div id="chartbox"><canvas id="chart"></canvas></div>
  </div>
  <div class="card" style="margin-top:14px"><h2>Live actions & bugs</h2><div id="log"></div></div>
 </div>
 <div>
  <div class="card"><h2>Quoting (our shop prices)</h2><div id="mkts"></div></div>
  <div class="card" style="margin-top:14px"><h2>Recent fills</h2><div id="fillt"></div></div>
 </div>
</div>
<script>
let chart;
const money=x=>(x>=0?"+":"")+x.toFixed(2);
async function tick(){
  let s; try{s=await(await fetch("/status")).json()}catch(e){return}
  document.getElementById("err").textContent=s.errors?s.errors+" errors — see log":"";
  const p=s.portfolio, fills=p.fills||[];
  const realised=fills.reduce((a,f)=>a+(f.pnl||0),0);
  const eq=s.equity.length?s.equity[s.equity.length-1].v:p.cash;
  equity.textContent=eq.toFixed(2); cash.textContent=p.cash.toFixed(2);
  document.getElementById("fills").textContent=fills.length;
  const pe=document.getElementById("pnl");
  pe.textContent=money(realised); pe.className=realised>=0?"up":"down";

  mkts.innerHTML=s.markets.length?"<table><tr><th>market</th><th class=num>we buy</th><th class=num>we sell</th></tr>"+
    s.markets.map(m=>`<tr><td>${m.title}</td><td class="num up">${m.our_bid??"—"}</td><td class="num down">${m.our_ask??"—"}</td></tr>`).join("")+"</table>"
    :"<div class=empty>Selecting markets…</div>";

  fillt.innerHTML=fills.length?"<table><tr><th>time</th><th>market</th><th>side</th><th class=num>P&L $</th></tr>"+
    fills.slice(-12).reverse().map(f=>`<tr><td>${f.t}</td><td>${f.title.slice(0,34)}</td><td>${f.side}</td><td class="num ${(f.pnl||0)>=0?'up':'down'}">${f.pnl!=null?money(f.pnl):""}</td></tr>`).join("")+"</table>"
    :"<div class=empty>No fills yet — offers are posted, waiting for traders.</div>";

  log.innerHTML=s.events.map(e=>`<p class="${e.level}"><time>${e.t}</time>${e.msg}</p>`).join("");

  const labels=s.equity.map(e=>e.t),values=s.equity.map(e=>e.v);
  if(!chart){chart=new Chart(document.getElementById("chart"),{type:"line",
    data:{labels,datasets:[{data:values,borderColor:"#58a6ff",borderWidth:1.5,
      pointRadius:0,fill:true,backgroundColor:"rgba(88,166,255,.08)",tension:.25}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:"#8b949e",maxTicksLimit:8},grid:{color:"#21262d"}},
              y:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}}}}})}
  else{chart.data.labels=labels;chart.data.datasets[0].data=values;chart.update("none")}
}
tick(); setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/status":
            with LOCK:
                body = json.dumps({
                    "errors": STATE["errors"], "events": list(STATE["events"]),
                    "markets": STATE["markets"], "portfolio": STATE["portfolio"],
                    "equity": list(STATE["equity"]),
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
            save_json(PORTFOLIO_FILE, STATE["portfolio"])
        emit("closed by user — portfolio saved")


if __name__ == "__main__":
    main()