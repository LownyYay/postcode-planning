"""
Пересчёт плана по факту спринта.

Спринты до текущего зафиксированы фактом, закрытые задачи уходят
из модели, у остальных остаётся новый остаток. Baseline только читается:
пересчёт не может изменить базу сравнения.
"""

from dataclasses import asdict

from src.planner.config import SolveConfig
from src.planner.engine import plan
from src.planner.facts import SprintFact, apply_facts, track_progress
from src.planner.result import Change, PlanResult
from src.planner.state import EPS, PlanningState
from src.store import Baseline, BaselineError


def rescale_previous(
    previous_plan: PlanResult,
    baseline_state: PlanningState,
    current_state: PlanningState,
) -> dict[str, dict[int, float]]:
    """
    Пересчитывает доли предыдущего плана в доли нового остатка.

    Задача, которая шла ровно по плану, после пересчёта даёт
    те же доли — и уровень стабильности не видит отклонений.
    """

    result: dict[str, dict[int, float]] = {}

    for task_id, task in current_state.tasks.items():
        previous = previous_plan.tasks.get(task_id)

        if previous is None or not previous.progress:
            continue

        future = {
            sprint: share
            for sprint, share in previous.progress.items()
            if sprint > current_state.current_sprint
        }

        planned = sum(future.values())

        if planned <= EPS:
            continue

        # Остаток работы раскладывается в тех же пропорциях,
        # что и в предыдущем плане, но уже как доли нового остатка.
        result[task_id] = {
            sprint: share / planned
            for sprint, share in future.items()
        }

    return result


def plan_changes(
    baseline_plan: PlanResult,
    previous_plan: PlanResult,
    current_plan: PlanResult,
    current_state: PlanningState,
) -> list[Change]:
    """
    Сдвиги относительно базы и предыдущего плана.
    Закрытая задача получает фактический спринт завершения.
    """

    changes = []

    for task_id, task_plan in sorted(baseline_plan.tasks.items()):
        done_in = current_state.done_tasks.get(task_id)

        if done_in is not None:
            current = done_in
            status = "done"
        elif task_id in current_plan.tasks:
            current = current_plan.tasks[task_id].forecast_sprint
            status = current_plan.tasks[task_id].status
        else:
            current = None
            status = "unknown"

        previous = previous_plan.tasks.get(task_id)

        changes.append(
            Change(
                kind="task",
                id=task_id,
                baseline=task_plan.forecast_sprint,
                previous=previous.forecast_sprint if previous else None,
                current=current,
                status=status,
            )
        )

    for initiative_id, initiative in sorted(baseline_plan.initiatives.items()):
        current = current_plan.initiatives.get(initiative_id)
        previous = previous_plan.initiatives.get(initiative_id)

        changes.append(
            Change(
                kind="initiative",
                id=initiative_id,
                baseline=initiative.forecast_sprint,
                previous=previous.forecast_sprint if previous else None,
                current=current.forecast_sprint if current else None,
                status="done" if current and current.done_in_quarter else "open",
            )
        )

    return changes


def replan(
    baseline_state: PlanningState,
    facts: list[SprintFact],
    previous_plan: PlanResult,
    baseline: Baseline,
    solve: SolveConfig,
) -> PlanResult:
    """
    Пересчитывает оставшуюся часть квартала по фактам спринтов.

    baseline_state — состояние на начало квартала, то же самое,
    по которому строился baseline. Возвращённый план содержит
    сравнение с базой и с предыдущим планом.
    """

    if asdict(baseline_state.input_config) != baseline.input:
        raise BaselineError(
            "Состояние собрано с другими параметрами входа, чем baseline: "
            "сравнивать такие планы нельзя"
        )

    fingerprint = baseline.fingerprint()

    current_state = apply_facts(baseline_state, facts)
    previous_progress = rescale_previous(previous_plan, baseline_state, current_state)

    result = plan(current_state, solve, previous_progress=previous_progress)

    result.changes = plan_changes(
        baseline.plan,
        previous_plan,
        result,
        current_state,
    )

    result.meta.warnings.extend(
        warning
        for warning in current_state.warnings
        if warning not in result.meta.warnings
    )

    if baseline.fingerprint() != fingerprint:
        raise BaselineError("Baseline изменился во время пересчёта")

    return result


def credited_sp(
    baseline_state: PlanningState,
    facts: list[SprintFact],
) -> dict[int, dict[str, float]]:
    """
    Фактически сделанные SP по спринтам и командам.
    """

    tracked = track_progress(baseline_state, facts)
    result: dict[int, dict[str, float]] = {}

    for task_id, progress in tracked.items():
        team = baseline_state.tasks[task_id].team

        for sprint, sp in progress.credited_sp.items():
            teams = result.setdefault(sprint, {})
            teams[team] = teams.get(team, 0.0) + sp

    return result
