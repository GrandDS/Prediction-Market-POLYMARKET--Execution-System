import random

class Market:
    def __init__(self):
        self.probability = 0.50

    def get_next_state(self):
        change = random.uniform(-0.05, 0.05)
        self.probability = min(max(self.probability + change, 0), 1)
        return round(self.probability, 2)
