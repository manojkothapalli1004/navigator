from navigator.safety.errors import BudgetExceeded

class BudgetGuard:
    def __init__(self, max_cost_usd: float):
        self.max_cost_usd = max_cost_usd
        self.spent_usd = 0.0
    def reset(self) -> None:
        """Zero accumulated spend — called per daemon cycle so the cap is
        per-cycle, not process-lifetime."""
        self.spent_usd = 0.0
    def record_spend(self, usd: float) -> None:
        self.spent_usd += usd
    def check_before_call(self, estimated_usd: float) -> None:
        if self.spent_usd + estimated_usd > self.max_cost_usd:
            raise BudgetExceeded(f"would exceed {self.max_cost_usd} (spent={self.spent_usd})")
