from market import Market
from strategy import MarketStrategy
from execution import Execution
from logger import log
import time

def run(steps=20):
    market = Market()
    strategy = MarketStrategy()
    execution = Execution()

    log("Starting Prediction Market Execution System")

    for step in range(1, steps + 1):
        probability = market.get_next_state()
        signal = strategy.generate_signal(probability)
        log(f"Step {step} | Probability: {probability} | Signal: {signal}")
        execution.act(signal, probability)
        time.sleep(0.1)

if __name__ == "__main__":
    run()
