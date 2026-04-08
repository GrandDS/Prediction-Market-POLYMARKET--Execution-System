class MarketStrategy:
    def generate_signal(self, probability):
        if probability > 0.70:
            return "SELL"
        if probability < 0.30:
            return "BUY"
        return "HOLD"
