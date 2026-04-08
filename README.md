# Prediction-Market-Execution-System
A rule-based prediction market trading system(POLYMARKET) designed to identify short-term mispricing in Polymarket markets and execute simulated or live trades based on mean-reversion principles.  The system continuously monitors market probabilities, detects overreactions, and places trades when prices deviate from expected equilibrium levels.
Key Features
Real-time Market Monitoring
Fetches and processes live market data from Polymarket (Gamma API).
Mean Reversion Strategy (Option B)
Identifies overbought/oversold conditions where probabilities deviate significantly from historical norms.
Automated Trade Execution (Simulated / Live-ready)
Executes trades based on predefined thresholds and strategy rules.
Trade Logging & Performance Tracking
Records all entries and exits
Tracks profit/loss per trade
Calculates overall strategy performance
Market Filtering Engine
Filters inactive or low-liquidity markets
Ensures only valid and tradable markets are used
State Persistence
Saves trading session data on shutdown for later analysis.
Graphical User Interface (PyQt6)
Live logs and system status
Start/Stop controls
Strategy visibility for debugging and monitoring
🧠 Strategy Logic

The system operates on a simple but effective concept:

Markets often overreact to new information, causing temporary mispricing.

Core logic:

If probability drops sharply → potential undervaluation → BUY YES
If probability spikes excessively → potential overvaluation → BUY NO
Exit when price normalizes (mean reversion achieved)
🏗️ Architecture
app.py – Entry point and orchestration
trader.py – Core strategy and trade logic
data.py – Market data fetching and preprocessing
gui.py – PyQt6 interface
logger.py – Logging and trade recording
⚙️ Tech Stack
Python 3.12
PyQt6 (GUI)
Requests / HTTPX (API calls)
JSON / CSV (data storage)
📈 Use Case
Backtesting prediction market strategies
Studying market inefficiencies
Building foundations for autonomous trading systems
Educational exploration of quantitative trading concepts
⚠️ Disclaimer

This project is for educational and research purposes only.
It does not constitute financial advice, and real-money trading involves risk.
