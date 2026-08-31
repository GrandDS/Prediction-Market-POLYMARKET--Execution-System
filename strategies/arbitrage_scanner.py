"""
============================================================
 POLYMARKET YES+NO ARBITRAGE SCANNER  (Paper Trading v1)
============================================================

STRATEGY
--------
On a binary market, 1 YES share + 1 NO share always pays exactly
$1.00 at resolution, no matter the outcome.

  * BUY-ARB : if best_ask(YES) + best_ask(NO) < $1.00 (after fees),
              buy BOTH sides. Hold to resolution. Profit is locked
              in at entry -- there is no directional risk.

  * SELL-ARB: if best_bid(YES) + best_bid(NO) > $1.00 (after fees),
              mint a YES+NO pair for $1.00 and sell both legs.
              Profit is realized immediately.

DATA SOURCES
------------
  * Gamma API (public)  -> market discovery + resolution status
  * CLOB API  (public)  -> LIVE order books (real asks/bids + depth)

Trade decisions are made ONLY from live CLOB order-book prices,
never from Gamma's delayed aggregate prices.

PAPER MODE
----------
No wallet, no keys, no real orders. Every "fill" is simulated at
the live best ask/bid, capped by the visible book depth, so the
results are as honest as paper trading can be. All detected
opportunities (traded or not) are written to CSV so you can
measure how often gaps appear and how big they really are --
that data decides whether going live is worth it.
============================================================
"""

import sys
import os
import csv
import time
import json
import queue
import logging
import requests
from datetime import datetime, timezone
from collections import deque
from logging.handlers import QueueHandler, QueueListener
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter
)

# Optional live charts (pip install pyqtgraph). Scanner works fine without it.
try:
    import pyqtgraph as pg
    import numpy as np
    HAS_PYQTGRAPH = True
except ImportError:
    HAS_PYQTGRAPH = False

# ============================================================
# CONFIG
# ============================================================

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# --- cadence ---
SCAN_INTERVAL_SECONDS = 10          # price scan cycle
DISCOVERY_INTERVAL_SECONDS = 300    # re-fetch the market catalogue every 5 min
RESOLUTION_CHECK_INTERVAL_SECONDS = 600  # check open positions for resolution every 10 min
REQUEST_TIMEOUT = 20

# --- market discovery filters ---
PAGE_LIMIT = 200
MAX_PAGES = 5
MIN_LIQUIDITY = 500.0               # ignore very thin markets (spread illusion risk)
MIN_VOLUME24H = 100.0
MIN_HOURS_TO_END = 1.0              # skip markets resolving within the hour
TOP_MARKETS_BY_VOLUME = 300         # cap tracked markets (=> 600 tokens per scan)

# --- opportunity thresholds ---
MIN_GROSS_GAP = 0.004               # raw gap before fees needed to even fetch full books
MIN_NET_PROFIT_PCT = 1.0            # % net profit (after fees) required to paper-trade
MAX_BOOKS_PER_CYCLE = 12            # politeness cap on full order-book fetches per cycle

# --- fees (category-aware model of Polymarket's 2026 dynamic taker fee) ---
# fee_per_share = rate_const * p * (1 - p), peaking at p = 0.5.
# Peak effective fees by category (2026): Crypto ~1.8%, Economics ~1.5%,
# Culture/Weather ~1.25%, Politics/Finance/Tech ~1.0%, Sports ~0.75%,
# Geopolitics free. rate_const = 4 x peak (since p(1-p) peaks at 0.25).
# Paper mode assumes worst case: both legs filled as TAKER.
FEE_CONST_BY_CATEGORY = {
    # NOTE: checked in order -- "geopolitics" MUST precede "politics"
    # because the lookup is substring-based.
    "geopolitics": 0.0,
    "crypto": 0.072,
    "economics": 0.060,
    "economy": 0.060,
    "culture": 0.050,
    "weather": 0.050,
    "mentions": 0.064,
    "politics": 0.040,
    "finance": 0.040,
    "tech": 0.040,
    "sports": 0.030,
}
FEE_CONST_DEFAULT = 0.072   # unknown category -> assume worst (crypto) rate

# --- paper sizing ---
STARTING_BALANCE = 1000.0
MAX_NOTIONAL_PER_ARB = 200.0        # max $ deployed into one arb
MIN_SHARES_TO_TRADE = 5.0           # ignore gaps with almost no depth
MARKET_COOLDOWN_MINUTES = 30        # after trading a market, ignore it for a while

HTTP_RETRY_TOTAL = 3
HTTP_BACKOFF_FACTOR = 0.7

LOG_DIR = "logs"
TRADE_DIR = "trades"


# ============================================================
# LOGGING
# ============================================================

class QtLogEmitter(logging.Handler):
    def __init__(self, signal):
        super().__init__()
        self._signal = signal

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._signal.emit(self.format(record))
        except Exception:
            pass


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def make_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def build_logging_system(gui_signal):
    ensure_dir(LOG_DIR)
    log_path = os.path.join(LOG_DIR, f"arb_scanner_{make_timestamp()}.log")

    logger = logging.getLogger("arb_scanner")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)

    q = queue.Queue()
    logger.addHandler(QueueHandler(q))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)

    gui_handler = QtLogEmitter(gui_signal)
    gui_handler.setFormatter(fmt)

    listener = QueueListener(q, file_handler, gui_handler)
    listener.start()

    logger.info(f"Logging started. File: {log_path}")
    return logger, listener, file_handler, log_path


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class MarketInfo:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    liquidity: float
    volume24h: float
    end_date_iso: Optional[str]
    category: str = ""


@dataclass
class Opportunity:
    """A detected (book-verified) arbitrage gap -- may or may not be traded."""
    found_at: float
    condition_id: str
    question: str
    arb_type: str            # "BUY" or "SELL"
    yes_price: float         # ask for BUY-arb, bid for SELL-arb
    no_price: float
    price_sum: float
    gross_gap: float         # |1 - sum|
    depth_shares: float      # max shares fillable at these prices
    est_fees_per_share: float
    net_profit_per_share: float
    net_profit_pct: float
    traded: bool = False


