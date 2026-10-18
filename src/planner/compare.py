"""
Сравнение точного планировщика с жадным.

Оба движка работают по одним правилам и проверяются одним валидатором,
поэтому их результаты сравнимы напрямую. Точный план не может завершить
меньше инициатив, чем жадный: жадный служит для него нижней границей.
"""

from dataclasses import dataclass, replace

from src.dataset import Dataset
from src.planner.config import PlannerConfig
from src.planner.engine import run_scenario


@dataclass(frozen=True)
class EngineResult:
    engine: str
    status: str
    initiatives_done: int
    initiatives_rung: float
    tasks_included: int
    borrowed_hours: float
    seconds: float


@dataclass(frozen=True)
class Comparison:
    """
    Один сценарий: что дал жадный план и что дал точный.
    """

    scenario: str
    greedy: EngineResult
    milp: EngineResult

    @property
    def initiatives_gain(self) -> int:
        return self.milp.initiatives_done - self.greedy.initiatives_done


def _result(plan) -> EngineResult:
    objective = plan.meta.objective

    return EngineResult(
        engine=plan.meta.engine,
        status=plan.meta.status,
        initiatives_done=int(objective["initiatives_done"]),
        initiatives_rung=objective["initiatives_rung"],
        tasks_included=int(objective["tasks_included"]),
        borrowed_hours=objective["borrowed_hours"],
        seconds=plan.meta.elapsed_seconds,
    )


def compare_engines(
    dataset: Dataset,
    scenarios: dict[str, PlannerConfig],
) -> list[Comparison]:
    """
    Прогоняет сценарии обоими движками.
    """

    rows = []

    for name, config in scenarios.items():
        greedy = run_scenario(
            dataset,
            replace(config, solve=replace(config.solve, engine="greedy")),
        )

        milp = run_scenario(
            dataset,
            replace(config, solve=replace(config.solve, engine="milp")),
        )

        rows.append(
            Comparison(
                scenario=name,
                greedy=_result(greedy),
                milp=_result(milp),
            )
        )

    return rows


def format_comparison(rows: list[Comparison]) -> str:
    """
    Таблица сравнения для консоли и отчёта.
    """

    width = max([len(row.scenario) for row in rows] + [len("сценарий")]) + 2

    header = (
        f"{'сценарий':{width}}{'жадный':>8}{'точный':>8}"
        f"{'выигрыш':>9}{'секунд':>9}"
    )

    lines = [header, "-" * len(header)]

    for row in rows:
        lines.append(
            f"{row.scenario:{width}}"
            f"{row.greedy.initiatives_done:>8}"
            f"{row.milp.initiatives_done:>8}"
            f"{row.initiatives_gain:>9}"
            f"{row.milp.seconds:>9.1f}"
        )

    return "\n".join(lines)
