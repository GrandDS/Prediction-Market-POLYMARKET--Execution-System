import sys
import os
import csv
import time
import json
import queue
import logging
import requests
import statistics
from datetime import datetime, timezone
from collections import deque, defaultdict
from logging.handlers import QueueHandler, QueueListener
from dataclasses import dataclass
from typing import Dict, List, Optional, Deque, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter
)

# ============================================================
# CONFIG
# ============================================================

GAMMA_API = "https://gamma-api.polymarket.com"

REFRESH_SECONDS = 15
REQUEST_TIMEOUT = 20
LOOKBACK_MINUTES = 30
MIN_HISTORY_POINTS = 5

PAGE_LIMIT = 200
MAX_PAGES = 3

MIN_LIQUIDITY = 250.0
MIN_VOLUME24H = 250.0
MAX_OPEN_TRADES = 4
BASE_NOTIONAL_PER_TRADE = 100.0

# Stronger quality filter
ENTRY_ZSCORE = 1.80
STRONG_ZSCORE = 2.50
MIN_ABS_MOVE_FALLBACK = 0.018
MIN_BASELINE_STD = 0.0025

MAX_PRICE_EXTREME_LOW = 0.02
MAX_PRICE_EXTREME_HIGH = 0.98

# Profit-first exit tuning
TAKE_PROFIT_PCT = 0.065
STOP_LOSS_PCT = 0.025
MAX_HOLD_MINUTES = 22
MIN_HOLD_FOR_MEAN_EXIT_MIN = 6

# Wait for deeper reversion before taking the exit
REVERSION_EXIT_ZABS = 0.40

# Only protect real profits, not noise
BREAKEVEN_ARM_PCT = 0.035
BREAKEVEN_FLOOR_PCT = 0.010

COOLDOWN_AFTER_EXIT_MIN = 8
COOLDOWN_AFTER_REJECT_MIN = 1

DAILY_LOSS_LIMIT = 50.0
MAX_CONSECUTIVE_LOSSES = 30

MIN_TIME_TO_END_MINUTES = 20

# Armed signal logic
ARMED_WINDOW_CYCLES = 6
ARMED_MIN_SCORE = 2

# Forced fallback logic
FORCE_TRADE_AFTER_NO_ENTRY_CYCLES = 15
FORCE_TRADE_MIN_SCORE = 2
FORCE_TRADE_STAKE_MULTIPLIER = 0.45
FORCE_TRADE_HIGHER_QUALITY_STAKE_MULTIPLIER = 0.60

# Profit-first forced-entry filters
FORCED_MIN_SIDE_PRICE = 0.05
FORCED_MAX_SIDE_PRICE = 0.95
FORCED_PREFERRED_MIN_YES = 0.20
FORCED_PREFERRED_MAX_YES = 0.80
FORCED_MIN_ABS_Z = 1.10
FORCED_MIN_ABS_RAW_MOVE = 0.012

STARTING_BALANCE = 1000.0

FEE_RATE_PER_SIDE = 0.002
SLIPPAGE_RATE_PER_SIDE = 0.001

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
    log_path = os.path.join(LOG_DIR, f"option_b_pro_{make_timestamp()}.log")

    logger = logging.getLogger("option_b_pro")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)

    q = queue.Queue()
    qh = QueueHandler(q)
    logger.addHandler(qh)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
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
class PricePoint:
    ts: float
    yes_price: float


@dataclass
class SignalCandidate:
    market_id: str
    question: str
    side: str
    yes_price: float
    no_price: float
    zscore: float
    score: int
    baseline_mean: float
    baseline_std: float
    raw_move: float
    reason: str
    liquidity: float
    volume24h: float


@dataclass
class ArmedSignal:
    market_id: str
    question: str
    side: str
    armed_yes_price: float
    armed_no_price: float
    armed_zscore: float
    baseline_mean: float
    baseline_std: float
    raw_move: float
    liquidity: float
    volume24h: float
    score: int
    reason: str
    armed_cycle: int
    expire_cycle: int


@dataclass
class OpenTrade:
    trade_id: int
    market_id: str
    question: str
    side: str
    entry_time: float
    entry_yes_price: float
    entry_side_price: float
    baseline_mean: float
    baseline_std: float
    zscore: float
    score: int
    shares: float
    notional: float
    reason: str
    best_return_pct: float = -9999.0

    def hold_minutes(self, now_ts: float) -> float:
        return (now_ts - self.entry_time) / 60.0


@dataclass
class ClosedTrade:
    trade_id: int
    market_id: str
    question: str
    side: str
    entry_time: float
    exit_time: float
    entry_yes_price: float
    exit_yes_price: float
    entry_side_price: float
    exit_side_price: float
    shares: float
    notional: float
    gross_pnl: float
    costs: float
    pnl: float
    return_pct: float
    hold_minutes: float
    score: int
    zscore: float
    reason_open: str
    reason_close: str


# ============================================================
# STRATEGY ENGINE
# ============================================================

