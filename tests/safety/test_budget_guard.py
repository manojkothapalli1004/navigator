import pytest
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.errors import BudgetExceeded

def test_blocks_before_call_when_over_budget():
    g = BudgetGuard(max_cost_usd=1.0)
    g.record_spend(0.6)
    g.check_before_call(estimated_usd=0.3)   # 0.9 ok
    g.record_spend(0.3)
    with pytest.raises(BudgetExceeded):
        g.check_before_call(estimated_usd=0.3)  # 1.2 > 1.0

def test_exact_cap_is_allowed():
    g = BudgetGuard(max_cost_usd=1.0)
    g.record_spend(0.7)
    g.check_before_call(estimated_usd=0.3)  # 1.0 == cap, strict > means allowed
