from collections import defaultdict

from src.planner.assemble import completion_sprint
from src.planner.config import SolveConfig
from src.planner.rules import blocker_ready_from, is_limited, pool_scope
from src.planner.result import TASK_STATUSES, PlanResult
from src.planner.state import PlanningState


# Допуск на сравнение часов и очков: решатель
# возвращает числа с плавающей точкой.
TOLERANCE = 1e-4


class PlanValidationError(Exception):
    """
    План нарушает правила планирования.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations

        super().__init__(
            "План нарушает правила:\n- "
            + "\n- ".join(violations)
        )


def validate_plan(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig | None = None,
) -> None:
    """
    Проверяет план независимо от модели и решателя.

    Подходит для любого плана — жадного и MILP.
    Правила решения берутся из solve, а если он не передан —
    из меты плана. При нарушениях выбрасывает PlanValidationError.
    """

    violations = find_violations(plan, state, solve)

    if violations:
        raise PlanValidationError(violations)


def find_violations(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig | None = None,
) -> list[str]:
    """
    Возвращает список нарушений; пустой список — план корректен.
    """

    if solve is None:
        solve = SolveConfig(**plan.meta.solve_config)

    checks = (
        _check_task_set,
        _check_task_records,
        _check_team_sp,
        _check_role_hours,
        _check_total_hours,
        _check_dependencies,
        _check_initiatives,
        _check_borrowing,
    )

    violations: list[str] = []

    for check in checks:
        violations.extend(check(plan, state, solve))

    return violations


def _near(value: float, target: float) -> bool:
    return abs(value - target) <= TOLERANCE * max(1.0, abs(target))


def _check_task_set(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    В плане ровно открытые задачи состояния.
    """

    planned = set(plan.tasks)
    expected = set(state.tasks)

    violations = [
        f"{task_id}: открытая задача отсутствует в плане"
        for task_id in sorted(expected - planned)
    ]

    violations += [
        f"{task_id}: задача в плане, но не открыта"
        for task_id in sorted(planned - expected)
    ]

    return violations


