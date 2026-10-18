"""
Правила, которые зависят от параметров решения.

Одни и те же функции используют предпроверка, оба движка и валидатор,
поэтому правила не могут разойтись между ними.
"""

from src.planner.config import SolveConfig
from src.planner.state import PlanningState, TaskInput


def is_limited(
    state: PlanningState,
    solve: SolveConfig,
    role: str,
) -> bool:
    """
    Ограничивает ли роль план.

    Роли без единого носителя при missing_roles=external
    выполняются вне фонда и выводятся как риск.
    """

    return not (
        role in state.missing_roles
        and solve.missing_roles == "external"
    )


def pool_fund(
    state: PlanningState,
    solve: SolveConfig,
    team: str,
    role: str,
    sprint: int | None = None,
) -> float:
    """
    Часы роли за спринт, доступные задаче команды
    в выбранной области пула.
    """

    if solve.role_pool == "team":
        return state.team_fund(team, role, sprint)

    return state.global_fund(role, sprint)


def peak_pool_fund(
    state: PlanningState,
    solve: SolveConfig,
    team: str,
    role: str,
) -> float:
    """
    Наибольший фонд роли по спринтам горизонта в области пула.

    Для проверок «выполнима ли работа в принципе»: фонд, равный нулю
    в одном спринте, ещё не делает задачу невозможной.
    """

    if solve.role_pool == "team":
        return state.peak_team_fund(team, role)

    return state.peak_global_fund(role)


def window_pool_fund(
    state: PlanningState,
    solve: SolveConfig,
    team: str,
    role: str,
    sprints,
) -> float:
    """
    Сумма фонда роли по набору спринтов в области пула.
    """

    if solve.role_pool == "team":
        return state.window_team_fund(team, role, sprints)

    return state.window_global_fund(role, sprints)


def pool_scope(solve: SolveConfig, team: str) -> str:
    """
    Область, в которой считается потолок часов: команда или организация.
    """

    return team if solve.role_pool == "team" else "*"


def task_role_fund(
    state: PlanningState,
    solve: SolveConfig,
    task: TaskInput,
    role: str,
    sprint: int | None = None,
) -> float:
    return pool_fund(state, solve, task.team, role, sprint)


def blocker_ready_from(
    state: PlanningState,
    solve: SolveConfig,
    blocking: str,
    finish: int | None,
) -> int | None:
    """
    Первый спринт, в котором можно начать задачу,
    заблокированную задачей blocking.

    Для закрытой блокирующей действует gap_done,
    для запланированной — gap_planned. None — блокирующая
    не завершается в горизонте.
    """

    if blocking in state.done_tasks:
        return state.done_tasks[blocking] + 1 + solve.gap_done

    if finish is None:
        return None

    return finish + 1 + solve.gap_planned