@dataclass
class PaperPosition:
    """An open BUY-arb: long equal shares of YES and NO, held to resolution."""
    position_id: int
    condition_id: str
    question: str
    entry_time: float
    shares: float
    yes_cost: float          # ask price paid per YES share
    no_cost: float
    total_cost: float        # shares * (yes_cost + no_cost)
    fees: float
    locked_payout: float     # shares * $1.00 at resolution
    locked_net: float        # locked_payout - total_cost - fees
    end_date_iso: Optional[str]


@dataclass
class CompletedTrade:
    position_id: int
    condition_id: str
    question: str
    arb_type: str            # "BUY (resolved)" or "SELL (instant)"
    entry_time: float
    close_time: float
    shares: float
    total_cost: float
    fees: float
    payout: float
    net_pnl: float


# ============================================================
# FEE MODEL
# ============================================================

def fee_const_for_category(category: str) -> float:
    key = (category or "").strip().lower()
    for name, const in FEE_CONST_BY_CATEGORY.items():
        if name in key:
            return const
    return FEE_CONST_DEFAULT


def taker_fee_per_share(price: float, rate_const: float = FEE_CONST_DEFAULT) -> float:
    """Polymarket's dynamic taker fee: fee = rate_const * p * (1-p),
    peaking at p = 0.5, falling to ~0 near the extremes."""
    p = min(max(price, 0.0), 1.0)
    return rate_const * p * (1.0 - p)


