"""
Показатели выполнения плана.

Оба показателя считаются относительно baseline: пересчёт плана
не меняет базу сравнения. Прогноз и факт разделены — прогнозные
значения берутся из текущего плана, фактические из фактов спринтов.
"""

from dataclasses import dataclass

from src.planner.facts import SprintFact
from src.planner.replan import credited_sp
from src.planner.result import PlanResult
from src.planner.state import PlanningState
from src.store import Baseline


@dataclass(frozen=True)
class SprintPerformance:
    """
    Выполнение плана спринта: сделанные SP к запланированным.
    """

    sprint: int
    planned_sp: float
    done_sp: float
    by_team: dict[str, tuple[float, float]]

    @property
    def ratio(self) -> float | None:
        if self.planned_sp <= 0:
            return None

        return self.done_sp / self.planned_sp


@dataclass(frozen=True)
class QuarterPerformance:
    """
    Процент выполнения квартального плана.

    planned — инициативы, которые baseline обещал завершить;
    done — из них завершены фактически;
    forecast — из них завершаются по текущему плану, включая факт.
    """

    planned: tuple[str, ...]
    done: tuple[str, ...]
    forecast: tuple[str, ...]

    @property
    def actual_ratio(self) -> float | None:
        if not self.planned:
            return None

        return len(self.done) / len(self.planned)

    @property
    def forecast_ratio(self) -> float | None:
        if not self.planned:
            return None

        return len(self.forecast) / len(self.planned)


def sprint_performance(
    baseline: Baseline,
    baseline_state: PlanningState,
    facts: list[SprintFact],
) -> list[SprintPerformance]:
    """
    Выполнение плана по каждому спринту, где есть факт.

    Плановые SP берутся из baseline и не меняются пересчётами.
    Сделанные SP засчитываются пропорционально прогрессу, а при
    закрытии задачи — весь её остаток.
    """

    planned = baseline.planned_sp_by_sprint()
    done = credited_sp(baseline_state, facts)
    result = []

    for fact in facts:
        planned_teams = planned.get(fact.sprint, {})
        done_teams = done.get(fact.sprint, {})

        teams = sorted(set(planned_teams) | set(done_teams))

        result.append(
            SprintPerformance(
                sprint=fact.sprint,
                planned_sp=sum(planned_teams.values()),
                done_sp=sum(done_teams.values()),
                by_team={
                    team: (planned_teams.get(team, 0.0), done_teams.get(team, 0.0))
                    for team in teams
                },
            )
        )

    return result


def quarter_performance(
    baseline: Baseline,
    current_plan: PlanResult,
    current_state: PlanningState,
) -> QuarterPerformance:
    """
    Процент выполнения квартального плана.

    Знаменатель — инициативы, запланированные к завершению в baseline.
    """

    planned = baseline.planned_initiatives

    done = tuple(
        initiative_id
        for initiative_id in planned
        if _all_tasks_done(initiative_id, current_state)
    )

    forecast = tuple(
        initiative_id
        for initiative_id in planned
        if initiative_id in current_plan.initiatives
        and current_plan.initiatives[initiative_id].done_in_quarter
    )

    return QuarterPerformance(planned=planned, done=done, forecast=forecast)


def _all_tasks_done(initiative_id: str, state: PlanningState) -> bool:
    initiative = state.initiatives.get(initiative_id)

    if initiative is None:
        return False

    return not initiative.open_task_ids
