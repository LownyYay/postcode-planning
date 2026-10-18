"""
Риски плана: красный, жёлтый и оранжевый алерты.

Красный — прогноз завершения инициативы выходит за пределы квартала,
то есть бизнес-инициатива срывается.

Жёлтый — сдвиг относительно исходной базы после пересчёта:
- cascade_shift — задача сдвинулась и потянула за собой цепочку зависимых
  работ, даже если дедлайн квартала пока не нарушен;
- initiative_slip — квартальная цель по-прежнему в квартале, но завершается
  позже, чем по базе, и теряет запас.

Оранжевый — дефицит специалистов на следующий спринт:
- role_deficit — роль в следующем спринте занята полностью, а готовые
  к работе задачи этой роли ждут; или все её носители отсутствуют;
- team_gap — работам команды нужно больше часов роли, чем есть у её людей,
  и план берёт недостающее у других команд: нужна договорённость о помощи;
- role_missing — роли, которых нет ни у одного инженера: их работа
  в плане не ограничена и выполняется вне штата.

Все числа берутся из готового плана: прогнозы, список изменений
относительно базы, загрузка ролей, заимствования и спрос на следующий спринт.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from src.planner.config import SolveConfig
from src.planner.result import PlanResult
from src.planner.rules import blocker_ready_from, is_limited
from src.planner.state import EPS, PlanningState
from src.text import first_sentences, sprints


LEVELS = ("red", "yellow", "orange")

LEVEL_ORDER = {level: index for index, level in enumerate(LEVELS)}

# Порядок видов внутри уровня: сначала то, что требует решения сейчас.
KIND_ORDER = {
    "initiative_overrun": 0,
    "cascade_shift": 1,
    "initiative_slip": 2,
    "role_deficit": 3,
    "team_gap": 4,
    "role_missing": 5,
}

# Роль считается занятой полностью, если свободно меньше часа
# или меньше 2 % фонда спринта.
FULL_TOLERANCE_HOURS = 1.0
FULL_TOLERANCE_SHARE = 0.02

# Сколько задач называть в тексте риска.
TEXT_LIMIT = 5


@dataclass(frozen=True)
class Risk:
    """
    Одно предупреждение.

    level — цвет алерта, kind — вид риска, subject — инициатива,
    задача, роль или команда, text — формулировка для интерфейса,
    details — числа, по которым риск найден.
    """

    level: str
    kind: str
    subject: str
    text: str
    details: dict[str, object] = field(default_factory=dict)


def find_risks(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
) -> list[Risk]:
    """
    Собирает все риски плана, от критических к менее срочным.
    """

    risks = [
        *_quarter_overrun(plan, state),
        *_cascade_shift(plan, state),
        *_initiative_slip(plan, state),
        *_role_deficit(plan, state, solve),
        *_team_gap(plan, state, solve),
        *_role_missing(plan, state, solve),
    ]

    return sorted(
        risks,
        key=lambda risk: (
            LEVEL_ORDER[risk.level],
            KIND_ORDER.get(risk.kind, len(KIND_ORDER)),
            -float(risk.details.get("gap_hours", 0) or 0),
            risk.subject,
        ),
    )


def _sprint(value: int | None) -> str:
    return "не завершается в горизонте" if value is None else f"С{value}"


def _listing(items: list[str], limit: int = TEXT_LIMIT) -> str:
    shown = ", ".join(items[:limit])

    if len(items) > limit:
        shown += f" и ещё {len(items) - limit}"

    return shown


def _baseline_forecasts(plan: PlanResult, kind: str) -> dict[str, int | None]:
    """
    Спринт завершения по исходной базе: есть только после пересчёта.
    """

    return {
        change.id: change.baseline
        for change in plan.changes
        if change.kind == kind
    }


def _goal_state(
    plan: PlanResult,
    state: PlanningState,
    initiative_id: str,
    baseline: dict[str, int | None],
) -> str:
    """
    Где сейчас инициатива: «PRODF-7124 — С6, в квартале (по базе С4)».
    """

    initiative = plan.initiatives.get(initiative_id)

    if initiative is None:
        return initiative_id

    where = "в квартале" if initiative.done_in_quarter else "за кварталом"
    text = f"{initiative_id} — {_sprint(initiative.forecast_sprint)}, {where}"
    was = baseline.get(initiative_id)

    if initiative_id in baseline and was != initiative.forecast_sprint:
        text += f" (по базе {_sprint(was)})"

    return text


def _quarter_overrun(
    plan: PlanResult,
    state: PlanningState,
) -> list[Risk]:
    """
    Красный алерт: инициатива не завершается в квартале.
    """

    baseline = _baseline_forecasts(plan, "initiative")
    risks = []

    for initiative_id, initiative in sorted(plan.initiatives.items()):
        if initiative.done_in_quarter:
            continue

        blocking = initiative.blocking_task_ids
        forecast = initiative.forecast_sprint

        if forecast is None:
            when = f"в горизонте {state.horizon} спринтов не завершается"
        else:
            when = f"прогноз завершения — спринт {forecast}"

        was = baseline.get(initiative_id)
        was_goal = was is not None and was <= state.quarter
        history = (
            f" По исходному плану она завершалась в спринте {was} — "
            f"это квартальная цель."
            if was_goal
            else ""
        )
        causes = first_sentences(
            plan.tasks[task_id].reason_text
            for task_id in blocking
            if task_id in plan.tasks
        )
        cause = f" Причина: {' '.join(causes)}" if causes else ""

        risks.append(
            Risk(
                level="red",
                kind="initiative_overrun",
                subject=initiative_id,
                text=(
                    f"Инициатива {initiative_id} (rung {initiative.rung:g}) "
                    f"срывается: {when} при квартале в {state.quarter} спринтов."
                    f"{history} Мешают задачи: {', '.join(blocking)}.{cause}"
                ),
                details={
                    "rung": initiative.rung,
                    "forecast_sprint": forecast,
                    "quarter_sprints": state.quarter,
                    "blocking_task_ids": list(blocking),
                    "baseline_sprint": was,
                    "baseline_goal": was_goal,
                    "causes": causes,
                    "counterfactual": initiative.counterfactual,
                },
            )
        )

    return risks


def _task_shifts(plan: PlanResult) -> dict[str, int]:
    return {
        change.id: change.shift_vs_baseline
        for change in plan.changes
        if change.kind == "task"
        and change.shift_vs_baseline
        and change.shift_vs_baseline > 0
    }


def _cascade_shift(
    plan: PlanResult,
    state: PlanningState,
) -> list[Risk]:
    """
    Жёлтый алерт: задача сдвинулась относительно базы
    и потянула за собой зависимые работы.

    Считается только после пересчёта: до него сравнивать не с чем.
    """

    shifts = _task_shifts(plan)

    if not shifts:
        return []

    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)

    for blocking, blocked in state.dependencies:
        successors[blocking].append(blocked)
        predecessors[blocked].append(blocking)

    initiative_of = {
        task_id: task.initiative
        for task_id, task in state.tasks.items()
    }
    baseline = _baseline_forecasts(plan, "initiative")
    risks = []

    for task_id, shift in sorted(shifts.items()):
        # Корень каскада: задача, чьи блокирующие сами не сдвинулись.
        if any(blocking in shifts for blocking in predecessors[task_id]):
            continue

        affected = _affected_chain(task_id, successors, shifts)

        if not affected:
            continue

        goals = sorted(
            {
                initiative_of[item]
                for item in [task_id, *affected]
                if item in initiative_of
            }
        )

        risks.append(
            Risk(
                level="yellow",
                kind="cascade_shift",
                subject=task_id,
                text=(
                    f"Задержка {task_id} на {sprints(shift)} сдвигает "
                    f"зависимые задачи: "
                    f"{', '.join(f'{item} (+{shifts[item]})' for item in affected)}. "
                    f"Цели: "
                    f"{'; '.join(_goal_state(plan, state, goal, baseline) for goal in goals)}."
                ),
                details={
                    "shift": shift,
                    "affected": {item: shifts[item] for item in affected},
                    "initiatives": goals,
                },
            )
        )

    return risks


def _affected_chain(
    task_id: str,
    successors: dict[str, list[str]],
    shifts: dict[str, int],
) -> list[str]:
    """
    Сдвинувшиеся задачи ниже по цепочке зависимостей.
    """

    seen: set[str] = set()
    queue = list(successors.get(task_id, []))

    while queue:
        current = queue.pop()

        if current in seen or current not in shifts:
            continue

        seen.add(current)
        queue.extend(successors.get(current, []))

    return sorted(seen)


def _initiative_slip(
    plan: PlanResult,
    state: PlanningState,
) -> list[Risk]:
    """
    Жёлтый алерт: квартальная цель ещё в квартале, но завершается
    позже, чем по исходной базе. Запас до конца квартала сокращается —
    следующая задержка может увести её за квартал.
    """

    task_shifts = _task_shifts(plan)
    risks = []

    for change in plan.changes:
        if change.kind != "initiative":
            continue

        shift = change.shift_vs_baseline
        initiative = plan.initiatives.get(change.id)

        if (
            not shift
            or shift <= 0
            or initiative is None
            or not initiative.done_in_quarter
        ):
            continue

        slack = state.quarter - change.current
        members = sorted(
            task_id
            for task_id, task in state.tasks.items()
            if task.initiative == change.id and task_id in task_shifts
        )
        moved = [f"{task_id} (+{task_shifts[task_id]})" for task_id in members]

        risks.append(
            Risk(
                level="yellow",
                kind="initiative_slip",
                subject=change.id,
                text=(
                    f"Цель {change.id} сдвинулась: завершение С{change.baseline} → "
                    f"С{change.current}, "
                    + (
                        "запаса до конца квартала не осталось."
                        if slack <= 0
                        else f"до конца квартала остаётся {sprints(slack)}."
                    )
                    + (f" Сдвинулись её задачи: {_listing(moved)}." if moved else "")
                ),
                details={
                    "baseline_sprint": change.baseline,
                    "forecast_sprint": change.current,
                    "shift": shift,
                    "slack_sprints": slack,
                    "tasks": members,
                },
            )
        )

    return sorted(risks, key=lambda risk: risk.details["slack_sprints"])


def _next_sprint(state: PlanningState) -> int | None:
    sprint = state.current_sprint + 1

    return sprint if sprint <= state.horizon else None


def _is_full(planned: float, fund: float) -> bool:
    free = fund - planned

    return free <= max(FULL_TOLERANCE_HOURS, FULL_TOLERANCE_SHARE * fund)


def _waiting_tasks(
    plan: PlanResult,
    state: PlanningState,
    ready_tasks: list[str],
    role: str,
    sprint: int,
) -> list[str]:
    """
    Готовые задачи роли, которые в спринте не получают всю свою работу,
    по убыванию приоритета.
    """

    waiting = []

    for task_id in ready_tasks:
        remaining = state.tasks[task_id].remaining_hours.get(role, 0.0)
        planned = (
            plan.tasks[task_id].hours_by_sprint.get(sprint, {}).get(role, 0.0)
            if task_id in plan.tasks
            else 0.0
        )

        if remaining - planned > EPS:
            waiting.append(task_id)

    return sorted(waiting, key=lambda task_id: (-state.tasks[task_id].rung, task_id))


def _ready_tasks(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
    sprint: int,
) -> list[str]:
    """
    Задачи, которые по правилу зазора можно вести в спринте:
    то же правило, что у спроса на следующий спринт в плане.
    """

    ready = []

    for task_id, task_plan in plan.tasks.items():
        if task_plan.status == "cancelled":
            continue

        if task_plan.forecast_sprint is not None and task_plan.forecast_sprint < sprint:
            continue

        allowed = True

        for blocking in state.predecessors(task_id):
            finish = (
                None
                if blocking in state.done_tasks or blocking not in plan.tasks
                else plan.tasks[blocking].forecast_sprint
            )
            ready_from = blocker_ready_from(state, solve, blocking, finish)

            if ready_from is None or sprint < ready_from:
                allowed = False
                break

        if allowed:
            ready.append(task_id)

    return ready


def _role_deficit(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
) -> list[Risk]:
    """
    Оранжевый алерт: дефицит роли на следующий спринт.

    Готовая к работе задача, которая в следующем спринте получает меньше
    часов роли, чем ей осталось, ждёт — это нормально, если роль свободна
    (задачу сдерживает что-то другое). Дефицит — когда роль в следующем
    спринте занята полностью, а готовая работа всё равно ждёт; или когда
    носители роли в этом спринте недоступны.
    """

    if not plan.next_sprint_demand:
        return []

    sprint = plan.next_sprint_demand[0].sprint
    # Фонд до конца квартала складывается по спринтам: при фонде,
    # который меняется от спринта к спринту, «фонд × число спринтов» неверен.
    window = range(sprint, state.quarter + 1)
    sprints_left = len(window)
    ready_tasks = _ready_tasks(plan, state, solve, sprint)
    rows = []

    if solve.role_pool == "team":
        for row in plan.next_sprint_demand:
            cell = plan.load.team_role_hours.get(row.team, {}).get(row.role, {}).get(sprint)
            planned = cell.planned if cell else row.planned_hours
            rows.append(
                (
                    f"{row.team} / {row.role}",
                    row.role,
                    [t for t in ready_tasks if state.tasks[t].team == row.team],
                    row.ready_hours,
                    planned,
                    row.fund_hours,
                    state.window_team_fund(row.team, row.role, window),
                )
            )
    else:
        demand: dict[str, float] = defaultdict(float)

        for row in plan.next_sprint_demand:
            demand[row.role] += row.ready_hours

        for role, ready in sorted(demand.items()):
            cell = plan.load.role_hours.get(role, {}).get(sprint)
            rows.append(
                (
                    role,
                    role,
                    ready_tasks,
                    ready,
                    cell.planned if cell else 0.0,
                    state.global_fund(role, sprint),
                    state.window_global_fund(role, window),
                )
            )

    risks = []

    for subject, role, candidates, ready, planned, fund, quarter_fund in rows:
        if not is_limited(state, solve, role):
            continue

        waiting_hours = ready - planned

        if ready <= EPS or waiting_hours <= FULL_TOLERANCE_HOURS:
            continue

        unavailable = fund <= EPS

        if not unavailable and not _is_full(planned, fund):
            continue

        waiting = _waiting_tasks(plan, state, candidates, role, sprint)
        goals = sorted({state.tasks[task_id].initiative for task_id in waiting})

        if unavailable:
            head = (
                f"Роль «{role}» в спринте {sprint} недоступна: все её носители "
                f"отсутствуют, а готовы работы на {ready:.0f} ч."
            )
            queue = None
        else:
            queue = waiting_hours / fund
            head = (
                f"Дефицит роли «{role}» в спринте {sprint}: роль занята полностью "
                f"({planned:.0f} из {fund:.0f} ч), ещё {waiting_hours:.0f} ч готовых "
                f"работ ждут — очередь на {queue:.1f} спринта."
            )

        text = head

        if waiting:
            text += f" Ждут задачи: {_listing(waiting)}."

        if goals:
            text += f" Их цели: {_listing(goals)}."

        over_quarter = ready > quarter_fund + EPS

        if over_quarter:
            text += (
                f" До конца квартала роль столько не отработает: "
                f"осталось {sprints(sprints_left)}, это {quarter_fund:.0f} ч."
            )

        risks.append(
            Risk(
                level="orange",
                kind="role_deficit",
                subject=subject,
                text=text,
                details={
                    "sprint": sprint,
                    "role": role,
                    "ready_hours": ready,
                    "planned_hours": planned,
                    "fund_hours": fund,
                    "gap_hours": waiting_hours,
                    "queue_sprints": queue,
                    "quarter_fund_hours": quarter_fund,
                    "over_quarter": over_quarter,
                    "waiting_tasks": waiting,
                    "initiatives": goals,
                },
            )
        )

    return risks


def _team_gap(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
) -> list[Risk]:
    """
    Оранжевый алерт: командам на следующий спринт не хватает своих
    специалистов, и план берёт часы у других команд.

    Это часы сверх фонда своей команды (заимствования) в ближайшем
    спринте. Один риск на все команды: работа зависит от людей, которых
    ещё никто не согласился отдать. Согласованная помощь переносит часы
    в фонд команды — после неё строка пропадает.
    """

    sprint = _next_sprint(state)

    if solve.role_pool != "team_borrow" or sprint is None:
        return []

    gaps: dict[str, dict[str, float]] = defaultdict(dict)

    for row in plan.borrowing:
        if row.sprint == sprint and row.hours > FULL_TOLERANCE_HOURS:
            gaps[row.team][row.role] = gaps[row.team].get(row.role, 0.0) + row.hours

    if not gaps:
        return []

    parts = []
    tasks: dict[str, list[str]] = {}

    for team, roles in sorted(gaps.items(), key=lambda item: -sum(item[1].values())):
        tasks[team] = sorted(
            task_id
            for task_id, task_plan in plan.tasks.items()
            if task_plan.team == team
            and any(
                hours > EPS and role in roles
                for role, hours in task_plan.hours_by_sprint.get(sprint, {}).items()
            )
        )
        roles_text = ", ".join(
            f"{role} {hours:.0f} ч"
            for role, hours in sorted(roles.items(), key=lambda item: -item[1])
        )
        parts.append(f"{team} — {roles_text} ({_listing(tasks[team], 3)})")

    total = sum(sum(roles.values()) for roles in gaps.values())

    return [
        Risk(
            level="orange",
            kind="team_gap",
            subject="Помощь между командами",
            text=(
                f"В спринте {sprint} работам команд не хватает своих специалистов, "
                f"план берёт {total:.0f} ч у других команд: {'; '.join(parts)}. "
                f"Пока помощь не согласована, эти часы держатся на договорённости "
                f"с чужими главами."
            ),
            details={
                "sprint": sprint,
                "teams": {team: dict(roles) for team, roles in gaps.items()},
                "tasks": tasks,
                "gap_hours": total,
            },
        )
    ]


def _role_missing(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
) -> list[Risk]:
    """
    Оранжевый алерт: роли без единого носителя.

    Их часы план не ограничивает — работа выполняется вне штата.
    Один риск на все такие роли: часы в следующем спринте и до конца квартала.
    """

    sprint = _next_sprint(state)

    if sprint is None:
        return []

    roles: dict[str, dict[str, float]] = {}

    for role in sorted(state.missing_roles):
        if is_limited(state, solve, role):
            continue

        next_hours = sum(
            task_plan.hours_by_sprint.get(sprint, {}).get(role, 0.0)
            for task_plan in plan.tasks.values()
        )
        quarter_hours = sum(
            hours.get(role, 0.0)
            for task_plan in plan.tasks.values()
            for s, hours in task_plan.hours_by_sprint.items()
            if sprint <= s <= state.quarter
        )
        ready = sum(
            row.ready_hours
            for row in plan.next_sprint_demand
            if row.role == role
        )

        if next_hours > EPS or quarter_hours > EPS or ready > EPS:
            roles[role] = {
                "sprint_hours": next_hours,
                "quarter_hours": quarter_hours,
                "ready_hours": ready,
            }

    if not roles:
        return []

    parts = [
        f"«{role}» — {row['quarter_hours']:.0f} ч до конца квартала"
        + (f", из них {row['sprint_hours']:.0f} ч в спринте {sprint}" if row["sprint_hours"] > EPS else "")
        for role, row in sorted(roles.items(), key=lambda item: -item[1]["quarter_hours"])
    ]
    total = sum(row["quarter_hours"] for row in roles.values())

    return [
        Risk(
            level="orange",
            kind="role_missing",
            subject="Роли вне штата",
            text=(
                f"Ролей нет ни у одного инженера: {'; '.join(parts)}. "
                f"План считает эту работу выполненной внешними исполнителями — "
                f"без них задачи с этими ролями не закрыть."
            ),
            details={
                "sprint": sprint,
                "roles": roles,
                "quarter_hours": total,
            },
        )
    ]


def summarize(risks: list[Risk]) -> dict[str, int]:
    """
    Сколько рисков каждого уровня.
    """

    counts = dict.fromkeys(LEVELS, 0)

    for risk in risks:
        counts[risk.level] += 1

    return counts