# ============================================================
# API FEEDS
# ============================================================

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "arb-scanner-paper/1.0"})
    retry = Retry(
        total=HTTP_RETRY_TOTAL,
        connect=HTTP_RETRY_TOTAL,
        read=HTTP_RETRY_TOTAL,
        status=HTTP_RETRY_TOTAL,
        backoff_factor=HTTP_BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class GammaFeed:
    """Market discovery + resolution status (public, no auth)."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.session = build_session()

    @staticmethod
    def _safe_float(value, default=0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _parse_json_list(value) -> List:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    def fetch_page(self, offset: int) -> List[dict]:
        params = {"active": "true", "closed": "false", "limit": PAGE_LIMIT, "offset": offset}
        r = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def _hours_to_end(self, market: dict) -> Optional[float]:
        raw = market.get("endDateIso") or market.get("endDate")
        if not raw:
            return None
        try:
            s = str(raw).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 3600.0
        except Exception:
            return None

    def discover_markets(self) -> List[MarketInfo]:
        """Fetch the catalogue and keep tradeable binary YES/NO markets."""
        raw: List[dict] = []
        for page in range(MAX_PAGES):
            try:
                batch = self.fetch_page(offset=page * PAGE_LIMIT)
            except Exception as exc:
                self.logger.warning(f"Gamma page {page} fetch failed: {exc}")
                break
            if not batch:
                break
            raw.extend(batch)

        markets: List[MarketInfo] = []
        for m in raw:
            try:
                if m.get("closed") is True or m.get("archived") is True:
                    continue
                if m.get("enableOrderBook") is False:
                    continue

                outcomes = [str(o).strip().upper() for o in self._parse_json_list(m.get("outcomes"))]
                token_ids = [str(t) for t in self._parse_json_list(m.get("clobTokenIds"))]
                if outcomes != ["YES", "NO"] or len(token_ids) != 2:
                    continue

                liquidity = self._safe_float(m.get("liquidityNum"), self._safe_float(m.get("liquidity")))
                vol24 = self._safe_float(m.get("volume24hr"), self._safe_float(m.get("volume24h")))
                if liquidity < MIN_LIQUIDITY or vol24 < MIN_VOLUME24H:
                    continue

                hours = self._hours_to_end(m)
                if hours is not None and hours < MIN_HOURS_TO_END:
                    continue

                condition_id = str(m.get("conditionId") or m.get("id") or "")
                question = str(m.get("question", "")).strip()
                if not condition_id or len(question) < 5:
                    continue

                markets.append(MarketInfo(
                    condition_id=condition_id,
                    question=question,
                    yes_token=token_ids[0],
                    no_token=token_ids[1],
                    liquidity=liquidity,
                    volume24h=vol24,
                    end_date_iso=str(m.get("endDateIso") or m.get("endDate") or "") or None,
                    category=str(m.get("category", "")),
                ))
            except Exception as exc:
                self.logger.warning(f"Market parse skipped: {exc}")

        markets.sort(key=lambda x: x.volume24h, reverse=True)
        return markets[:TOP_MARKETS_BY_VOLUME]

    def check_resolved(self, condition_id: str) -> Optional[bool]:
        """True if the market is closed/resolved, False if still live, None on error."""
        try:
            r = self.session.get(
                f"{GAMMA_API}/markets",
                params={"condition_ids": condition_id},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
            if row is None:
                return None
            if row.get("closed") is True:
                return True
            status = str(row.get("umaResolutionStatus", "")).lower()
            if "resolved" in status:
                return True
            return False
        except Exception as exc:
            self.logger.warning(f"Resolution check failed for {condition_id}: {exc}")
            return None


class ClobFeed:
    """LIVE order-book data from the CLOB (public endpoints, no auth)."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.session = build_session()

    def batch_prices(self, token_ids: List[str], side: str) -> Dict[str, float]:
        """
        Best price for many tokens in one call.
        NOTE side refers to the side of the ORDER BOOK, not your action:
          side='BUY'  -> best BID  (what you'd RECEIVE selling now)
          side='SELL' -> best ASK  (what you'd PAY buying now)
        Returns {token_id: price}; tokens with no liquidity are omitted.
        """
        out: Dict[str, float] = {}
        CHUNK = 100
        for i in range(0, len(token_ids), CHUNK):
            chunk = token_ids[i:i + CHUNK]
            payload = [{"token_id": t, "side": side} for t in chunk]
            try:
                r = self.session.post(f"{CLOB_API}/prices", json=payload, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    continue
                for token_id, entry in data.items():
                    price = None
                    if isinstance(entry, dict):
                        price = entry.get(side) or entry.get(side.lower()) or entry.get("price")
                    elif isinstance(entry, (str, float, int)):
                        price = entry
                    try:
                        p = float(price)
                        if 0.0 < p < 1.0:
                            out[str(token_id)] = p
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                self.logger.warning(f"CLOB batch prices failed (chunk {i//CHUNK}): {exc}")
        return out

    def get_book_top(self, token_id: str) -> Optional[dict]:
        """Best ask/bid AND their sizes from the full order book."""
        try:
            r = self.session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            book = r.json()
            asks = book.get("asks") or []
            bids = book.get("bids") or []

            def levels(rows):
                out = []
                for row in rows:
                    try:
                        out.append((float(row["price"]), float(row["size"])))
                    except Exception:
                        pass
                return out

            ask_levels = levels(asks)
            bid_levels = levels(bids)
            best_ask = min(ask_levels, key=lambda x: x[0]) if ask_levels else None
            best_bid = max(bid_levels, key=lambda x: x[0]) if bid_levels else None
            return {
                "best_ask": best_ask[0] if best_ask else None,
                "ask_size": best_ask[1] if best_ask else 0.0,
                "best_bid": best_bid[0] if best_bid else None,
                "bid_size": best_bid[1] if best_bid else 0.0,
            }
        except Exception as exc:
            self.logger.warning(f"CLOB book fetch failed for {token_id}: {exc}")
            return None


# ============================================================
# ARBITRAGE ENGINE (paper)
# ============================================================

class ArbEngine:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.cash = STARTING_BALANCE
        self.position_counter = 0
        self.open_positions: Dict[int, PaperPosition] = {}
        self.completed: List[CompletedTrade] = []
        self.opportunities_log: deque = deque(maxlen=500)
        self.market_cooldown_until: Dict[str, float] = {}
        self.total_opps_found = 0
        self.total_buy_opps = 0
        self.total_sell_opps = 0

        ensure_dir(TRADE_DIR)
        stamp = f"{make_timestamp()}_{time.time_ns() % 1_000_000}"
        self.opps_csv = os.path.join(TRADE_DIR, f"arb_opportunities_{stamp}.csv")
        self.trades_csv = os.path.join(TRADE_DIR, f"arb_trades_{stamp}.csv")
        self._init_csvs()

    def _init_csvs(self):
        with open(self.opps_csv, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "found_at_utc", "condition_id", "question", "arb_type",
                "yes_price", "no_price", "price_sum", "gross_gap",
                "depth_shares", "est_fees_per_share",
                "net_profit_per_share", "net_profit_pct", "traded"
            ])
        with open(self.trades_csv, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "position_id", "condition_id", "question", "arb_type",
                "entry_time_utc", "close_time_utc", "shares",
                "total_cost", "fees", "payout", "net_pnl"
            ])

    # ---------- bookkeeping ----------

    def in_cooldown(self, condition_id: str, now_ts: float) -> bool:
        return now_ts < self.market_cooldown_until.get(condition_id, 0.0)

    def has_open_position(self, condition_id: str) -> bool:
        return any(p.condition_id == condition_id for p in self.open_positions.values())

    def locked_profit_open(self) -> float:
        return sum(p.locked_net for p in self.open_positions.values())

    def realized_pnl(self) -> float:
        return sum(t.net_pnl for t in self.completed)

    def capital_deployed(self) -> float:
        return sum(p.total_cost + p.fees for p in self.open_positions.values())

    def _log_opportunity(self, opp: Opportunity):
        self.opportunities_log.appendleft(opp)
        self.total_opps_found += 1
        if opp.arb_type == "BUY":
            self.total_buy_opps += 1
        else:
            self.total_sell_opps += 1
        with open(self.opps_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.fromtimestamp(opp.found_at, tz=timezone.utc).isoformat(sep=" ", timespec="seconds"),
                opp.condition_id, opp.question, opp.arb_type,
                f"{opp.yes_price:.4f}", f"{opp.no_price:.4f}",
                f"{opp.price_sum:.4f}", f"{opp.gross_gap:.4f}",
                f"{opp.depth_shares:.2f}", f"{opp.est_fees_per_share:.5f}",
                f"{opp.net_profit_per_share:.5f}", f"{opp.net_profit_pct:.3f}",
                opp.traded,
            ])

    def _log_completed(self, t: CompletedTrade):
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                t.position_id, t.condition_id, t.question, t.arb_type,
                datetime.fromtimestamp(t.entry_time, tz=timezone.utc).isoformat(sep=" ", timespec="seconds"),
                datetime.fromtimestamp(t.close_time, tz=timezone.utc).isoformat(sep=" ", timespec="seconds"),
                f"{t.shares:.4f}", f"{t.total_cost:.4f}", f"{t.fees:.4f}",
                f"{t.payout:.4f}", f"{t.net_pnl:.4f}",
            ])

    # ---------- evaluation ----------

    def evaluate_buy_arb(self, market: MarketInfo, yes_book: dict, no_book: dict,
                         now_ts: float) -> Optional[Opportunity]:
        """BUY-arb: buy YES at ask + NO at ask, guaranteed $1/share at resolution."""
        ay, an = yes_book.get("best_ask"), no_book.get("best_ask")
        if ay is None or an is None:
            return None
        price_sum = ay + an
        if price_sum >= 1.0:
            return None

        rate = fee_const_for_category(market.category)
        fees_ps = taker_fee_per_share(ay, rate) + taker_fee_per_share(an, rate)
        net_ps = 1.0 - price_sum - fees_ps
        if net_ps <= 0:
            return None

        depth = min(yes_book.get("ask_size", 0.0), no_book.get("ask_size", 0.0))
        net_pct = (net_ps / price_sum) * 100.0

        return Opportunity(
            found_at=now_ts, condition_id=market.condition_id, question=market.question,
            arb_type="BUY", yes_price=ay, no_price=an, price_sum=price_sum,
            gross_gap=1.0 - price_sum, depth_shares=depth,
            est_fees_per_share=fees_ps, net_profit_per_share=net_ps,
            net_profit_pct=net_pct,
        )

    def evaluate_sell_arb(self, market: MarketInfo, yes_book: dict, no_book: dict,
                          now_ts: float) -> Optional[Opportunity]:
        """SELL-arb: mint YES+NO for $1, sell both at bid for > $1. Instant profit."""
        by, bn = yes_book.get("best_bid"), no_book.get("best_bid")
        if by is None or bn is None:
            return None
        price_sum = by + bn
        if price_sum <= 1.0:
            return None

        rate = fee_const_for_category(market.category)
        fees_ps = taker_fee_per_share(by, rate) + taker_fee_per_share(bn, rate)
        net_ps = price_sum - 1.0 - fees_ps
        if net_ps <= 0:
            return None

        depth = min(yes_book.get("bid_size", 0.0), no_book.get("bid_size", 0.0))
        net_pct = net_ps * 100.0  # cost basis is the $1 mint

        return Opportunity(
            found_at=now_ts, condition_id=market.condition_id, question=market.question,
            arb_type="SELL", yes_price=by, no_price=bn, price_sum=price_sum,
            gross_gap=price_sum - 1.0, depth_shares=depth,
            est_fees_per_share=fees_ps, net_profit_per_share=net_ps,
            net_profit_pct=net_pct,
        )

    # ---------- paper execution ----------

    def maybe_paper_trade(self, opp: Opportunity, market: MarketInfo, now_ts: float) -> bool:
        """Simulate a fill if the opportunity clears all thresholds."""
        if opp.net_profit_pct < MIN_NET_PROFIT_PCT:
            self._log_opportunity(opp)
            return False
        if self.has_open_position(opp.condition_id) or self.in_cooldown(opp.condition_id, now_ts):
            self._log_opportunity(opp)
            return False
        if opp.depth_shares < MIN_SHARES_TO_TRADE:
            self._log_opportunity(opp)
            return False

        if opp.arb_type == "BUY":
            cost_per_share = opp.price_sum
            max_by_cash = (self.cash * 0.95) / max(cost_per_share, 1e-9)
            shares = min(opp.depth_shares, MAX_NOTIONAL_PER_ARB / max(cost_per_share, 1e-9), max_by_cash)
            if shares < MIN_SHARES_TO_TRADE:
                self._log_opportunity(opp)
                return False

            total_cost = shares * cost_per_share
            fees = shares * opp.est_fees_per_share
            locked_payout = shares * 1.0
            locked_net = locked_payout - total_cost - fees

            self.position_counter += 1
            pos = PaperPosition(
                position_id=self.position_counter,
                condition_id=opp.condition_id, question=opp.question,
                entry_time=now_ts, shares=shares,
                yes_cost=opp.yes_price, no_cost=opp.no_price,
                total_cost=total_cost, fees=fees,
                locked_payout=locked_payout, locked_net=locked_net,
                end_date_iso=market.end_date_iso,
            )
            self.open_positions[pos.position_id] = pos
            self.cash -= (total_cost + fees)
            self.market_cooldown_until[opp.condition_id] = now_ts + MARKET_COOLDOWN_MINUTES * 60
            opp.traded = True
            self._log_opportunity(opp)
            self.logger.info(
                f"PAPER BUY-ARB #{pos.position_id} | {shares:.1f} sh | "
                f"YES@{opp.yes_price:.3f} + NO@{opp.no_price:.3f} = {opp.price_sum:.4f} | "
                f"locked net = ${locked_net:.2f} ({opp.net_profit_pct:.2f}%) | {opp.question}"
            )
            return True

        # SELL-arb: instant realization (mint $1, sell both legs)
        shares = min(opp.depth_shares, MAX_NOTIONAL_PER_ARB, (self.cash * 0.95))
        if shares < MIN_SHARES_TO_TRADE:
            self._log_opportunity(opp)
            return False

        mint_cost = shares * 1.0
        proceeds = shares * opp.price_sum
        fees = shares * opp.est_fees_per_share
        net = proceeds - mint_cost - fees

        self.position_counter += 1
        trade = CompletedTrade(
            position_id=self.position_counter,
            condition_id=opp.condition_id, question=opp.question,
            arb_type="SELL (instant)",
            entry_time=now_ts, close_time=now_ts,
            shares=shares, total_cost=mint_cost, fees=fees,
            payout=proceeds, net_pnl=net,
        )
        self.completed.append(trade)
        self.cash += net
        self.market_cooldown_until[opp.condition_id] = now_ts + MARKET_COOLDOWN_MINUTES * 60
        opp.traded = True
        self._log_opportunity(opp)
        self._log_completed(trade)
        self.logger.info(
            f"PAPER SELL-ARB #{trade.position_id} | {shares:.1f} sh | "
            f"bids {opp.yes_price:.3f}+{opp.no_price:.3f}={opp.price_sum:.4f} | "
            f"net = ${net:.2f} | {opp.question}"
        )
        return True

    def resolve_position(self, position_id: int, now_ts: float):
        pos = self.open_positions.pop(position_id, None)
        if pos is None:
            return
        payout = pos.shares * 1.0
        net = payout - pos.total_cost - pos.fees
        self.cash += payout
        trade = CompletedTrade(
            position_id=pos.position_id, condition_id=pos.condition_id,
            question=pos.question, arb_type="BUY (resolved)",
            entry_time=pos.entry_time, close_time=now_ts,
            shares=pos.shares, total_cost=pos.total_cost, fees=pos.fees,
            payout=payout, net_pnl=net,
        )
        self.completed.append(trade)
        self._log_completed(trade)
        self.logger.info(
            f"RESOLVED #{pos.position_id} | payout ${payout:.2f} | net ${net:.2f} | {pos.question}"
        )

    def summary(self) -> dict:
        return {
            "cash": self.cash,
            "deployed": self.capital_deployed(),
            "open_positions": len(self.open_positions),
            "locked_profit": self.locked_profit_open(),
            "realized_pnl": self.realized_pnl(),
            "completed": len(self.completed),
            "opps_found": self.total_opps_found,
            "buy_opps": self.total_buy_opps,
            "sell_opps": self.total_sell_opps,
            "equity": self.cash + self.capital_deployed() + self.locked_profit_open(),
        }


