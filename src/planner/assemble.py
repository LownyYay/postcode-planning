from collections import defaultdict

from src.planner.result import (
    GOAL_POSTPONED,
    REASON_OVER_PLAN,
    BorrowRow,
    Bottleneck,
    DemandRow,
    InitiativePlan,
    Load,
    LoadCell,
    Meta,
    PlanResult,
    Reason,
    TaskPlan,
)
from src.planner.config import SolveConfig
from src.planner.rules import blocker_ready_from, is_limited
from src.planner.state import EPS, PlanningState


def completion_sprint(
    progress: dict[int, float],
) -> int | None:
    """
    Спринт, в котором накопленная доля достигает 100 %.
    """

    total = 0.0

    for sprint in sorted(progress):
        total += progress[sprint]

        if total >= 1 - EPS:
            return sprint

    return None


def assemble_plan(
    state: PlanningState,
    solve: SolveConfig,
    progress: dict[str, dict[int, float]],
    cancelled: dict[str, Reason],
    meta: Meta,
    reasons: dict[str, Reason] | None = None,
) -> PlanResult:
    """
    Собирает PlanResult из решения движка.

    Движок отдаёт только доли прогресса по спринтам и причины.
    Статусы, часы, нагрузка, заимствования и прогнозы
    считаются здесь одинаково для жадного плана и для MILP.
    """

    reasons = reasons or {}
    quarter = state.quarter

    tasks: dict[str, TaskPlan] = {}

    for task_id, task in state.tasks.items():
        if task_id in cancelled:
            reason = cancelled[task_id]

            tasks[task_id] = TaskPlan(
                task_id=task_id,
                team=task.team,
                initiative=task.initiative,
                status="cancelled",
                reason_code=reason.code,
                reason_text=reason.text,
                details=dict(reason.details),
            )
            continue

        task_progress = {
            sprint: share
            for sprint, share in sorted(progress.get(task_id, {}).items())
            if share > EPS
        }

        finish = completion_sprint(task_progress)
        included = finish is not None and finish <= quarter

        plan = TaskPlan(
            task_id=task_id,
            team=task.team,
            initiative=task.initiative,
            status="included" if included else "postponed",
            progress=task_progress,
            sprint_start=min(task_progress) if task_progress else None,
            sprint_end=max(task_progress) if task_progress else None,
            forecast_sprint=finish,
            sp_by_sprint={
                sprint: task.sp_effective * share
                for sprint, share in task_progress.items()
            },
            hours_by_sprint={
                sprint: {
                    role: hours * share
                    for role, hours in task.remaining_hours.items()
                }
                for sprint, share in task_progress.items()
            },
        )

        if task_id in reasons:
            reason = reasons[task_id]
            plan.reason_code = reason.code
            plan.reason_text = reason.text
            plan.details = dict(reason.details)
        elif task.zero_remaining and included:
            plan.reason_text = "Остаток 0 ч — закрытие без затрат."
            plan.details = {"rule": "zero_remaining"}
        elif not included:
            plan.reason_code = REASON_OVER_PLAN
            plan.reason_text = _postponed_text(plan, quarter)
            plan.details = {
                "goal_option": GOAL_POSTPONED,
                "forecast_sprint": finish,
            }

        tasks[task_id] = plan

    return PlanResult(
        tasks=tasks,
        initiatives=_initiatives(state, tasks),
        load=_load(state, solve, tasks),
        borrowing=_borrowing(state, solve, tasks),
        next_sprint_demand=_next_sprint_demand(state, solve, tasks),
        meta=meta,
    )


def _postponed_text(plan: TaskPlan, quarter: int) -> str:
    if plan.forecast_sprint is None:
        return (
            "Задача выполнима, но проиграла приоритетам "
            "и не помещается в горизонт планирования."
        )

    return (
        f"Задача выполнима, но проиграла приоритетам: "
        f"прогноз завершения — спринт {plan.forecast_sprint}, "
        f"за пределами квартала ({quarter} спринтов)."
    )


