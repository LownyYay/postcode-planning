"""
Сравнение точного планировщика с жадным.
"""

from src.dataset import load_dataset
from src.planner import PlannerConfig, SolveConfig
from src.planner.compare import compare_engines, format_comparison


# Сценарии строгой команды решаются за доли секунды:
# тест проверяет само сравнение, а не производительность.
SCENARIOS = {
    "своя команда": PlannerConfig(
        solve=SolveConfig(role_pool="team", counterfactuals=False),
    ),
    "своя команда x0.5": PlannerConfig(
        solve=SolveConfig(role_pool="team", counterfactuals=False),
    ).with_input(capacity_scale=0.5),
}


def test_milp_is_not_worse_than_greedy() -> None:
    rows = compare_engines(load_dataset(), SCENARIOS)

    assert len(rows) == len(SCENARIOS)

    for row in rows:
        assert row.greedy.engine == "greedy"
        assert row.milp.engine == "milp"
        assert row.milp.status == "optimal"

        # Жадный план — нижняя граница для точного.
        assert row.initiatives_gain >= 0
        assert row.milp.initiatives_rung >= row.greedy.initiatives_rung


def test_comparison_table() -> None:
    table = format_comparison(compare_engines(load_dataset(), SCENARIOS))

    assert "сценарий" in table
    assert "своя команда x0.5" in table
