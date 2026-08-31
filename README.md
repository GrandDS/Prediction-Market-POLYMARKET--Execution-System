# Polymarket Strategy Research System

A collection of independent Python strategies and research tools built around public Polymarket market data. The uploaded code is primarily paper-trading/research software; each strategy is kept separate so its assumptions and results can be evaluated independently.

## Strategies

| File | Strategy |
|---|---|
| `strategies/option_b_pro.py` | Mean-reversion / signal-ranking desktop application with PyQt6 monitoring |
| `strategies/market_maker.py` | Paper market-making strategy with inventory-aware quoting and a local dashboard |
| `strategies/copy_trader_v2.py` | Automated wallet discovery, scoring, and paper copy-trading pipeline |
| `strategies/copy_trader_v3.py` | Consolidated copy-trading bot with dashboard and faster monitoring |
| `strategies/arbitrage_scanner.py` | YES/NO order-book arbitrage scanner with paper execution and PyQt6 UI |

## Installation

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
pip install -r requirements.txt
```

Run a strategy directly, for example:

```bash
python strategies/arbitrage_scanner.py
```

or:

```bash
python strategies/option_b_pro.py
```

## Data and generated files

Runtime logs, portfolios, trade CSVs, screenshots, PyCharm metadata, cached bytecode, and the local virtual environment are deliberately excluded from version control. They are generated artefacts rather than source code.

## Security

The retained strategy files use public Polymarket data endpoints and the uploaded versions did not expose hard-coded private keys or API credentials during the repository preparation check. Do not commit wallet private keys, signing credentials, API secrets, or `.env` files.

## Research note

Paper fills and historical observations do not guarantee executable live returns. Market latency, liquidity, spread, fees, queue position, API changes, and settlement mechanics can materially change live performance.