# ============================================================
# WORKER THREAD
# ============================================================

class ScannerWorker(QThread):
    status_signal = pyqtSignal(str)
    opps_signal = pyqtSignal(list)
    positions_signal = pyqtSignal(list)
    completed_signal = pyqtSignal(list)
    stats_signal = pyqtSignal(dict)
    heartbeat_signal = pyqtSignal(str)
    chart_signal = pyqtSignal(dict)

    def __init__(self, logger: logging.Logger):
        super().__init__()
        self.logger = logger
        self.gamma = GammaFeed(logger)
        self.clob = ClobFeed(logger)
        self.engine = ArbEngine(logger)
        self._stop = False
        self._cycle = 0
        self.markets: List[MarketInfo] = []
        self.markets_by_id: Dict[str, MarketInfo] = {}
        self.last_discovery = 0.0
        self.last_resolution_check = 0.0
        self.closest_ask_sum: Optional[float] = None
        self.closest_bid_sum: Optional[float] = None

    def request_stop(self):
        self._stop = True

    # ---------- pipeline steps ----------

    def _discover(self, now_ts: float):
        self.logger.info("Discovering markets from Gamma...")
        markets = self.gamma.discover_markets()
        if markets:
            self.markets = markets
            self.markets_by_id = {m.condition_id: m for m in markets}
            self.logger.info(f"Tracking {len(markets)} markets (top by 24h volume).")
        else:
            self.logger.warning("Discovery returned 0 markets; keeping previous list.")
        self.last_discovery = now_ts

    def _scan_for_gaps(self, now_ts: float) -> List[Opportunity]:
        """Batch-fetch best prices for all tracked tokens, then verify
        candidates against full order books (depth included)."""
        if not self.markets:
            return []

        all_tokens: List[str] = []
        for m in self.markets:
            all_tokens.extend([m.yes_token, m.no_token])

        # CLOB /prices side semantics (confirmed against live behavior):
        #   side=BUY  -> best BID (highest resting buy order)
        #   side=SELL -> best ASK (lowest resting sell order)
        # So: the price you PAY to buy now is the ASK  -> request side="SELL"
        #     the price you GET selling now is the BID -> request side="BUY"
        asks = self.clob.batch_prices(all_tokens, side="SELL")
        bids = self.clob.batch_prices(all_tokens, side="BUY")

        buy_candidates: List[Tuple[float, MarketInfo]] = []
        sell_candidates: List[Tuple[float, MarketInfo]] = []

        # visibility: how close is the market to an arb right now?
        self.closest_ask_sum = None   # lowest ask(YES)+ask(NO) seen (arb if < 1)
        self.closest_bid_sum = None   # highest bid(YES)+bid(NO) seen (arb if > 1)

        for m in self.markets:
            ay, an = asks.get(m.yes_token), asks.get(m.no_token)
            if ay is not None and an is not None:
                s = ay + an
                if self.closest_ask_sum is None or s < self.closest_ask_sum:
                    self.closest_ask_sum = s
                gap = 1.0 - s
                if gap >= MIN_GROSS_GAP:
                    buy_candidates.append((gap, m))

            by, bn = bids.get(m.yes_token), bids.get(m.no_token)
            if by is not None and bn is not None:
                s = by + bn
                if self.closest_bid_sum is None or s > self.closest_bid_sum:
                    self.closest_bid_sum = s
                gap = s - 1.0
                if gap >= MIN_GROSS_GAP:
                    sell_candidates.append((gap, m))

        buy_candidates.sort(key=lambda x: x[0], reverse=True)
        sell_candidates.sort(key=lambda x: x[0], reverse=True)

        found: List[Opportunity] = []
        books_used = 0

        for gap, market in buy_candidates + sell_candidates:
            if books_used >= MAX_BOOKS_PER_CYCLE:
                break
            yes_book = self.clob.get_book_top(market.yes_token)
            no_book = self.clob.get_book_top(market.no_token)
            books_used += 2
            if not yes_book or not no_book:
                continue

            is_buy_side = (gap, market) in buy_candidates
            opp = (self.engine.evaluate_buy_arb(market, yes_book, no_book, now_ts)
                   if is_buy_side else
                   self.engine.evaluate_sell_arb(market, yes_book, no_book, now_ts))
            if opp:
                found.append(opp)
                self.engine.maybe_paper_trade(opp, market, now_ts)

        closest_txt = ""
        if self.closest_ask_sum is not None:
            closest_txt += f" | lowest ask-sum={self.closest_ask_sum:.4f} (arb if <1)"
        if self.closest_bid_sum is not None:
            closest_txt += f" | highest bid-sum={self.closest_bid_sum:.4f} (arb if >1)"
        self.logger.info(
            f"Scan: {len(buy_candidates)} buy-gap / {len(sell_candidates)} sell-gap candidates; "
            f"{len(found)} confirmed on live books{closest_txt}"
        )
        return found

    def _check_resolutions(self, now_ts: float):
        if not self.engine.open_positions:
            return
        self.logger.info(f"Checking resolution status of {len(self.engine.open_positions)} open positions...")
        for pid in list(self.engine.open_positions.keys()):
            pos = self.engine.open_positions.get(pid)
            if pos is None:
                continue
            resolved = self.gamma.check_resolved(pos.condition_id)
            if resolved is True:
                self.engine.resolve_position(pid, now_ts)
        self.last_resolution_check = now_ts

    # ---------- render helpers ----------

    def _render_opps(self) -> List[dict]:
        return [{
            "time": datetime.fromtimestamp(o.found_at).strftime("%H:%M:%S"),
            "type": o.arb_type,
            "question": o.question,
            "yes": o.yes_price, "no": o.no_price, "sum": o.price_sum,
            "gap_pct": o.gross_gap * 100.0,
            "net_pct": o.net_profit_pct,
            "depth": o.depth_shares,
            "traded": "YES" if o.traded else "-",
        } for o in list(self.engine.opportunities_log)[:100]]

    def _render_positions(self) -> List[dict]:
        return [{
            "id": p.position_id, "question": p.question,
            "shares": p.shares, "cost": p.total_cost + p.fees,
            "payout": p.locked_payout, "net": p.locked_net,
            "entry": datetime.fromtimestamp(p.entry_time).strftime("%m-%d %H:%M"),
            "ends": (p.end_date_iso or "")[:16],
        } for p in self.engine.open_positions.values()]

    def _render_completed(self) -> List[dict]:
        return [{
            "id": t.position_id, "type": t.arb_type, "question": t.question,
            "shares": t.shares, "cost": t.total_cost + t.fees,
            "payout": t.payout, "net": t.net_pnl,
            "closed": datetime.fromtimestamp(t.close_time).strftime("%m-%d %H:%M"),
        } for t in reversed(self.engine.completed[-50:])]

    # ---------- main loop ----------

    def run(self):
        self.status_signal.emit("Running (paper mode)")
        self.logger.info("Arb scanner started -- PAPER MODE. No real orders will be placed.")

        while not self._stop:
            cycle_start = time.time()
            self._cycle += 1
            try:
                now_ts = time.time()
                self.heartbeat_signal.emit(datetime.now().strftime("%H:%M:%S"))

                if now_ts - self.last_discovery >= DISCOVERY_INTERVAL_SECONDS or not self.markets:
                    self._discover(now_ts)

                found = self._scan_for_gaps(now_ts)

                if now_ts - self.last_resolution_check >= RESOLUTION_CHECK_INTERVAL_SECONDS:
                    self._check_resolutions(now_ts)

                s = self.engine.summary()
                self.logger.info(
                    f"Cycle {self._cycle} | markets={len(self.markets)} | "
                    f"opps_total={s['opps_found']} (buy={s['buy_opps']}, sell={s['sell_opps']}) | "
                    f"open={s['open_positions']} | locked=${s['locked_profit']:.2f} | "
                    f"realized=${s['realized_pnl']:.2f} | equity=${s['equity']:.2f}"
                )

                self.opps_signal.emit(self._render_opps())
                self.positions_signal.emit(self._render_positions())
                self.completed_signal.emit(self._render_completed())
                self.stats_signal.emit(s)
                self.chart_signal.emit({
                    "ts": now_ts,
                    "ask_sum": self.closest_ask_sum,
                    "bid_sum": self.closest_bid_sum,
                    "equity": s["equity"],
                    "new_gaps_pct": [o.gross_gap * 100.0 for o in found],
                })

            except Exception as exc:
                self.logger.exception(f"Cycle error: {exc}")

            elapsed = time.time() - cycle_start
            remaining = max(1.0, SCAN_INTERVAL_SECONDS - elapsed)
            end_sleep = time.time() + remaining
            while time.time() < end_sleep and not self._stop:
                time.sleep(0.25)

        self.status_signal.emit("Stopped")
        self.logger.info("Arb scanner stopped.")