def _initiatives(
    state: PlanningState,
    tasks: dict[str, TaskPlan],
) -> dict[str, InitiativePlan]:
    quarter = state.quarter
    result = {}

    for initiative_id, initiative in state.initiatives.items():
        open_ids = list(initiative.open_task_ids)
        plans = [tasks[task_id] for task_id in open_ids]

        done = all(plan.status == "included" for plan in plans)

        finishes = [plan.forecast_sprint for plan in plans]

        if not plans:
            forecast = 0
        elif any(
            finish is None or plan.status == "cancelled"
            for finish, plan in zip(finishes, plans)
        ):
            forecast = None
        else:
            forecast = max(finishes)

        blocking = [
            plan.task_id
            for plan in plans
            if plan.status != "included"
        ]

        partial = not done and any(
            sprint <= quarter
            for plan in plans
            for sprint in plan.progress
        )

        result[initiative_id] = InitiativePlan(
            initiative_id=initiative_id,
            rung=initiative.rung,
            done_in_quarter=done,
            forecast_sprint=forecast,
            open_task_ids=open_ids,
            blocking_task_ids=blocking,
            partial=partial,
        )

    return result


def _usage(
    tasks: dict[str, TaskPlan],
) -> tuple[
    dict[tuple[str, int], float],
    dict[tuple[str, str, int], float],
]:
    """
    Суммарные очки команды и часы роли команды по спринтам.
    """

    team_sp: dict[tuple[str, int], float] = defaultdict(float)
    team_role: dict[tuple[str, str, int], float] = defaultdict(float)

    for plan in tasks.values():
        for sprint, sp in plan.sp_by_sprint.items():
            team_sp[(plan.team, sprint)] += sp

        for sprint, roles in plan.hours_by_sprint.items():
            for role, hours in roles.items():
                team_role[(plan.team, role, sprint)] += hours

    return team_sp, team_role


def _load(
    state: PlanningState,
    solve: SolveConfig,
    tasks: dict[str, TaskPlan],
) -> Load:
    team_sp_usage, team_role_usage = _usage(tasks)
    sprints = list(state.sprints)
    load = Load()

    for team in state.teams:
        load.team_sp[team] = {
            sprint: LoadCell(
                planned=team_sp_usage.get((team, sprint), 0.0),
                capacity=state.team_capacity[team],
            )
            for sprint in sprints
        }

    roles = sorted(
        {role for (_, role, _) in team_role_usage}
        | {role for (_, role) in state.team_role_fund}
        | {role for (_, role, _) in state.sprint_role_fund}
    )

    for role in roles:
        load.role_hours[role] = {
            sprint: LoadCell(
                planned=sum(
                    team_role_usage.get((team, role, sprint), 0.0)
                    for team in state.teams
                ),
                capacity=state.global_fund(role, sprint),
            )
            for sprint in sprints
        }

    for team in state.teams:
        team_roles = sorted(
            {
                role
                for (usage_team, role, _) in team_role_usage
                if usage_team == team
            }
            | {
                role
                for (fund_team, role) in state.team_role_fund
                if fund_team == team
            }
            | {
                role
                for (fund_team, role, _) in state.sprint_role_fund
                if fund_team == team
            }
        )

        load.team_role_hours[team] = {
            role: {
                sprint: LoadCell(
                    planned=team_role_usage.get((team, role, sprint), 0.0),
                    capacity=state.team_fund(team, role, sprint),
                )
                for sprint in sprints
            }
            for role in team_roles
        }

    load.bottlenecks = _bottlenecks(state, solve, load)

    return load


def _is_full(cell: LoadCell) -> bool:
    return (
        cell.capacity > EPS
        and cell.planned >= cell.capacity - 1e-4
    )


def _bottlenecks(
    state: PlanningState,
    solve: SolveConfig,
    load: Load,
) -> list[Bottleneck]:
    rows = []

    for team, cells in load.team_sp.items():
        for sprint, cell in cells.items():
            if _is_full(cell):
                rows.append(
                    Bottleneck("team_sp", team, sprint, cell.planned, cell.capacity)
                )

    if solve.role_pool == "team":
        for team, roles in load.team_role_hours.items():
            for role, cells in roles.items():
                if not is_limited(state, solve, role):
                    continue

                for sprint, cell in cells.items():
                    if _is_full(cell):
                        rows.append(
                            Bottleneck(
                                "role_hours",
                                f"{team} / {role}",
                                sprint,
                                cell.planned,
                                cell.capacity,
                            )
                        )
    else:
        for role, cells in load.role_hours.items():
            if not is_limited(state, solve, role):
                continue

            for sprint, cell in cells.items():
                if _is_full(cell):
                    rows.append(
                        Bottleneck("role_hours", role, sprint, cell.planned, cell.capacity)
                    )

    return sorted(rows, key=lambda row: (row.sprint, row.kind, row.key))


