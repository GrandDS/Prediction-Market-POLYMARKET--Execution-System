class Execution:
    def __init__(self):
        self.position = None

    def act(self, signal, probability):
        if signal == "BUY" and self.position is None:
            self.position = probability
            print(f"[BUY] Entered at {probability}")
        elif signal == "SELL" and self.position is not None:
            pnl = probability - self.position
            print(f"[SELL] Exited at {probability} | PnL: {pnl:.2f}")
            self.position = None