def _check_task_records(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Доли, окно работы, часы по ролям, статусы и причины.
    """

    quarter = state.quarter
    violations = []

    for task_id, task_plan in sorted(plan.tasks.items()):
        if task_id not in state.tasks:
            continue

        task = state.tasks[task_id]
        progress = task_plan.progress

        if task_plan.status not in TASK_STATUSES:
            violations.append(f"{task_id}: неизвестный статус {task_plan.status}")
            continue

        if task_plan.status != "included":
            if not task_plan.reason_code or not task_plan.reason_text:
                violations.append(
                    f"{task_id}: статус {task_plan.status} без причины"
                )
            elif state.reason_codes and task_plan.reason_code not in state.reason_codes:
                violations.append(
                    f"{task_id}: код причины «{task_plan.reason_code}» "
                    f"не из справочника датасета"
                )

        if task_plan.status == "cancelled":
            if progress:
                violations.append(f"{task_id}: отменённая задача получила работу")
            continue

        bad_sprints = [
            sprint
            for sprint in progress
            if sprint not in state.sprints
        ]

        if bad_sprints:
            violations.append(
                f"{task_id}: работа вне горизонта или в прошедших спринтах: "
                f"{bad_sprints}"
            )

        if any(share < -TOLERANCE or share > 1 + TOLERANCE for share in progress.values()):
            violations.append(f"{task_id}: доля спринта вне диапазона 0..1")

        total = sum(progress.values())

        if total > 1 + TOLERANCE:
            violations.append(f"{task_id}: сумма долей {total:.4f} больше 100 %")

        finish = completion_sprint(progress)

        if task_plan.forecast_sprint != finish:
            violations.append(
                f"{task_id}: прогноз {task_plan.forecast_sprint}, "
                f"а доли дают завершение в спринте {finish}"
            )

        if task_plan.status == "included":
            if finish is None or finish > quarter:
                violations.append(
                    f"{task_id}: включена, но не завершается в квартале"
                )
        elif finish is not None and finish <= quarter:
            violations.append(
                f"{task_id}: завершается в квартале, но не включена"
            )

        # Не начинаем в квартале то, что не закончим в квартале.
        if any(sprint <= quarter for sprint in progress) and (
            finish is None or finish > quarter
        ):
            violations.append(
                f"{task_id}: начата в квартале, но не завершается в нём"
            )

        if progress:
            start, end = min(progress), max(progress)

            not_before = state.not_before.get(task_id)

            if not_before is not None and start < not_before:
                violations.append(
                    f"{task_id}: работа начата в спринте {start}, "
                    f"а глава команды указал не раньше {not_before}"
                )

            if (task_plan.sprint_start, task_plan.sprint_end) != (start, end):
                violations.append(
                    f"{task_id}: окно {task_plan.sprint_start}–{task_plan.sprint_end} "
                    f"не совпадает с работой {start}–{end}"
                )

            # Работа идёт непрерывно и не мельче минимальной доли.
            for sprint in range(start, end + 1):
                share = progress.get(sprint, 0.0)

                if share < solve.min_progress - TOLERANCE:
                    violations.append(
                        f"{task_id}: в спринте {sprint} доля {share:.3f} "
                        f"меньше минимальной {solve.min_progress}"
                    )
        elif task_plan.sprint_start is not None or task_plan.sprint_end is not None:
            violations.append(f"{task_id}: есть окно работы, но нет долей")

        violations += _check_task_amounts(task_id, task, task_plan)

    return violations


def _check_task_amounts(task_id, task, task_plan) -> list[str]:
    """
    Очки и часы по спринтам соответствуют долям и не превышают остаток.
    """

    violations = []

    if set(task_plan.sp_by_sprint) != set(task_plan.progress):
        violations.append(f"{task_id}: очки расписаны не по спринтам работы")

    if set(task_plan.hours_by_sprint) != set(task_plan.progress):
        violations.append(f"{task_id}: часы расписаны не по спринтам работы")

    spent: dict[str, float] = defaultdict(float)

    for sprint, share in task_plan.progress.items():
        sp = task_plan.sp_by_sprint.get(sprint, 0.0)

        if not _near(sp, task.sp_effective * share):
            violations.append(
                f"{task_id}: в спринте {sprint} списано {sp:.3f} SP "
                f"вместо {task.sp_effective * share:.3f}"
            )

        for role, hours in task_plan.hours_by_sprint.get(sprint, {}).items():
            if role not in task.remaining_hours:
                violations.append(
                    f"{task_id}: часы роли «{role}», которой нет в остатке"
                )
                continue

            spent[role] += hours

            expected = task.remaining_hours[role] * share

            if not _near(hours, expected):
                violations.append(
                    f"{task_id}: в спринте {sprint} роль «{role}» "
                    f"{hours:.2f} ч вместо {expected:.2f}"
                )

    for role, hours in spent.items():
        limit = task.remaining_hours[role]

        if hours > limit + TOLERANCE * max(1.0, limit):
            violations.append(
                f"{task_id}: роль «{role}» {hours:.2f} ч больше остатка {limit:.2f}"
            )

    return violations


def _team_usage(plan: PlanResult):
    team_sp: dict[tuple[str, int], float] = defaultdict(float)
    team_role: dict[tuple[str, str, int], float] = defaultdict(float)

    for task_plan in plan.tasks.values():
        for sprint, sp in task_plan.sp_by_sprint.items():
            team_sp[(task_plan.team, sprint)] += sp

        for sprint, roles in task_plan.hours_by_sprint.items():
            for role, hours in roles.items():
                team_role[(task_plan.team, role, sprint)] += hours

    return team_sp, team_role


def _check_team_sp(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Очки команды за спринт не больше её ёмкости.
    """

    team_sp, _ = _team_usage(plan)
    violations = []

    for (team, sprint), sp in sorted(team_sp.items()):
        capacity = state.team_capacity.get(team, 0.0)

        if sp > capacity + TOLERANCE * max(1.0, capacity):
            violations.append(
                f"{team}, спринт {sprint}: {sp:.2f} SP при ёмкости {capacity:.2f}"
            )

    return violations


def _check_role_hours(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Часы роли за спринт не больше фонда в выбранной области пула.
    """

    _, team_role = _team_usage(plan)
    violations = []

    if solve.role_pool == "team":
        for (team, role, sprint), hours in sorted(team_role.items()):
            if not is_limited(state, solve, role):
                continue

            fund = state.team_fund(team, role, sprint)

            if hours > fund + TOLERANCE * max(1.0, fund):
                violations.append(
                    f"{team} / {role}, спринт {sprint}: "
                    f"{hours:.2f} ч при фонде команды {fund:.2f}"
                )

        return violations

    total: dict[tuple[str, int], float] = defaultdict(float)

    for (_, role, sprint), hours in team_role.items():
        total[(role, sprint)] += hours

    for (role, sprint), hours in sorted(total.items()):
        if not is_limited(state, solve, role):
            continue

        fund = state.global_fund(role, sprint)

        if hours > fund + TOLERANCE * max(1.0, fund):
            violations.append(
                f"{role}, спринт {sprint}: {hours:.2f} ч "
                f"при фонде организации {fund:.2f}"
            )

    return violations


def _check_total_hours(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Часы по всем ролям сразу за спринт не больше потолка hours_limit.

    Потолок не даёт потратить дважды часы человека, который числится
    в нескольких ролях. Без записей в hours_limit проверка пустая.
    """

    if not state.hours_limit:
        return []

    _, team_role = _team_usage(plan)
    total: dict[tuple[str, int], float] = defaultdict(float)

    for (team, role, sprint), hours in team_role.items():
        if not is_limited(state, solve, role):
            continue

        total[(pool_scope(solve, team), sprint)] += hours

    violations = []

    for (scope, sprint), hours in sorted(total.items()):
        limit = state.sprint_hours_limit(scope, sprint)

        if limit is None:
            continue

        if hours > limit + TOLERANCE * max(1.0, limit):
            where = "организация" if scope == "*" else scope
            violations.append(
                f"{where}, спринт {sprint}: {hours:.2f} ч по всем ролям "
                f"при потолке {limit:.2f}"
            )

    return violations


def _check_dependencies(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Зазоры зависимостей и готовность блокирующих задач.
    """

    violations = []

    for blocking, blocked in state.dependencies:
        if blocked not in plan.tasks:
            continue

        blocked_plan = plan.tasks[blocked]

        if blocking in state.done_tasks:
            finish = state.done_tasks[blocking]
            blocking_included = True
        else:
            blocking_plan = plan.tasks.get(blocking)
            finish = blocking_plan.forecast_sprint if blocking_plan else None
            blocking_included = (
                blocking_plan is not None
                and blocking_plan.status == "included"
            )

            if blocking_plan is not None and blocking_plan.status == "cancelled":
                if blocked_plan.status != "cancelled":
                    violations.append(
                        f"{blocked}: блокирующая {blocking} отменена, "
                        f"а блокируемая нет"
                    )

        if blocked_plan.status == "included" and not blocking_included:
            violations.append(
                f"{blocked}: включена, хотя блокирующая {blocking} "
                f"не включена и не закрыта"
            )

        if not blocked_plan.progress:
            continue

        start = min(blocked_plan.progress)
        ready_from = blocker_ready_from(state, solve, blocking, finish)

        if ready_from is None:
            violations.append(
                f"{blocked}: работа начата, а блокирующая {blocking} "
                f"не завершается в горизонте"
            )
        elif start < ready_from:
            gap = ready_from - finish - 1
            violations.append(
                f"{blocked}: старт в спринте {start}, а {blocking} "
                f"завершается в спринте {finish}; нужен зазор {gap}"
            )

    return violations


def _check_initiatives(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Инициатива завершена тогда и только тогда,
    когда все её задачи включены или закрыты.
    """

    violations = []

    for initiative_id, initiative in state.initiatives.items():
        initiative_plan = plan.initiatives.get(initiative_id)

        if initiative_plan is None:
            violations.append(f"{initiative_id}: инициатива отсутствует в плане")
            continue

        expected = all(
            plan.tasks[task_id].status == "included"
            for task_id in initiative.open_task_ids
            if task_id in plan.tasks
        )

        if initiative_plan.done_in_quarter != expected:
            violations.append(
                f"{initiative_id}: отмечена "
                f"{'завершённой' if initiative_plan.done_in_quarter else 'незавершённой'}, "
                f"что не совпадает со статусами её задач"
            )

    return violations


def _check_borrowing(plan: PlanResult, state: PlanningState, solve: SolveConfig) -> list[str]:
    """
    Каждый час сверх фонда своей команды отражён в таблице заимствований.
    """

    if solve.role_pool == "team":
        if plan.borrowing:
            return ["Заимствования при role_pool=team"]
        return []

    _, team_role = _team_usage(plan)

    recorded = {
        (row.team, row.role, row.sprint): row.hours
        for row in plan.borrowing
    }

    violations = []

    for (team, role, sprint), hours in sorted(team_role.items()):
        if not is_limited(state, solve, role):
            continue

        excess = hours - state.team_fund(team, role, sprint)

        if excess > 1e-3 and not _near(recorded.get((team, role, sprint), 0.0), excess):
            violations.append(
                f"{team} / {role}, спринт {sprint}: заимствовано {excess:.2f} ч, "
                f"в таблице {recorded.get((team, role, sprint), 0.0):.2f}"
            )

    return violations
