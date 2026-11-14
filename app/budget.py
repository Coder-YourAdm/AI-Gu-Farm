"""Atomic request reservations and a monotonic wall-clock deadline."""
import time


class BudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, wall_limit_s=None, max_calls=None, *, used=0, elapsed_s=0):
        self.started = time.monotonic() - elapsed_s
        self.wall_limit_s = wall_limit_s
        self.max_calls = max_calls
        self.used = used

    def remaining(self):
        if self.wall_limit_s is None:
            return None
        remaining = self.wall_limit_s - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded('wall_limit_s exhausted')
        return remaining

    def reserve(self):
        # No await between checking and incrementing: atomic on the event loop.
        self.remaining()
        if self.max_calls is not None and self.used >= self.max_calls:
            raise BudgetExceeded('max_calls exhausted')
        self.used += 1

    def to_dict(self):
        return {'http_attempts': self.used, 'elapsed_s': time.monotonic() - self.started}