def _borrowing(
    state: PlanningState,
    solve: SolveConfig,
    tasks: dict[str, TaskPlan],
) -> list[BorrowRow]:
    """
    Часы сверх фонда своей команды.

    При role_pool=team заимствований не бывает по построению.
    """

    if solve.role_pool == "team":
        return []

    _, team_role_usage = _usage(tasks)
    rows = []

    for (team, role, sprint), hours in sorted(team_role_usage.items()):
        if not is_limited(state, solve, role):
            continue

        excess = hours - state.team_fund(team, role, sprint)

        if excess > 1e-4:
            rows.append(BorrowRow(team, role, sprint, excess))

    return rows


def _next_sprint_demand(
    state: PlanningState,
    solve: SolveConfig,
    tasks: dict[str, TaskPlan],
) -> list[DemandRow]:
    """
    Спрос задач, готовых к работе в следующем спринте.

    Готова — не отменена, не завершена раньше и все её блокирующие
    задачи закрыты с соблюдением зазора.
    """

    sprint = state.current_sprint + 1

    if sprint > state.horizon:
        return []

    _, team_role_usage = _usage(tasks)
    ready_hours: dict[tuple[str, str], float] = defaultdict(float)

    for task_id, plan in tasks.items():
        if plan.status == "cancelled":
            continue

        if plan.forecast_sprint is not None and plan.forecast_sprint < sprint:
            continue

        if not _blockers_allow(state, solve, tasks, task_id, sprint):
            continue

        task = state.tasks[task_id]

        for role, hours in task.remaining_hours.items():
            ready_hours[(task.team, role)] += hours

    return [
        DemandRow(
            sprint=sprint,
            team=team,
            role=role,
            ready_hours=hours,
            planned_hours=team_role_usage.get((team, role, sprint), 0.0),
            fund_hours=state.team_fund(team, role, sprint),
        )
        for (team, role), hours in sorted(ready_hours.items())
    ]


def _blockers_allow(
    state: PlanningState,
    solve: SolveConfig,
    tasks: dict[str, TaskPlan],
    task_id: str,
    sprint: int,
) -> bool:
    """
    Можно ли начать задачу в указанном спринте по правилу зазора.
    """

    for blocking in state.predecessors(task_id):
        finish = None if blocking in state.done_tasks else tasks[blocking].forecast_sprint
        ready_from = blocker_ready_from(state, solve, blocking, finish)

        if ready_from is None or sprint < ready_from:
            return False

    return True


def summarize_objective(
    state: PlanningState,
    plan: PlanResult,
) -> dict[str, float]:
    """
    Значения уровней лестницы целей для готового плана.

    Считаются одинаково для жадного плана и MILP,
    поэтому их можно сравнивать напрямую.
    """

    quarter = state.quarter

    done = [
        initiative
        for initiative in plan.initiatives.values()
        if initiative.done_in_quarter
    ]

    early_finish = sum(
        quarter - task_plan.forecast_sprint + 1
        for task_plan in plan.tasks.values()
        if task_plan.status == "included"
    )

    return {
        "initiatives_done": float(len(done)),
        "initiatives_rung": float(sum(item.rung for item in done)),
        # Заимствования квартала — то, что минимизирует L4;
        # после квартала план только прогноз.
        "borrowed_hours": float(
            sum(row.hours for row in plan.borrowing if row.sprint <= quarter)
        ),
        "borrowed_hours_horizon": float(sum(row.hours for row in plan.borrowing)),
        "tasks_included": float(len(plan.included_task_ids)),
        "early_finish": float(early_finish),
    }
