import time
from collections import defaultdict
from dataclasses import asdict

from src.planner.assemble import (
    assemble_plan,
    completion_sprint,
    summarize_objective,
)
from src.planner.config import SolveConfig
from src.planner.precheck import PrecheckResult, run_precheck
from src.planner.result import Meta, PlanResult
from src.planner.rules import (
    blocker_ready_from,
    is_limited,
    pool_scope,
    task_role_fund,
)
from src.planner.state import EPS, PlanningState, TaskInput


def greedy_order(
    state: PlanningState,
    precheck: PrecheckResult,
) -> list[str]:
    """
    Порядок размещения задач.

    Сначала задачи инициатив с большим rung, внутри инициативы —
    по готовности зависимостей, затем по rung задачи и идентификатору.
    Задача берётся в работу, только когда все её блокирующие уже решены.
    """

    def key(task_id: str) -> tuple:
        task = state.tasks[task_id]
        initiative = state.initiatives[task.initiative]

        return (
            -initiative.rung,
            initiative.initiative_id,
            precheck.earliest_start.get(task_id, 0),
            -task.rung,
            task_id,
        )

    pending = sorted(state.tasks, key=key)
    order: list[str] = []
    decided: set[str] = set()

    while pending:
        for task_id in pending:
            waiting = [
                blocking
                for blocking in state.predecessors(task_id)
                if blocking in state.tasks and blocking not in decided
            ]

            if not waiting:
                order.append(task_id)
                decided.add(task_id)
                pending.remove(task_id)
                break
        else:
            raise ValueError("Цикл в зависимостях между задачами")

    return order


class Capacity:
    """
    Остаток ёмкости команд и фондов ролей по спринтам.
    """

    def __init__(self, state: PlanningState, solve: SolveConfig) -> None:
        self.state = state
        self.solve = solve
        self.sp = {
            (team, sprint): capacity
            for team, capacity in state.team_capacity.items()
            for sprint in state.sprints
        }
        self.used: dict[tuple[str, str, int], float] = defaultdict(float)
        self.total_used: dict[tuple[str, int], float] = defaultdict(float)

    def _pool_key(self, task: TaskInput, role: str) -> tuple[str, str]:
        if self.solve.role_pool == "team":
            return (task.team, role)

        return ("*", role)

    def max_share(self, task: TaskInput, sprint: int) -> float:
        """
        Наибольшая доля задачи, которую можно сделать в спринте.
        """

        share = 1.0

        if task.sp_effective > EPS:
            share = min(share, self.sp[(task.team, sprint)] / task.sp_effective)

        for role, hours in task.remaining_hours.items():
            if not is_limited(self.state, self.solve, role):
                continue

            pool = self._pool_key(task, role)
            fund = task_role_fund(self.state, self.solve, task, role, sprint)
            left = fund - self.used[(*pool, sprint)]
            share = min(share, left / hours)

        share = min(share, self._total_hours_share(task, sprint))

        return max(share, 0.0)

    def _total_hours_share(self, task: TaskInput, sprint: int) -> float:
        """
        Доля, которую пропускает потолок часов по всем ролям сразу.
        """

        scope = pool_scope(self.solve, task.team)
        limit = self.state.sprint_hours_limit(scope, sprint)

        if limit is None:
            return 1.0

        demand = sum(
            hours
            for role, hours in task.remaining_hours.items()
            if is_limited(self.state, self.solve, role)
        )

        if demand <= EPS:
            return 1.0

        left = limit - self.total_used[(scope, sprint)]

        return left / demand

    def take(self, task: TaskInput, sprint: int, share: float) -> None:
        self.sp[(task.team, sprint)] -= task.sp_effective * share
        scope = pool_scope(self.solve, task.team)

        for role, hours in task.remaining_hours.items():
            if not is_limited(self.state, self.solve, role):
                continue

            pool = self._pool_key(task, role)
            self.used[(*pool, sprint)] += hours * share
            self.total_used[(scope, sprint)] += hours * share


def _try_start(
    state: PlanningState,
    solve: SolveConfig,
    capacity: Capacity,
    task: TaskInput,
    start: int,
) -> dict[int, float] | None:
    """
    Доли задачи по спринтам при старте в указанном спринте.

    Работа идёт непрерывно: в каждом активном спринте не меньше
    min_progress. Если это невозможно — старт не подходит.
    """

    shares: dict[int, float] = {}
    done = 0.0
    sprint = start

    while done < 1 - EPS:
        if sprint > state.horizon:
            return None

        remaining = 1 - done
        available = capacity.max_share(task, sprint)

        if available >= remaining - EPS:
            share = remaining
        else:
            share = available

            # Хвост меньше минимальной доли нельзя будет закрыть.
            if remaining - share < solve.min_progress - EPS:
                share = remaining - solve.min_progress

        if share < solve.min_progress - EPS or share < 0:
            return None

        shares[sprint] = share
        done += share
        sprint += 1

    return shares


def _place(
    state: PlanningState,
    solve: SolveConfig,
    capacity: Capacity,
    task: TaskInput,
    earliest: int,
) -> dict[int, float] | None:
    """
    Самый ранний допустимый старт задачи.

    Нельзя начинать в квартале то, что в нём не закончится.
    Задача в работе, если попадает в квартал, стартует
    с первого доступного спринта.
    """

    quarter = state.quarter

    for start in range(earliest, state.horizon + 1):
        if (
            task.status == "InProgress"
            and start <= quarter
            and start != earliest
        ):
            continue

        shares = _try_start(state, solve, capacity, task, start)

        if shares is None:
            continue

        finish = completion_sprint(shares)

        if start <= quarter and (finish is None or finish > quarter):
            continue

        return shares

    return None


def plan_greedy(
    state: PlanningState,
    solve: SolveConfig,
    precheck: PrecheckResult | None = None,
    status: str = "greedy",
    fallback_reason: str | None = None,
) -> PlanResult:
    """
    Жадное размещение: задачи по порядку приоритета,
    каждая — в самый ранний допустимый спринт.

    Запасной вариант для MILP и база сравнения.
    """

    started = time.perf_counter()
    precheck = precheck or run_precheck(state, solve)
    capacity = Capacity(state, solve)

    progress: dict[str, dict[int, float]] = {}
    finish: dict[str, int | None] = {}

    for task_id in greedy_order(state, precheck):
        if task_id in precheck.cancelled:
            finish[task_id] = None
            continue

        task = state.tasks[task_id]
        earliest = max(
            state.current_sprint + 1,
            state.not_before.get(task_id, 0),
        )
        blocked = False

        for blocking in state.predecessors(task_id):
            ready_from = blocker_ready_from(state, solve, blocking, finish.get(blocking))

            if ready_from is None:
                blocked = True
            else:
                earliest = max(earliest, ready_from)

        shares = None if blocked else _place(state, solve, capacity, task, earliest)

        if shares is None:
            finish[task_id] = None
            continue

        for sprint, share in shares.items():
            capacity.take(task, sprint, share)

        progress[task_id] = shares
        finish[task_id] = completion_sprint(shares)

    meta = Meta(
        engine="greedy",
        solver=None,
        status=status,
        elapsed_seconds=0.0,
        current_sprint=state.current_sprint,
        input_config=asdict(state.input_config),
        solve_config=asdict(solve),
        warnings=list(state.warnings),
        fallback_reason=fallback_reason,
    )

    plan = assemble_plan(state, solve, progress, precheck.cancelled, meta)
    plan.meta.objective = summarize_objective(state, plan)
    plan.meta.elapsed_seconds = time.perf_counter() - started

    return plan