class OptionBProEngine:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.price_history: Dict[str, Deque[PricePoint]] = defaultdict(deque)
        self.open_trades: Dict[int, OpenTrade] = {}
        self.closed_trades: List[ClosedTrade] = []
        self.active_market_trade: Dict[str, int] = {}
        self.market_cooldown_until: Dict[str, float] = {}
        self.trade_counter = 0
        self.consecutive_losses = 0

        self.armed_signals: Dict[str, ArmedSignal] = {}
        self.reject_counts: Dict[str, int] = defaultdict(int)
        self.last_cycle_reject_counts: Dict[str, int] = {}

        ensure_dir(TRADE_DIR)
        self.session_stamp = make_timestamp()
        self.trade_csv_path = os.path.join(TRADE_DIR, f"option_b_pro_trades_{self.session_stamp}.csv")
        self.snapshot_json_path = os.path.join(TRADE_DIR, f"option_b_pro_snapshot_{self.session_stamp}.json")
        self.summary_txt_path = os.path.join(TRADE_DIR, f"option_b_pro_summary_{self.session_stamp}.txt")
        self._init_trade_csv()

    def _init_trade_csv(self):
        with open(self.trade_csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "trade_id", "market_id", "question", "side",
                "entry_time", "exit_time",
                "entry_yes_price", "exit_yes_price",
                "entry_side_price", "exit_side_price",
                "shares", "notional",
                "gross_pnl", "costs", "net_pnl", "return_pct",
                "hold_minutes", "score", "zscore",
                "reason_open", "reason_close"
            ])

    def start_cycle_diagnostics(self):
        self.reject_counts = defaultdict(int)

    def note_reject(self, reason: str):
        self.reject_counts[reason] += 1

    def end_cycle_diagnostics(self):
        self.last_cycle_reject_counts = dict(self.reject_counts)

    def format_reject_summary(self) -> str:
        if not self.last_cycle_reject_counts:
            return "none"
        ordered = sorted(self.last_cycle_reject_counts.items(), key=lambda x: (-x[1], x[0]))
        return ", ".join(f"{k}={v}" for k, v in ordered[:15])

    def append_price(self, market_id: str, yes_price: float, now_ts: float):
        hist = self.price_history[market_id]
        hist.append(PricePoint(now_ts, yes_price))
        cutoff = now_ts - LOOKBACK_MINUTES * 60
        while hist and hist[0].ts < cutoff:
            hist.popleft()

    def history_count(self, market_id: str) -> int:
        hist = self.price_history.get(market_id)
        return len(hist) if hist else 0

    def get_recent_prices(self, market_id: str) -> List[float]:
        hist = self.price_history.get(market_id)
        if not hist:
            return []
        return [p.yes_price for p in hist]

    def in_cooldown(self, market_id: str, now_ts: float) -> bool:
        return now_ts < self.market_cooldown_until.get(market_id, 0.0)

    def set_cooldown(self, market_id: str, now_ts: float, minutes: int):
        self.market_cooldown_until[market_id] = now_ts + minutes * 60

    def current_day_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def daily_pnl(self) -> float:
        today = self.current_day_key()
        total = 0.0
        for t in self.closed_trades:
            exit_day = datetime.fromtimestamp(t.exit_time, tz=timezone.utc).strftime("%Y-%m-%d")
            if exit_day == today:
                total += t.pnl
        return total

    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    def ending_balance(self) -> float:
        return STARTING_BALANCE + self.net_pnl()

    def roi_pct(self) -> float:
        if STARTING_BALANCE <= 0:
            return 0.0
        return (self.net_pnl() / STARTING_BALANCE) * 100.0

    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.closed_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.closed_trades if t.pnl < 0))
        if gross_loss == 0:
            return gross_profit if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    def trading_paused(self) -> Optional[str]:
        if self.daily_pnl() <= -abs(DAILY_LOSS_LIMIT):
            return f"daily loss limit reached ({self.daily_pnl():.2f})"
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return f"consecutive loss limit reached ({self.consecutive_losses})"
        return None

    def market_quality_ok(self, market_id: str, yes_price: float, liquidity: float, volume24h: float) -> bool:
        if liquidity < MIN_LIQUIDITY:
            return False
        if volume24h < MIN_VOLUME24H:
            return False
        if yes_price <= MAX_PRICE_EXTREME_LOW or yes_price >= MAX_PRICE_EXTREME_HIGH:
            return False
        if self.history_count(market_id) < MIN_HISTORY_POINTS:
            return False
        return True

    def _reversal_confirmed(self, prices: List[float], side: str) -> Tuple[bool, str]:
        if len(prices) < 3:
            return False, "not_enough_points_for_reversal"

        prev2 = prices[-3]
        prev1 = prices[-2]
        curr = prices[-1]

        if side == "BUY_YES":
            if curr > prev1 and prev1 <= prev2:
                return True, "bounce_confirmed"
            if curr >= prev1 and curr > prev2:
                return True, "micro_bounce_confirmed"
            return False, "no_bounce_confirmation"

        if side == "BUY_NO":
            if curr < prev1 and prev1 >= prev2:
                return True, "reject_confirmed"
            if curr <= prev1 and curr < prev2:
                return True, "micro_reject_confirmed"
            return False, "no_reject_confirmation"

        return False, "unknown_side"

    def _build_base_signal(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        no_price: float,
        liquidity: float,
        volume24h: float,
        now_ts: float,
    ) -> Optional[SignalCandidate]:
        if market_id in self.active_market_trade:
            self.note_reject("already_in_trade")
            return None

        if self.in_cooldown(market_id, now_ts):
            self.note_reject("market_cooldown")
            return None

        if not self.market_quality_ok(market_id, yes_price, liquidity, volume24h):
            if liquidity < MIN_LIQUIDITY:
                self.note_reject("low_liquidity")
            elif volume24h < MIN_VOLUME24H:
                self.note_reject("low_volume")
            elif yes_price <= MAX_PRICE_EXTREME_LOW or yes_price >= MAX_PRICE_EXTREME_HIGH:
                self.note_reject("extreme_price")
            elif self.history_count(market_id) < MIN_HISTORY_POINTS:
                self.note_reject("insufficient_history")
            else:
                self.note_reject("market_quality_fail")
            return None

        prices = self.get_recent_prices(market_id)
        if len(prices) < MIN_HISTORY_POINTS:
            self.note_reject("insufficient_history")
            return None

        current = prices[-1]
        baseline = prices[:-1]
        if len(baseline) < 2:
            self.note_reject("baseline_too_short")
            return None

        mean = statistics.mean(baseline)
        std = statistics.pstdev(baseline) if len(baseline) >= 2 else 0.0
        raw_move = current - mean

        z = 0.0
        if std > 1e-9:
            z = raw_move / std

        score = 0
        reasons = []

        strong_enough = False

        if std >= MIN_BASELINE_STD and abs(z) >= ENTRY_ZSCORE:
            score += 3
            reasons.append(f"|z|>={ENTRY_ZSCORE}")
            strong_enough = True
        elif abs(raw_move) >= MIN_ABS_MOVE_FALLBACK:
            score += 2
            reasons.append(f"|move|>={MIN_ABS_MOVE_FALLBACK}")
            strong_enough = True

        if not strong_enough:
            if std < MIN_BASELINE_STD and abs(raw_move) < MIN_ABS_MOVE_FALLBACK:
                self.note_reject("flat_and_weak_move")
            elif std < MIN_BASELINE_STD:
                self.note_reject("baseline_std_too_low")
            else:
                self.note_reject("weak_deviation")
            return None

        if std >= MIN_BASELINE_STD and abs(z) >= STRONG_ZSCORE:
            score += 1
            reasons.append(f"|z|>={STRONG_ZSCORE}")

        if volume24h >= MIN_VOLUME24H * 5:
            score += 2
            reasons.append("very_high_volume")
        elif volume24h >= MIN_VOLUME24H * 2:
            score += 1
            reasons.append("good_volume")

        if liquidity >= MIN_LIQUIDITY * 5:
            score += 2
            reasons.append("very_high_liquidity")
        elif liquidity >= MIN_LIQUIDITY * 2:
            score += 1
            reasons.append("good_liquidity")

        side = None
        if raw_move <= -MIN_ABS_MOVE_FALLBACK or z <= -ENTRY_ZSCORE:
            side = "BUY_YES"
        elif raw_move >= MIN_ABS_MOVE_FALLBACK or z >= ENTRY_ZSCORE:
            side = "BUY_NO"

        if side is None:
            self.note_reject("side_not_resolved")
            return None

        return SignalCandidate(
            market_id=market_id,
            question=question,
            side=side,
            yes_price=yes_price,
            no_price=no_price,
            zscore=z,
            score=score,
            baseline_mean=mean,
            baseline_std=std,
            raw_move=raw_move,
            reason=", ".join(reasons),
            liquidity=liquidity,
            volume24h=volume24h,
        )

    def maybe_arm_signal(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        no_price: float,
        liquidity: float,
        volume24h: float,
        now_ts: float,
        cycle_no: int,
    ) -> Optional[ArmedSignal]:
        if market_id in self.armed_signals:
            return None

        base = self._build_base_signal(
            market_id=market_id,
            question=question,
            yes_price=yes_price,
            no_price=no_price,
            liquidity=liquidity,
            volume24h=volume24h,
            now_ts=now_ts,
        )
        if base is None:
            return None

        if base.score < ARMED_MIN_SCORE:
            self.note_reject("armed_score_too_low")
            return None

        armed = ArmedSignal(
            market_id=base.market_id,
            question=base.question,
            side=base.side,
            armed_yes_price=base.yes_price,
            armed_no_price=base.no_price,
            armed_zscore=base.zscore,
            baseline_mean=base.baseline_mean,
            baseline_std=base.baseline_std,
            raw_move=base.raw_move,
            liquidity=base.liquidity,
            volume24h=base.volume24h,
            score=base.score,
            reason=base.reason,
            armed_cycle=cycle_no,
            expire_cycle=cycle_no + ARMED_WINDOW_CYCLES,
        )
        self.armed_signals[armed.market_id] = armed
        self.logger.info(
            f"ARMED SIGNAL | market_id={armed.market_id} | side={armed.side} | "
            f"score={armed.score} | z={armed.armed_zscore:.3f} | expires_cycle={armed.expire_cycle} | {armed.question}"
        )
        return armed

    def process_armed_signals(
        self,
        market_map: Dict[str, dict],
        now_ts: float,
        cycle_no: int,
        parse_prices_func,
    ) -> List[SignalCandidate]:
        triggered: List[SignalCandidate] = []

        for market_id in list(self.armed_signals.keys()):
            armed = self.armed_signals.get(market_id)
            if armed is None:
                continue

            if market_id in self.active_market_trade:
                self.armed_signals.pop(market_id, None)
                continue

            if cycle_no > armed.expire_cycle:
                self.logger.info(
                    f"ARMED EXPIRED | market_id={armed.market_id} | side={armed.side} | "
                    f"armed_cycle={armed.armed_cycle} | current_cycle={cycle_no} | {armed.question}"
                )
                self.armed_signals.pop(market_id, None)
                self.set_cooldown(market_id, now_ts, COOLDOWN_AFTER_REJECT_MIN)
                continue

            market = market_map.get(market_id)
            if not market:
                continue

            prices_tuple = parse_prices_func(market)
            if not prices_tuple:
                continue

            yes_price, no_price = prices_tuple
            prices = self.get_recent_prices(market_id)
            if len(prices) < 3:
                continue

            confirmed, confirm_label = self._reversal_confirmed(prices, armed.side)
            if not confirmed:
                self.note_reject(confirm_label)
                continue

            score = max(armed.score + 1, 4)
            reason = f"{armed.reason}, armed_then_{confirm_label}"

            candidate = SignalCandidate(
                market_id=armed.market_id,
                question=armed.question,
                side=armed.side,
                yes_price=yes_price,
                no_price=no_price,
                zscore=armed.armed_zscore,
                score=score,
                baseline_mean=armed.baseline_mean,
                baseline_std=armed.baseline_std,
                raw_move=armed.raw_move,
                reason=reason,
                liquidity=armed.liquidity,
                volume24h=armed.volume24h,
            )
            triggered.append(candidate)

            self.logger.info(
                f"ARMED TRIGGERED | market_id={armed.market_id} | side={armed.side} | "
                f"score={score} | confirm={confirm_label} | {armed.question}"
            )
            self.armed_signals.pop(market_id, None)

        return triggered

    def build_forced_candidate(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        no_price: float,
        liquidity: float,
        volume24h: float,
    ) -> Optional[SignalCandidate]:
        if market_id in self.active_market_trade:
            return None

        prices = self.get_recent_prices(market_id)
        if len(prices) < 3:
            return None

        baseline = prices[:-1]
        if len(baseline) < 2:
            return None

        mean = statistics.mean(baseline)
        std = statistics.pstdev(baseline) if len(baseline) >= 2 else 0.0
        raw_move = yes_price - mean

        z = 0.0
        if std > 1e-9:
            z = raw_move / std

        side = None
        side_price = None

        if raw_move <= -FORCED_MIN_ABS_RAW_MOVE or z <= -FORCED_MIN_ABS_Z:
            side = "BUY_YES"
            side_price = yes_price
        elif raw_move >= FORCED_MIN_ABS_RAW_MOVE or z >= FORCED_MIN_ABS_Z:
            side = "BUY_NO"
            side_price = no_price

        if side is None or side_price is None:
            return None

        if side_price < FORCED_MIN_SIDE_PRICE or side_price > FORCED_MAX_SIDE_PRICE:
            return None

        in_preferred_zone = FORCED_PREFERRED_MIN_YES <= yes_price <= FORCED_PREFERRED_MAX_YES
        if not in_preferred_zone:
            return None

        score = FORCE_TRADE_MIN_SCORE
        reasons = ["forced_entry_mode"]

        if abs(z) >= ENTRY_ZSCORE:
            score += 1
            reasons.append("z_stretched")
        if abs(raw_move) >= MIN_ABS_MOVE_FALLBACK:
            score += 1
            reasons.append("raw_move_stretched")
        if in_preferred_zone:
            score += 1
            reasons.append("preferred_midrange_price")

        if score < FORCE_TRADE_MIN_SCORE:
            return None

        return SignalCandidate(
            market_id=market_id,
            question=question,
            side=side,
            yes_price=yes_price,
            no_price=no_price,
            zscore=z,
            score=score,
            baseline_mean=mean,
            baseline_std=std,
            raw_move=raw_move,
            reason=", ".join(reasons),
            liquidity=liquidity,
            volume24h=volume24h,
        )

    def forced_candidate_rank_key(self, c: SignalCandidate) -> Tuple:
        yes_mid_dist = abs(c.yes_price - 0.50)
        return (
            c.score,
            -yes_mid_dist,
            abs(c.zscore),
            abs(c.raw_move),
            c.volume24h,
            c.liquidity,
        )

    def _notional_for_score(self, score: int) -> float:
        if score <= FORCE_TRADE_MIN_SCORE:
            return round(BASE_NOTIONAL_PER_TRADE * FORCE_TRADE_STAKE_MULTIPLIER, 2)
        if score == FORCE_TRADE_MIN_SCORE + 1:
            return round(BASE_NOTIONAL_PER_TRADE * FORCE_TRADE_HIGHER_QUALITY_STAKE_MULTIPLIER, 2)

        stake = BASE_NOTIONAL_PER_TRADE
        if score >= 8:
            stake *= 1.25
        elif score >= 6:
            stake *= 1.10
        return round(stake, 2)

    def maybe_open_trade(self, signal: SignalCandidate, now_ts: float) -> Optional[OpenTrade]:
        pause_reason = self.trading_paused()
        if pause_reason:
            self.logger.warning(f"Trading paused: {pause_reason}")
            return None

        if len(self.open_trades) >= MAX_OPEN_TRADES:
            self.note_reject("max_open_trades_reached")
            return None

        if signal.market_id in self.active_market_trade:
            self.note_reject("already_in_trade")
            return None

        entry_side_price = signal.yes_price if signal.side == "BUY_YES" else signal.no_price
        entry_side_price = max(0.001, entry_side_price)

        notional = self._notional_for_score(signal.score)
        shares = notional / entry_side_price

        self.trade_counter += 1
        trade = OpenTrade(
            trade_id=self.trade_counter,
            market_id=signal.market_id,
            question=signal.question,
            side=signal.side,
            entry_time=now_ts,
            entry_yes_price=signal.yes_price,
            entry_side_price=entry_side_price,
            baseline_mean=signal.baseline_mean,
            baseline_std=signal.baseline_std,
            zscore=signal.zscore,
            score=signal.score,
            shares=shares,
            notional=notional,
            reason=signal.reason,
        )

        self.open_trades[trade.trade_id] = trade
        self.active_market_trade[trade.market_id] = trade.trade_id

        self.logger.info(
            f"OPEN TRADE #{trade.trade_id} | {trade.side} | yes={trade.entry_yes_price:.3f} "
            f"| entry_side={trade.entry_side_price:.3f} | z={trade.zscore:.3f} | score={trade.score} "
            f"| stake={trade.notional:.2f} | shares={trade.shares:.4f} | {trade.question}"
        )
        return trade

    def _current_side_price(self, trade: OpenTrade, current_yes_price: float) -> float:
        return current_yes_price if trade.side == "BUY_YES" else (1.0 - current_yes_price)

    def _gross_pnl(self, trade: OpenTrade, current_yes_price: float) -> float:
        exit_side_price = self._current_side_price(trade, current_yes_price)
        return (exit_side_price - trade.entry_side_price) * trade.shares

    def _estimated_costs(self, trade: OpenTrade) -> float:
        round_trip_cost_rate = 2 * (FEE_RATE_PER_SIDE + SLIPPAGE_RATE_PER_SIDE)
        return trade.notional * round_trip_cost_rate

    def _current_reversion_z(self, trade: OpenTrade, current_yes_price: float) -> float:
        std = max(trade.baseline_std, 1e-9)
        return (current_yes_price - trade.baseline_mean) / std

    def maybe_close_trade(self, trade: OpenTrade, current_yes_price: float, now_ts: float) -> Optional[ClosedTrade]:
        hold_minutes = trade.hold_minutes(now_ts)
        gross_pnl = self._gross_pnl(trade, current_yes_price)
        costs = self._estimated_costs(trade)
        pnl = gross_pnl - costs
        return_pct = (pnl / trade.notional) * 100.0 if trade.notional else 0.0
        reason_close = None

        trade.best_return_pct = max(trade.best_return_pct, return_pct)
        current_reversion_z = abs(self._current_reversion_z(trade, current_yes_price))

        if return_pct >= TAKE_PROFIT_PCT * 100.0:
            reason_close = f"take profit hit ({return_pct:.2f}%)"
        elif return_pct <= -STOP_LOSS_PCT * 100.0:
            reason_close = f"stop loss hit ({return_pct:.2f}%)"
        elif trade.best_return_pct >= BREAKEVEN_ARM_PCT * 100.0 and return_pct <= BREAKEVEN_FLOOR_PCT * 100.0:
            reason_close = f"profit protected ({return_pct:.2f}%)"
        elif hold_minutes >= MIN_HOLD_FOR_MEAN_EXIT_MIN and current_reversion_z <= REVERSION_EXIT_ZABS and return_pct > 0.80:
            reason_close = f"mean reversion captured (|z|={current_reversion_z:.2f})"
        elif hold_minutes >= MAX_HOLD_MINUTES:
            reason_close = f"time exit after {hold_minutes:.1f} minutes"

        if reason_close is None:
            return None

        exit_side_price = self._current_side_price(trade, current_yes_price)

        closed = ClosedTrade(
            trade_id=trade.trade_id,
            market_id=trade.market_id,
            question=trade.question,
            side=trade.side,
            entry_time=trade.entry_time,
            exit_time=now_ts,
            entry_yes_price=trade.entry_yes_price,
            exit_yes_price=current_yes_price,
            entry_side_price=trade.entry_side_price,
            exit_side_price=exit_side_price,
            shares=trade.shares,
            notional=trade.notional,
            gross_pnl=gross_pnl,
            costs=costs,
            pnl=pnl,
            return_pct=return_pct,
            hold_minutes=hold_minutes,
            score=trade.score,
            zscore=trade.zscore,
            reason_open=trade.reason,
            reason_close=reason_close,
        )

        self.closed_trades.append(closed)
        self._write_closed_trade(closed)

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        self.set_cooldown(trade.market_id, now_ts, COOLDOWN_AFTER_EXIT_MIN)

        self.logger.info(
            f"CLOSE TRADE #{closed.trade_id} | {closed.side} | entry_yes={closed.entry_yes_price:.3f} "
            f"| exit_yes={closed.exit_yes_price:.3f} | gross={closed.gross_pnl:.2f} | costs={closed.costs:.2f} "
            f"| net={closed.pnl:.2f} | ret={closed.return_pct:.2f}% | hold={closed.hold_minutes:.1f}m "
            f"| score={closed.score} | z={closed.zscore:.3f} | {reason_close}"
        )

        del self.open_trades[trade.trade_id]
        self.active_market_trade.pop(trade.market_id, None)
        return closed

    def _write_closed_trade(self, t: ClosedTrade):
        with open(self.trade_csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                t.trade_id, t.market_id, t.question, t.side,
                datetime.fromtimestamp(t.entry_time, tz=timezone.utc).isoformat(sep=" ", timespec="seconds"),
                datetime.fromtimestamp(t.exit_time, tz=timezone.utc).isoformat(sep=" ", timespec="seconds"),
                f"{t.entry_yes_price:.6f}",
                f"{t.exit_yes_price:.6f}",
                f"{t.entry_side_price:.6f}",
                f"{t.exit_side_price:.6f}",
                f"{t.shares:.6f}",
                f"{t.notional:.2f}",
                f"{t.gross_pnl:.6f}",
                f"{t.costs:.6f}",
                f"{t.pnl:.6f}",
                f"{t.return_pct:.6f}",
                f"{t.hold_minutes:.2f}",
                f"{t.score}",
                f"{t.zscore:.6f}",
                t.reason_open,
                t.reason_close,
            ])

    def save_snapshot(self):
        snapshot = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "summary": self.summary(),
            "reject_summary_last_cycle": self.last_cycle_reject_counts,
            "armed_signals": [
                {
                    "market_id": a.market_id,
                    "question": a.question,
                    "side": a.side,
                    "armed_yes_price": a.armed_yes_price,
                    "armed_no_price": a.armed_no_price,
                    "armed_zscore": a.armed_zscore,
                    "score": a.score,
                    "armed_cycle": a.armed_cycle,
                    "expire_cycle": a.expire_cycle,
                    "reason": a.reason,
                }
                for a in self.armed_signals.values()
            ],
            "open_trades": [
                {
                    "trade_id": t.trade_id,
                    "market_id": t.market_id,
                    "question": t.question,
                    "side": t.side,
                    "entry_time": datetime.fromtimestamp(t.entry_time, tz=timezone.utc).isoformat(),
                    "entry_yes_price": t.entry_yes_price,
                    "entry_side_price": t.entry_side_price,
                    "baseline_mean": t.baseline_mean,
                    "baseline_std": t.baseline_std,
                    "zscore": t.zscore,
                    "score": t.score,
                    "shares": t.shares,
                    "notional": t.notional,
                    "reason": t.reason,
                    "best_return_pct": t.best_return_pct,
                }
                for t in self.open_trades.values()
            ],
            "closed_trades_count": len(self.closed_trades),
            "trade_csv_path": self.trade_csv_path,
        }

        with open(self.snapshot_json_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)

        with open(self.summary_txt_path, "w", encoding="utf-8") as f:
            for k, v in self.summary().items():
                f.write(f"{k}: {v}\n")
            f.write("\n")
            f.write("reject_summary_last_cycle:\n")
            for k, v in sorted(self.last_cycle_reject_counts.items(), key=lambda x: (-x[1], x[0])):
                f.write(f"{k}: {v}\n")
            f.write("\n")
            f.write(f"armed_signals_count: {len(self.armed_signals)}\n")

    def summary(self) -> Dict[str, float]:
        total = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        losses = sum(1 for t in self.closed_trades if t.pnl < 0)
        net_pnl = self.net_pnl()
        avg_ret = sum(t.return_pct for t in self.closed_trades) / total if total else 0.0

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.closed_trades:
            equity += t.pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        return {
            "closed_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "net_pnl": net_pnl,
            "avg_return_pct": avg_ret,
            "max_drawdown": max_dd,
            "open_trades": len(self.open_trades),
            "armed_signals": len(self.armed_signals),
            "daily_pnl": self.daily_pnl(),
            "consecutive_losses": self.consecutive_losses,
            "starting_balance": STARTING_BALANCE,
            "ending_balance": self.ending_balance(),
            "roi_pct": self.roi_pct(),
            "profit_factor": self.profit_factor(),
        }