# ============================================================
# CHARTS PANEL (live visualization, requires pyqtgraph)
# ============================================================

class ChartsPanel(QWidget):
    """Three live charts:
      1. Distance-to-arb: lowest ask-sum & highest bid-sum vs the $1.00 line
      2. Gap histogram: size distribution of every book-verified gap
      3. Equity curve: paper cash + locked profit over time
    """

    MAX_POINTS = 8640  # 24h of 10s cycles

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)

        if not HAS_PYQTGRAPH:
            msg = QLabel(
                "Charts require pyqtgraph.\n\n"
                "Install it with:   pip install pyqtgraph\n\n"
                "then restart the app. The scanner itself works fine without it."
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(msg)
            self.enabled = False
            return

        self.enabled = True
        pg.setConfigOptions(antialias=True)

        # buffers
        self.ask_ts, self.ask_vals = deque(maxlen=self.MAX_POINTS), deque(maxlen=self.MAX_POINTS)
        self.bid_ts, self.bid_vals = deque(maxlen=self.MAX_POINTS), deque(maxlen=self.MAX_POINTS)
        self.eq_ts, self.eq_vals = deque(maxlen=self.MAX_POINTS), deque(maxlen=self.MAX_POINTS)
        self.gaps_pct: List[float] = []

        # --- 1. distance to arb ---
        self.p_arb = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
            title="Distance to Arbitrage  —  the closer to the $1.00 line, the closer to a real gap"
        )
        self.p_arb.showGrid(x=True, y=True, alpha=0.25)
        self.p_arb.addLegend(offset=(10, 10))
        self.ask_curve = self.p_arb.plot(
            pen=pg.mkPen("#e15759", width=2), name="lowest ask-sum  (BUY-arb when it dips BELOW 1.00)"
        )
        self.bid_curve = self.p_arb.plot(
            pen=pg.mkPen("#4e79a7", width=2), name="highest bid-sum (SELL-arb when it rises ABOVE 1.00)"
        )
        self.p_arb.addItem(pg.InfiniteLine(
            pos=1.0, angle=0,
            pen=pg.mkPen("#888888", width=1, style=Qt.PenStyle.DashLine),
            label="$1.00", labelOpts={"position": 0.05, "color": "#888888"}
        ))

        # --- 2. gap histogram ---
        self.p_hist = pg.PlotWidget(title="Gap Size Distribution (book-verified opportunities, gross gap %)")
        self.p_hist.showGrid(x=True, y=True, alpha=0.25)
        self.p_hist.setLabel("bottom", "gross gap %")
        self.p_hist.setLabel("left", "count")
        self.hist_item: Optional[pg.BarGraphItem] = None
        self.hist_note = pg.TextItem("no gaps detected yet", color="#888888", anchor=(0.5, 0.5))
        self.p_hist.addItem(self.hist_note)

        # --- 3. equity curve ---
        self.p_eq = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
            title=f"Paper Equity (cash + deployed + locked profit)  —  starts at ${STARTING_BALANCE:.0f}"
        )
        self.p_eq.showGrid(x=True, y=True, alpha=0.25)
        self.eq_curve = self.p_eq.plot(pen=pg.mkPen("#59a14f", width=2))
        self.p_eq.addItem(pg.InfiniteLine(
            pos=STARTING_BALANCE, angle=0,
            pen=pg.mkPen("#888888", width=1, style=Qt.PenStyle.DashLine)
        ))

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.p_arb)
        splitter.addWidget(self.p_hist)
        splitter.addWidget(self.p_eq)
        splitter.setSizes([320, 220, 220])
        lay.addWidget(splitter)

    def on_chart_data(self, d: dict):
        if not self.enabled:
            return
        ts = d.get("ts")
        if ts is None:
            return

        if d.get("ask_sum") is not None:
            self.ask_ts.append(ts)
            self.ask_vals.append(d["ask_sum"])
            self.ask_curve.setData(list(self.ask_ts), list(self.ask_vals))

        if d.get("bid_sum") is not None:
            self.bid_ts.append(ts)
            self.bid_vals.append(d["bid_sum"])
            self.bid_curve.setData(list(self.bid_ts), list(self.bid_vals))

        if d.get("equity") is not None:
            self.eq_ts.append(ts)
            self.eq_vals.append(d["equity"])
            self.eq_curve.setData(list(self.eq_ts), list(self.eq_vals))

        new_gaps = d.get("new_gaps_pct") or []
        if new_gaps:
            self.gaps_pct.extend(new_gaps)
            self._redraw_histogram()

    def _redraw_histogram(self):
        if not self.gaps_pct:
            return
        if self.hist_note is not None:
            self.p_hist.removeItem(self.hist_note)
            self.hist_note = None
        counts, edges = np.histogram(self.gaps_pct, bins=20)
        centers = (edges[:-1] + edges[1:]) / 2.0
        width = (edges[1] - edges[0]) * 0.9 if len(edges) > 1 else 0.1
        if self.hist_item is not None:
            self.p_hist.removeItem(self.hist_item)
        self.hist_item = pg.BarGraphItem(x=centers, height=counts, width=width, brush="#f28e2b")
        self.p_hist.addItem(self.hist_item)


