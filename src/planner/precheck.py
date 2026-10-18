import math
from dataclasses import dataclass

from src.planner.result import (
    REASON_NO_RESOURCES,
    REASON_NOT_READY,
    Reason,
)
from src.planner.config import SolveConfig
from src.planner.rules import (
    blocker_ready_from,
    is_limited,
    peak_pool_fund,
)
from src.planner.state import EPS, PlanningState, TaskInput


@dataclass(frozen=True)
class PrecheckResult:
    """
    Итог предпроверки.

    cancelled — задачи, невозможные при любом плане, с причинами;
    earliest_start — самый ранний спринт, в котором задачу можно начать;
    min_duration — нижняя оценка длительности в спринтах.
    """

    cancelled: dict[str, Reason]
    earliest_start: dict[str, int]
    min_duration: dict[str, int]


def topological_order(state: PlanningState) -> list[str]:
    """
    Открытые задачи так, что блокирующие идут раньше блокируемых.
    При равенстве — по идентификатору.
    """

    open_ids = sorted(state.tasks)
    placed: list[str] = []
    seen: set[str] = set()

    while len(placed) < len(open_ids):
        progressed = False

        for task_id in open_ids:
            if task_id in seen:
                continue

            waiting = [
                blocking
                for blocking in state.predecessors(task_id)
                if blocking in state.tasks and blocking not in seen
            ]

            if not waiting:
                placed.append(task_id)
                seen.add(task_id)
                progressed = True

        if not progressed:
            cycle = sorted(set(open_ids) - seen)
            raise ValueError(
                f"Цикл в зависимостях между задачами: {', '.join(cycle)}"
            )

    return placed


def run_precheck(
    state: PlanningState,
    solve: SolveConfig,
) -> PrecheckResult:
    """
    Отсекает задачи, невозможные при любом плане.

    1. Нужной роли нет ни у кого в выбранной области пула.
    2. Работы по роли или очков больше, чем можно выполнить
       до конца горизонта с учётом зазоров зависимостей.
    3. Блокирующая задача отменена — отмена идёт по цепочке.
    """

    cancelled: dict[str, Reason] = {}
    earliest_start: dict[str, int] = {}
    min_duration: dict[str, int] = {}

    for task_id in topological_order(state):
        task = state.tasks[task_id]

        blocked_by = [
            blocking
            for blocking in state.predecessors(task_id)
            if blocking in cancelled
        ]

        if blocked_by:
            cancelled[task_id] = Reason(
                code=REASON_NOT_READY,
                text=(
                    f"Отменена блокирующая задача {', '.join(blocked_by)}, "
                    f"поэтому {task_id} выполнить нельзя."
                ),
                details={
                    "rule": "cancelled_blocker",
                    "blocking_tasks": blocked_by,
                },
            )
            continue

        missing = _missing_role_reason(state, solve, task)

        if missing is not None:
            cancelled[task_id] = missing
            continue

        duration, limiting = _min_duration(state, solve, task)
        start = _earliest_start(state, solve, task_id, earliest_start, min_duration)

        earliest_start[task_id] = start
        min_duration[task_id] = duration

        if start + duration - 1 > state.horizon:
            available = max(state.horizon - start + 1, 0)

            cancelled[task_id] = Reason(
                code=REASON_NO_RESOURCES,
                text=(
                    f"Даже без конкуренции задаче нужно не меньше "
                    f"{duration} спринтов ({limiting}), а с учётом "
                    f"зависимостей до конца горизонта остаётся {available}."
                ),
                details={
                    "rule": "duration_exceeds_horizon",
                    "min_duration": duration,
                    "earliest_start": start,
                    "sprints_available": available,
                    "limiting": limiting,
                },
            )

    return PrecheckResult(
        cancelled=cancelled,
        earliest_start=earliest_start,
        min_duration=min_duration,
    )


def _missing_role_reason(
    state: PlanningState,
    solve: SolveConfig,
    task: TaskInput,
) -> Reason | None:
    """
    Причина отмены, если задаче нужна роль без носителей в области пула.
    """

    for role, hours in task.remaining_hours.items():
        if not is_limited(state, solve, role):
            # Роли без единого носителя при external не ограничивают план.
            continue

        # Пик по горизонту: роль, доступная хотя бы в одном спринте,
        # задачу не отменяет — она лишь сдвигает срок.
        if peak_pool_fund(state, solve, task.team, role) > EPS:
            continue

        if solve.role_pool == "team":
            elsewhere = state.peak_global_fund(role)
            text = (
                f"В команде {task.team} нет роли «{role}»: "
                f"нужно {hours:.0f} ч, доступно 0 ч."
            )

            if elsewhere > EPS:
                text += (
                    f" В других командах эта роль есть "
                    f"({elsewhere:.0f} ч за спринт), но заимствование выключено."
                )
        else:
            text = (
                f"Роли «{role}» нет ни у одного инженера: "
                f"нужно {hours:.0f} ч, доступно 0 ч."
            )

        return Reason(
            code=REASON_NO_RESOURCES,
            text=text,
            details={
                "rule": "role_missing",
                "role": role,
                "hours_needed": hours,
                "hours_available": 0.0,
                "pool": solve.role_pool,
            },
        )

    return None


def _min_duration(
    state: PlanningState,
    solve: SolveConfig,
    task: TaskInput,
) -> tuple[int, str]:
    """
    Нижняя оценка длительности: сколько спринтов нужно,
    даже если задаче отдать весь фонд каждой роли и всю ёмкость команды.
    """

    duration = 1
    limiting = "работа укладывается в один спринт"

    capacity = state.team_capacity.get(task.team, 0.0)

    if task.sp_effective > EPS:
        if capacity <= EPS:
            return (
                state.horizon + 1,
                f"ёмкость команды {task.team} равна нулю",
            )

        sprints = math.ceil(task.sp_effective / capacity - EPS)

        if sprints > duration:
            duration = sprints
            limiting = (
                f"{task.sp_effective:.1f} SP при ёмкости "
                f"{task.team} {capacity:.1f} SP за спринт"
            )

    for role, hours in task.remaining_hours.items():
        if not is_limited(state, solve, role):
            continue

        fund = peak_pool_fund(state, solve, task.team, role)
        sprints = math.ceil(hours / fund - EPS)

        if sprints > duration:
            duration = sprints
            limiting = (
                f"«{role}»: {hours:.0f} ч при фонде {fund:.0f} ч за спринт"
            )

    return duration, limiting


def _earliest_start(
    state: PlanningState,
    solve: SolveConfig,
    task_id: str,
    earliest_start: dict[str, int],
    min_duration: dict[str, int],
) -> int:
    """
    Самый ранний спринт старта по зазорам зависимостей
    и по ручному ограничению главы команды.
    """

    start = max(
        state.current_sprint + 1,
        state.not_before.get(task_id, 0),
    )

    for blocking in state.predecessors(task_id):
        if blocking in state.done_tasks:
            finish = None
        else:
            finish = earliest_start[blocking] + min_duration[blocking] - 1

        start = max(start, blocker_ready_from(state, solve, blocking, finish))

    return start