# ============================================================
# MARKET FEED
# ============================================================

class MarketFeed:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "option-b-pro/1.0"})

        retry = Retry(
            total=HTTP_RETRY_TOTAL,
            connect=HTTP_RETRY_TOTAL,
            read=HTTP_RETRY_TOTAL,
            status=HTTP_RETRY_TOTAL,
            backoff_factor=HTTP_BACKOFF_FACTOR,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def fetch_page(self, offset: int, limit: int = PAGE_LIMIT) -> List[dict]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        r = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def fetch_many_markets(self) -> List[dict]:
        all_rows: List[dict] = []
        for page in range(MAX_PAGES):
            offset = page * PAGE_LIMIT
            batch = self.fetch_page(offset=offset, limit=PAGE_LIMIT)
            if not batch:
                break
            all_rows.extend(batch)
        return all_rows


# ============================================================
# WORKER THREAD
# ============================================================

class BotWorker(QThread):
    status_signal = pyqtSignal(str)
    markets_signal = pyqtSignal(list)
    open_trades_signal = pyqtSignal(list)
    closed_trades_signal = pyqtSignal(list)
    summary_signal = pyqtSignal(dict)
    heartbeat_signal = pyqtSignal(str)

    def __init__(self, logger: logging.Logger):
        super().__init__()
        self.logger = logger
        self.engine = OptionBProEngine(logger)
        self.feed = MarketFeed(logger)
        self._stop_requested = False
        self._debug_samples_logged = 0
        self._cycle_count = 0
        self.no_entry_cycles = 0

    def request_stop(self):
        self._stop_requested = True

    def _safe_float(self, value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _parse_json_list(self, value) -> List:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    def _parse_yes_no_prices(self, market: dict) -> Optional[tuple]:
        outcomes = self._parse_json_list(market.get("outcomes"))
        prices = self._parse_json_list(market.get("outcomePrices"))

        if not outcomes or not prices or len(outcomes) != len(prices):
            return None

        yes_price = None
        no_price = None

        for outcome, price in zip(outcomes, prices):
            outcome_name = str(outcome).strip().upper()
            p = self._safe_float(price, default=-1.0)
            if outcome_name == "YES":
                yes_price = p
            elif outcome_name == "NO":
                no_price = p

        if yes_price is None or no_price is None:
            return None
        if yes_price < 0 or no_price < 0:
            return None

        return yes_price, no_price

    def _parse_end_time(self, market: dict) -> Optional[datetime]:
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

            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _minutes_to_end(self, market: dict) -> Optional[float]:
        dt = self._parse_end_time(market)
        if dt is None:
            return None
        return (dt - datetime.now(timezone.utc)).total_seconds() / 60.0

    def _market_id(self, market: dict) -> str:
        return str(market.get("conditionId") or market.get("id") or "")

    def _question(self, market: dict) -> str:
        return str(market.get("question", "")).strip()

    def _liquidity(self, market: dict) -> float:
        return self._safe_float(market.get("liquidityNum"), self._safe_float(market.get("liquidity")))

    def _volume24h(self, market: dict) -> float:
        return self._safe_float(
            market.get("volume24hr"),
            self._safe_float(market.get("volume24h"), self._safe_float(market.get("volume")))
        )

    def _is_usable_market(self, market: dict) -> bool:
        question = self._question(market)
        if len(question) < 10:
            return False

        if market.get("active") is False:
            return False
        if market.get("closed") is True:
            return False
        if market.get("archived") is True:
            return False
        if market.get("enableOrderBook") is False:
            return False

        end_time = self._parse_end_time(market)
        if end_time is not None:
            now_utc = datetime.now(timezone.utc)
            if end_time <= now_utc:
                return False

        mins_to_end = self._minutes_to_end(market)
        if mins_to_end is not None and mins_to_end < MIN_TIME_TO_END_MINUTES:
            return False

        prices = self._parse_yes_no_prices(market)
        if not prices:
            return False

        yes_price, no_price = prices

        if yes_price <= 0.005 or yes_price >= 0.995:
            return False
        if no_price <= 0.005 or no_price >= 0.995:
            return False

        if self._liquidity(market) < MIN_LIQUIDITY:
            return False
        if self._volume24h(market) < MIN_VOLUME24H:
            return False

        return True

    def _normalize_markets(self, markets: List[dict]) -> List[dict]:
        rows = []
        for m in markets:
            try:
                if not self._is_usable_market(m):
                    continue

                prices = self._parse_yes_no_prices(m)
                if not prices:
                    continue

                yes_price, no_price = prices
                market_id = self._market_id(m)
                question = self._question(m)
                liquidity = self._liquidity(m)
                vol24h = self._volume24h(m)

                recent_prices = self.engine.get_recent_prices(market_id)
                baseline_mean = 0.0
                zscore = 0.0
                raw_move = 0.0

                if len(recent_prices) >= MIN_HISTORY_POINTS:
                    baseline = recent_prices[:-1]
                    if len(baseline) >= 2:
                        baseline_mean = statistics.mean(baseline)
                        baseline_std = statistics.pstdev(baseline)
                        raw_move = yes_price - baseline_mean
                        if baseline_std > 1e-9:
                            zscore = raw_move / baseline_std

                rows.append({
                    "market_id": market_id,
                    "question": question,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "liquidity": liquidity,
                    "volume24h": vol24h,
                    "baseline_mean": baseline_mean,
                    "zscore": zscore,
                    "raw_move": raw_move,
                })
            except Exception as exc:
                self.logger.warning(f"Normalize market skipped due to error: {exc}")

        rows.sort(key=lambda x: (abs(x["zscore"]), abs(x["raw_move"]), x["volume24h"]), reverse=True)
        return rows

    def _render_open_trades(self) -> List[dict]:
        rows = []
        for t in self.engine.open_trades.values():
            rows.append({
                "trade_id": t.trade_id,
                "question": t.question,
                "side": t.side,
                "entry_yes_price": t.entry_yes_price,
                "entry_side_price": t.entry_side_price,
                "zscore": t.zscore,
                "score": t.score,
                "shares": t.shares,
                "notional": t.notional,
            })
        return rows

    def _render_closed_trades(self) -> List[dict]:
        rows = []
        for t in reversed(self.engine.closed_trades[-50:]):
            rows.append({
                "trade_id": t.trade_id,
                "question": t.question,
                "side": t.side,
                "entry_yes_price": t.entry_yes_price,
                "exit_yes_price": t.exit_yes_price,
                "pnl": t.pnl,
                "return_pct": t.return_pct,
                "hold_minutes": t.hold_minutes,
                "score": t.score,
                "zscore": t.zscore,
            })
        return rows

    def _emit_heartbeat(self):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.heartbeat_signal.emit(stamp)

    def run(self):
        self.status_signal.emit("Running")
        self.logger.info("Option B Pro worker started.")

        while not self._stop_requested:
            cycle_start = time.time()
            self._cycle_count += 1

            try:
                self.engine.start_cycle_diagnostics()
                self._emit_heartbeat()
                self.logger.info(f"Heartbeat | cycle={self._cycle_count} | worker_alive=True")

                raw_markets = self.feed.fetch_many_markets()
                self.logger.info(f"Fetched {len(raw_markets)} markets from Gamma.")

                now_ts = time.time()
                usable_markets = 0
                history_ready = 0
                filtered_markets: List[dict] = []
                market_map: Dict[str, dict] = {}

                for m in raw_markets:
                    try:
                        if not self._is_usable_market(m):
                            if self._debug_samples_logged < 2:
                                self.logger.info(f"DEBUG rejected usable-filter sample: {m}")
                                self._debug_samples_logged += 1
                            continue

                        market_id = self._market_id(m)
                        question = self._question(m)
                        prices = self._parse_yes_no_prices(m)
                        if not market_id or not question or not prices:
                            continue

                        yes_price, no_price = prices
                        liquidity = self._liquidity(m)
                        vol24h = self._volume24h(m)

                        usable_markets += 1
                        filtered_markets.append(m)
                        market_map[market_id] = m

                        self.engine.append_price(market_id, yes_price, now_ts)

                        if self.engine.history_count(market_id) >= MIN_HISTORY_POINTS:
                            history_ready += 1

                        self.engine.maybe_arm_signal(
                            market_id=market_id,
                            question=question,
                            yes_price=yes_price,
                            no_price=no_price,
                            liquidity=liquidity,
                            volume24h=vol24h,
                            now_ts=now_ts,
                            cycle_no=self._cycle_count,
                        )
                    except Exception as exc:
                        self.logger.warning(f"Market processing skipped due to error: {exc}")

                for trade in list(self.engine.open_trades.values()):
                    try:
                        market_now = market_map.get(trade.market_id)
                        if market_now is None:
                            continue

                        prices = self._parse_yes_no_prices(market_now)
                        if not prices:
                            continue

                        current_yes_price, _ = prices
                        self.engine.maybe_close_trade(trade, current_yes_price, now_ts)
                    except Exception as exc:
                        self.logger.warning(f"Trade close check skipped due to error: {exc}")

                candidates = self.engine.process_armed_signals(
                    market_map=market_map,
                    now_ts=now_ts,
                    cycle_no=self._cycle_count,
                    parse_prices_func=self._parse_yes_no_prices,
                )

                candidates.sort(key=lambda c: (c.score, abs(c.zscore), abs(c.raw_move), c.volume24h), reverse=True)

                opened_count = 0
                for candidate in candidates:
                    try:
                        if len(self.engine.open_trades) >= MAX_OPEN_TRADES:
                            break
                        trade = self.engine.maybe_open_trade(candidate, now_ts)
                        if trade:
                            opened_count += 1
                    except Exception as exc:
                        self.logger.warning(f"Trade open skipped due to error: {exc}")

                if opened_count == 0 and len(self.engine.open_trades) == 0:
                    self.no_entry_cycles += 1
                else:
                    self.no_entry_cycles = 0

                if (
                    opened_count == 0
                    and len(self.engine.open_trades) == 0
                    and self.no_entry_cycles >= FORCE_TRADE_AFTER_NO_ENTRY_CYCLES
                ):
                    ranked_rows = self._normalize_markets(filtered_markets)
                    forced_candidates: List[SignalCandidate] = []

                    for row in ranked_rows:
                        cand = self.engine.build_forced_candidate(
                            market_id=row["market_id"],
                            question=row["question"],
                            yes_price=row["yes_price"],
                            no_price=row["no_price"],
                            liquidity=row["liquidity"],
                            volume24h=row["volume24h"],
                        )
                        if cand:
                            forced_candidates.append(cand)

                    forced_candidates.sort(
                        key=self.engine.forced_candidate_rank_key,
                        reverse=True
                    )

                    if forced_candidates:
                        forced_candidate = forced_candidates[0]
                        trade = self.engine.maybe_open_trade(forced_candidate, now_ts)
                        if trade:
                            opened_count += 1
                            self.logger.info(
                                f"FORCED PAPER TRADE OPENED after {self.no_entry_cycles} quiet cycles "
                                f"| trade_id={trade.trade_id} | market_id={trade.market_id} | side={trade.side} "
                                f"| yes={forced_candidate.yes_price:.3f} | score={forced_candidate.score}"
                            )
                            self.no_entry_cycles = 0
                    else:
                        self.logger.info(
                            f"Forced-entry mode checked after {self.no_entry_cycles} quiet cycles, but no profit-quality market found."
                        )

                self.engine.end_cycle_diagnostics()

                pause_reason = self.engine.trading_paused()
                if pause_reason:
                    self.logger.warning(f"Trading paused: {pause_reason}")

                self.logger.info(
                    f"Cycle complete | cycle={self._cycle_count} | usable_markets={usable_markets} | "
                    f"history_ready={history_ready} | armed={len(self.engine.armed_signals)} | "
                    f"candidates={len(candidates)} | opened_this_cycle={opened_count} | "
                    f"quiet_cycles={self.no_entry_cycles} | open_trades={len(self.engine.open_trades)} | "
                    f"closed_trades={len(self.engine.closed_trades)} | ending_balance={self.engine.ending_balance():.2f}"
                )

                self.logger.info(
                    f"Reject diagnostics | cycle={self._cycle_count} | {self.engine.format_reject_summary()}"
                )

                try:
                    market_rows = self._normalize_markets(filtered_markets)
                    self.markets_signal.emit(market_rows[:100])
                    self.open_trades_signal.emit(self._render_open_trades())
                    self.closed_trades_signal.emit(self._render_closed_trades())
                    self.summary_signal.emit(self.engine.summary())
                    self._emit_heartbeat()
                except Exception as exc:
                    self.logger.exception(f"UI signal emit error: {exc}")

            except Exception as e:
                self.logger.exception(f"Worker cycle error: {e}")

            elapsed = time.time() - cycle_start
            remaining = max(1.0, REFRESH_SECONDS - elapsed)
            end_sleep = time.time() + remaining
            while time.time() < end_sleep:
                if self._stop_requested:
                    break
                time.sleep(0.25)

        self.status_signal.emit("Stopped")
        self.logger.info("Option B Pro worker stopped.")


# ============================================================
# GUI
# ============================================================

class MainWindow(QMainWindow):
    append_log = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Polymarket Option B Pro")

        self.append_log.connect(self._append_log)
        self.logger, self.listener, self.file_handler, self.log_path = build_logging_system(self.append_log)
        self.worker: Optional[BotWorker] = None
        self.last_heartbeat_ts: Optional[float] = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        self.lbl_status = QLabel("Status: Idle")
        self.lbl_last_cycle = QLabel("Last cycle: -")
        self.lbl_worker_health = QLabel("Worker health: -")

        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        top.addStretch(1)
        top.addWidget(self.lbl_status)
        top.addWidget(self.lbl_last_cycle)
        top.addWidget(self.lbl_worker_health)
        layout.addLayout(top)

        self.lbl_summary = QLabel(
            "Closed: 0 | Wins: 0 | Win rate: 0.00% | Net PnL: 0.00 | Avg Ret: 0.00% | DD: 0.00 | Open: 0 | Armed: 0"
        )
        self.lbl_capital = QLabel(
            f"Start Bal: {STARTING_BALANCE:.2f} | End Bal: {STARTING_BALANCE:.2f} | ROI: 0.00% | Profit Factor: 0.00 | Daily: 0.00 | Loss Streak: 0"
        )

        layout.addWidget(self.lbl_summary)
        layout.addWidget(self.lbl_capital)

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(vertical_splitter)

        markets_box = QWidget()
        markets_layout = QVBoxLayout(markets_box)
        markets_layout.setContentsMargins(0, 0, 0, 0)
        markets_layout.addWidget(QLabel("Live Markets (filtered + ranked)"))

        self.markets_table = QTableWidget(0, 9)
        self.markets_table.setHorizontalHeaderLabels([
            "Question", "YES", "NO", "24h Volume", "Liquidity", "Baseline Mean", "Z-Score", "Raw Move", "Market ID"
        ])
        self._prep_table(self.markets_table, stretch_first=True)
        markets_layout.addWidget(self.markets_table)

        trades_splitter = QSplitter(Qt.Orientation.Vertical)

        open_box = QWidget()
        open_layout = QVBoxLayout(open_box)
        open_layout.setContentsMargins(0, 0, 0, 0)
        open_layout.addWidget(QLabel("Open Paper Trades"))
        self.open_trades_table = QTableWidget(0, 9)
        self.open_trades_table.setHorizontalHeaderLabels([
            "Trade ID", "Question", "Side", "Entry YES", "Entry Side Px", "Z-Score", "Score", "Shares", "Stake"
        ])
        self._prep_table(self.open_trades_table, stretch_first=True)
        open_layout.addWidget(self.open_trades_table)

        closed_box = QWidget()
        closed_layout = QVBoxLayout(closed_box)
        closed_layout.setContentsMargins(0, 0, 0, 0)
        closed_layout.addWidget(QLabel("Closed Paper Trades"))
        self.closed_trades_table = QTableWidget(0, 10)
        self.closed_trades_table.setHorizontalHeaderLabels([
            "Trade ID", "Question", "Side", "Entry YES", "Exit YES", "PnL", "Return %", "Hold Min", "Score", "Z-Score"
        ])
        self._prep_table(self.closed_trades_table, stretch_first=True)
        closed_layout.addWidget(self.closed_trades_table)

        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Realtime Log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(10000)
        log_layout.addWidget(self.log_view)

        trades_splitter.addWidget(open_box)
        trades_splitter.addWidget(closed_box)
        trades_splitter.addWidget(log_box)
        trades_splitter.setSizes([180, 220, 260])

        vertical_splitter.addWidget(markets_box)
        vertical_splitter.addWidget(trades_splitter)
        vertical_splitter.setSizes([300, 520])

        self.btn_start.clicked.connect(self.start_bot)
        self.btn_stop.clicked.connect(self.stop_bot)

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self._update_worker_health)
        self.watchdog_timer.start(2000)

        self.logger.info("GUI initialized.")

    def _prep_table(self, table: QTableWidget, stretch_first=False):
        header = table.horizontalHeader()

        if stretch_first:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for c in range(1, table.columnCount()):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(c, 115)

        header.setSectionsMovable(True)
        header.setCascadingSectionResizes(True)
        header.setMinimumSectionSize(60)

        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def _append_log(self, line: str):
        self.log_view.appendPlainText(line)

    def _set_item(self, table: QTableWidget, row: int, col: int, text: str, right=False):
        item = QTableWidgetItem(text)
        if right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, col, item)

    def _set_status(self, text: str):
        self.lbl_status.setText(f"Status: {text}")

    def _on_heartbeat(self, stamp: str):
        self.last_heartbeat_ts = time.time()
        self.lbl_last_cycle.setText(f"Last cycle: {stamp}")

    def _update_worker_health(self):
        if self.worker and self.worker.isRunning():
            if self.last_heartbeat_ts is None:
                self.lbl_worker_health.setText("Worker health: waiting")
                return

            age = time.time() - self.last_heartbeat_ts
            if age <= (REFRESH_SECONDS + 10):
                self.lbl_worker_health.setText("Worker health: active")
            else:
                self.lbl_worker_health.setText(f"Worker health: stale ({age:.0f}s)")
        else:
            self.lbl_worker_health.setText("Worker health: stopped")

    def _render_markets(self, rows: List[dict]):
        self.markets_table.setRowCount(len(rows))
        for r, m in enumerate(rows):
            self._set_item(self.markets_table, r, 0, str(m["question"]))
            self._set_item(self.markets_table, r, 1, f'{m["yes_price"]:.3f}', True)
            self._set_item(self.markets_table, r, 2, f'{m["no_price"]:.3f}', True)
            self._set_item(self.markets_table, r, 3, f'{m["volume24h"]:.2f}', True)
            self._set_item(self.markets_table, r, 4, f'{m["liquidity"]:.2f}', True)
            baseline_text = f'{m["baseline_mean"]:.3f}' if m["baseline_mean"] > 0 else "-"
            self._set_item(self.markets_table, r, 5, baseline_text, True)
            zscore_text = f'{m["zscore"]:+.2f}' if abs(m["zscore"]) > 0 else "-"
            self._set_item(self.markets_table, r, 6, zscore_text, True)
            raw_move_text = f'{m["raw_move"]:+.3f}' if abs(m["raw_move"]) > 0 else "-"
            self._set_item(self.markets_table, r, 7, raw_move_text, True)
            self._set_item(self.markets_table, r, 8, str(m["market_id"]))

    def _render_open_trades(self, rows: List[dict]):
        self.open_trades_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            self._set_item(self.open_trades_table, r, 0, str(t["trade_id"]))
            self._set_item(self.open_trades_table, r, 1, str(t["question"]))
            self._set_item(self.open_trades_table, r, 2, str(t["side"]))
            self._set_item(self.open_trades_table, r, 3, f'{t["entry_yes_price"]:.3f}', True)
            self._set_item(self.open_trades_table, r, 4, f'{t["entry_side_price"]:.3f}', True)
            self._set_item(self.open_trades_table, r, 5, f'{t["zscore"]:+.2f}', True)
            self._set_item(self.open_trades_table, r, 6, f'{t["score"]}', True)
            self._set_item(self.open_trades_table, r, 7, f'{t["shares"]:.3f}', True)
            self._set_item(self.open_trades_table, r, 8, f'{t["notional"]:.2f}', True)

    def _render_closed_trades(self, rows: List[dict]):
        self.closed_trades_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            self._set_item(self.closed_trades_table, r, 0, str(t["trade_id"]))
            self._set_item(self.closed_trades_table, r, 1, str(t["question"]))
            self._set_item(self.closed_trades_table, r, 2, str(t["side"]))
            self._set_item(self.closed_trades_table, r, 3, f'{t["entry_yes_price"]:.3f}', True)
            self._set_item(self.closed_trades_table, r, 4, f'{t["exit_yes_price"]:.3f}', True)
            self._set_item(self.closed_trades_table, r, 5, f'{t["pnl"]:.2f}', True)
            self._set_item(self.closed_trades_table, r, 6, f'{t["return_pct"]:.2f}', True)
            self._set_item(self.closed_trades_table, r, 7, f'{t["hold_minutes"]:.1f}', True)
            self._set_item(self.closed_trades_table, r, 8, f'{t["score"]}', True)
            self._set_item(self.closed_trades_table, r, 9, f'{t["zscore"]:+.2f}', True)

    def _render_summary(self, s: dict):
        self.lbl_summary.setText(
            f'Closed: {s["closed_trades"]} | Wins: {s["wins"]} | '
            f'Win rate: {s["win_rate"]:.2f}% | Net PnL: {s["net_pnl"]:.2f} | '
            f'Avg Ret: {s["avg_return_pct"]:.2f}% | DD: {s["max_drawdown"]:.2f} | '
            f'Open: {s["open_trades"]} | Armed: {s["armed_signals"]}'
        )
        self.lbl_capital.setText(
            f'Start Bal: {s["starting_balance"]:.2f} | End Bal: {s["ending_balance"]:.2f} | '
            f'ROI: {s["roi_pct"]:.2f}% | Profit Factor: {s["profit_factor"]:.2f} | '
            f'Daily: {s["daily_pnl"]:.2f} | Loss Streak: {s["consecutive_losses"]}'
        )

    def start_bot(self):
        if self.worker and self.worker.isRunning():
            return

        self.last_heartbeat_ts = None

        self.worker = BotWorker(self.logger)
        self.worker.status_signal.connect(self._set_status)
        self.worker.markets_signal.connect(self._render_markets)
        self.worker.open_trades_signal.connect(self._render_open_trades)
        self.worker.closed_trades_signal.connect(self._render_closed_trades)
        self.worker.summary_signal.connect(self._render_summary)
        self.worker.heartbeat_signal.connect(self._on_heartbeat)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.logger.info("Start pressed.")

    def stop_bot(self):
        if not self.worker:
            return

        if self.worker.isRunning():
            self.logger.info("Stop pressed.")
            self.worker.request_stop()
            self.worker.wait(15000)

        try:
            self.worker.engine.save_snapshot()
            self.logger.info(
                f"Session saved | csv={self.worker.engine.trade_csv_path} | "
                f"snapshot={self.worker.engine.snapshot_json_path} | "
                f"summary={self.worker.engine.summary_txt_path}"
            )
        except Exception as e:
            self.logger.error(f"Save failed: {e}")

        try:
            for handler in self.logger.handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            self.file_handler.flush()
        except Exception:
            pass

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def closeEvent(self, event):
        try:
            self.logger.info("App closing requested.")
            self.stop_bot()
        except Exception:
            pass

        try:
            self.watchdog_timer.stop()
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
    w.resize(1600, 980)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()