# ============================================================
# GUI
# ============================================================

class MainWindow(QMainWindow):
    append_log = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Polymarket YES+NO Arbitrage Scanner — PAPER MODE")

        self.append_log.connect(self._append_log)
        self.logger, self.listener, self.file_handler, self.log_path = build_logging_system(self.append_log)
        self.worker: Optional[ScannerWorker] = None
        self.last_heartbeat_ts: Optional[float] = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        # --- top bar ---
        top = QHBoxLayout()
        self.btn_start = QPushButton("Start Scanning")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.lbl_status = QLabel("Status: Idle")
        self.lbl_heartbeat = QLabel("Last scan: -")
        self.lbl_health = QLabel("Health: -")
        mode = QLabel("PAPER MODE — no real orders")
        mode.setStyleSheet("color: #b8860b; font-weight: bold;")
        self.btn_charts = QPushButton("Show Charts ▼")
        self.btn_charts.setCheckable(True)
        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        top.addWidget(self.btn_charts)
        top.addWidget(mode)
        top.addStretch(1)
        top.addWidget(self.lbl_status)
        top.addWidget(self.lbl_heartbeat)
        top.addWidget(self.lbl_health)
        layout.addLayout(top)

        # --- stats bar ---
        self.lbl_stats1 = QLabel("Opportunities found: 0 (buy: 0 / sell: 0) | Open positions: 0 | Completed: 0")
        self.lbl_stats2 = QLabel(
            f"Cash: ${STARTING_BALANCE:.2f} | Deployed: $0.00 | Locked profit: $0.00 | "
            f"Realized PnL: $0.00 | Equity: ${STARTING_BALANCE:.2f}"
        )
        layout.addWidget(self.lbl_stats1)
        layout.addWidget(self.lbl_stats2)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)

        # --- collapsible charts panel (hidden by default; toggled from top bar) ---
        self.charts = ChartsPanel()
        self.charts.setVisible(False)

        # --- opportunities table ---
        opps_box, self.opps_table = self._make_table_box(
            "Detected Opportunities (live, book-verified)",
            ["Time", "Type", "Question", "YES px", "NO px", "Sum", "Gap %", "Net %", "Depth (sh)", "Traded"]
        )

        # --- open positions table ---
        pos_box, self.pos_table = self._make_table_box(
            "Open Paper Positions (BUY-arbs held to resolution)",
            ["ID", "Question", "Shares", "Cost+Fees", "Locked Payout", "Locked Net", "Entry", "Market Ends"]
        )

        # --- completed table ---
        done_box, self.done_table = self._make_table_box(
            "Completed Paper Trades",
            ["ID", "Type", "Question", "Shares", "Cost+Fees", "Payout", "Net PnL", "Closed"]
        )

        # --- log ---
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Realtime Log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(10000)
        log_layout.addWidget(self.log_view)

        splitter.addWidget(self.charts)      # collapsible layer (top, hidden by default)
        splitter.addWidget(opps_box)
        splitter.addWidget(pos_box)
        splitter.addWidget(done_box)
        splitter.addWidget(log_box)
        splitter.setSizes([0, 260, 200, 200, 220])
        self._main_splitter = splitter

        self.btn_charts.toggled.connect(self._toggle_charts)
        self.btn_start.clicked.connect(self.start_scanner)
        self.btn_stop.clicked.connect(self.stop_scanner)

        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self._update_health)
        self.watchdog.start(2000)

        self.logger.info("GUI initialized. Press 'Start Scanning' to begin.")

    # ---------- ui helpers ----------

    def _toggle_charts(self, checked: bool):
        """Expand/collapse the charts layer above the tables."""
        self.charts.setVisible(checked)
        self.btn_charts.setText("Hide Charts ▲" if checked else "Show Charts ▼")
        sizes = self._main_splitter.sizes()
        if checked:
            # give the chart layer ~40% of the window; shrink the rest proportionally
            total = sum(sizes) or 1000
            chart_h = int(total * 0.40)
            rest = total - chart_h
            other = sizes[1:]
            other_total = sum(other) or 1
            self._main_splitter.setSizes(
                [chart_h] + [max(60, int(rest * s / other_total)) for s in other]
            )
        else:
            self._main_splitter.setSizes([0] + sizes[1:])

    def _make_table_box(self, title: str, headers: List[str]) -> Tuple[QWidget, QTableWidget]:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(title))
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        header = table.horizontalHeader()
        # stretch the "Question" column if present
        q_col = headers.index("Question") if "Question" in headers else 0
        header.setSectionResizeMode(q_col, QHeaderView.ResizeMode.Stretch)
        for c in range(len(headers)):
            if c != q_col:
                header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
                table.setColumnWidth(c, 95)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(table)
        return box, table

    def _set(self, table: QTableWidget, row: int, col: int, text: str, right=False):
        item = QTableWidgetItem(text)
        if right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, col, item)

    def _append_log(self, line: str):
        self.log_view.appendPlainText(line)

    def _update_health(self):
        if self.worker and self.worker.isRunning():
            if self.last_heartbeat_ts is None:
                self.lbl_health.setText("Health: starting...")
                return
            age = time.time() - self.last_heartbeat_ts
            self.lbl_health.setText("Health: active" if age <= SCAN_INTERVAL_SECONDS + 15
                                    else f"Health: stale ({age:.0f}s)")
        else:
            self.lbl_health.setText("Health: stopped")

    # ---------- signal handlers ----------

    def _on_heartbeat(self, stamp: str):
        self.last_heartbeat_ts = time.time()
        self.lbl_heartbeat.setText(f"Last scan: {stamp}")

    def _on_status(self, text: str):
        self.lbl_status.setText(f"Status: {text}")

    def _on_opps(self, rows: List[dict]):
        self.opps_table.setRowCount(len(rows))
        for r, o in enumerate(rows):
            self._set(self.opps_table, r, 0, o["time"])
            self._set(self.opps_table, r, 1, o["type"])
            self._set(self.opps_table, r, 2, o["question"])
            self._set(self.opps_table, r, 3, f'{o["yes"]:.3f}', True)
            self._set(self.opps_table, r, 4, f'{o["no"]:.3f}', True)
            self._set(self.opps_table, r, 5, f'{o["sum"]:.4f}', True)
            self._set(self.opps_table, r, 6, f'{o["gap_pct"]:.2f}', True)
            self._set(self.opps_table, r, 7, f'{o["net_pct"]:.2f}', True)
            self._set(self.opps_table, r, 8, f'{o["depth"]:.0f}', True)
            self._set(self.opps_table, r, 9, o["traded"])

    def _on_positions(self, rows: List[dict]):
        self.pos_table.setRowCount(len(rows))
        for r, p in enumerate(rows):
            self._set(self.pos_table, r, 0, str(p["id"]))
            self._set(self.pos_table, r, 1, p["question"])
            self._set(self.pos_table, r, 2, f'{p["shares"]:.1f}', True)
            self._set(self.pos_table, r, 3, f'{p["cost"]:.2f}', True)
            self._set(self.pos_table, r, 4, f'{p["payout"]:.2f}', True)
            self._set(self.pos_table, r, 5, f'{p["net"]:.2f}', True)
            self._set(self.pos_table, r, 6, p["entry"])
            self._set(self.pos_table, r, 7, p["ends"])

    def _on_completed(self, rows: List[dict]):
        self.done_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            self._set(self.done_table, r, 0, str(t["id"]))
            self._set(self.done_table, r, 1, t["type"])
            self._set(self.done_table, r, 2, t["question"])
            self._set(self.done_table, r, 3, f'{t["shares"]:.1f}', True)
            self._set(self.done_table, r, 4, f'{t["cost"]:.2f}', True)
            self._set(self.done_table, r, 5, f'{t["payout"]:.2f}', True)
            self._set(self.done_table, r, 6, f'{t["net"]:.2f}', True)
            self._set(self.done_table, r, 7, t["closed"])

    def _on_stats(self, s: dict):
        self.lbl_stats1.setText(
            f'Opportunities found: {s["opps_found"]} (buy: {s["buy_opps"]} / sell: {s["sell_opps"]}) | '
            f'Open positions: {s["open_positions"]} | Completed: {s["completed"]}'
        )
        self.lbl_stats2.setText(
            f'Cash: ${s["cash"]:.2f} | Deployed: ${s["deployed"]:.2f} | '
            f'Locked profit: ${s["locked_profit"]:.2f} | '
            f'Realized PnL: ${s["realized_pnl"]:.2f} | Equity: ${s["equity"]:.2f}'
        )

    # ---------- lifecycle ----------

    def start_scanner(self):
        if self.worker and self.worker.isRunning():
            return
        self.last_heartbeat_ts = None
        self.worker = ScannerWorker(self.logger)
        self.worker.status_signal.connect(self._on_status)
        self.worker.opps_signal.connect(self._on_opps)
        self.worker.positions_signal.connect(self._on_positions)
        self.worker.completed_signal.connect(self._on_completed)
        self.worker.stats_signal.connect(self._on_stats)
        self.worker.heartbeat_signal.connect(self._on_heartbeat)
        self.worker.chart_signal.connect(self.charts.on_chart_data)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.logger.info("Start pressed.")

    def stop_scanner(self):
        if not self.worker:
            return
        if self.worker.isRunning():
            self.logger.info("Stop pressed.")
            self.worker.request_stop()
            self.worker.wait(15000)
        try:
            s = self.worker.engine.summary()
            self.logger.info(
                f"Session summary | opps={s['opps_found']} | open={s['open_positions']} | "
                f"locked=${s['locked_profit']:.2f} | realized=${s['realized_pnl']:.2f} | "
                f"CSV: {self.worker.engine.opps_csv}"
            )
        except Exception:
            pass
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def closeEvent(self, event):
        try:
            self.stop_scanner()
        except Exception:
            pass
        try:
            self.watchdog.stop()
        except Exception:
            pass
        try:
            self.listener.stop()
        except Exception:
            pass
        try:
            self.file_handler.flush()
            self.file_handler.close()
        except Exception:
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1500, 950)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
    #botgod