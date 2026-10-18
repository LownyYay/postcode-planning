"""Адаптер состояния ОРБИТЫ во вход расчётного ядра src/.

Ядро считает по ролям и не знает про людей, навыки, персональную
доступность и передачи часов между командами. Всё это выражается
через фонды, а не через новые сущности:

- доступность инженера по спринтам и передачи часов складываются
  в фонд пары «команда, роль» на конкретный спринт;
- требования к технологиям и ручное назначение исполнителя сужают
  круг носителей роли, поэтому дают производную роль со своим фондом;
- один человек, числящийся в нескольких ролях, отдаёт часы в каждый
  фонд, поэтому сверху задаётся потолок часов на спринт.

Обратного преобразования здесь нет: PlanResult в формат ОРБИТЫ
переводит отдельный модуль.
"""

from dataclasses import replace

import pandas as pd

from src.dataset import Dataset
from src.planner import PlanningState, build_state

from .engine import PLANNER, capacities, people_of, snapshot
from .importer import InputError

# Справочники на случай данных без них (демонстрационный набор, старые
# базы). Официальный датасет приносит свои списки из Excel, и ядро
# сверяет с ними код причины каждого решения.
VARIANCE_REASONS = (
    "Не оценивалась",
    "Отсутствие ресурсов",
    "Отсутствует готовность смежных команд",
    "Превышение плана",
)

GOAL_OPTIONS = (
    "не будет взято в квартал",
    "Отменено заказчиком",
)


def role_key(role, skills=(), engineer=None):
    """
    Имя роли для ядра.

    Роль без условий остаётся собой. Требование технологий или ручное
    назначение исполнителя сужают круг носителей, а значит и фонд часов,
    поэтому получают собственное имя. Оно попадает в причины и риски,
    поэтому читается по-человечески.
    """

    conditions = sorted(skills)

    if engineer:
        conditions.append(engineer)

    if not conditions:
        return role

    return f"{role} ({', '.join(conditions)})"


def task_role_keys(task):
    """
    Имена ролей задачи: роль → имя с учётом навыков и назначения.
    """

    required = task.get("required_skills") or {}
    assigned = task.get("manual_assignments") or {}

    return {
        role: role_key(role, required.get(role, ()), assigned.get(role))
        for role in task["remaining_hours"]
    }


def _qualifies(person, role, skills, engineer):
    """
    Может ли человек закрывать эту производную роль.
    """

    if role not in person["roles"]:
        return False

    if engineer and person["id"] != engineer:
        return False

    have = {skill.casefold() for skill in person["skills"]}

    return {skill.casefold() for skill in skills}.issubset(have)


def role_conditions(tasks):
    """
    Все имена ролей, встречающиеся в задачах, с их условиями.

    Возвращает имя роли → (базовая роль, навыки, назначенный инженер).
    """

    conditions = {}

    for task in tasks.values():
        if task["status"] == "Done":
            continue

        required = task.get("required_skills") or {}
        assigned = task.get("manual_assignments") or {}

        for role in task["remaining_hours"]:
            skills = tuple(sorted(required.get(role, ())))
            engineer = assigned.get(role)
            conditions[role_key(role, skills, engineer)] = (
                role,
                skills,
                engineer,
            )

    return conditions


def _tasks_frame(tasks):
    """
    Таблица задач в форме датасета.

    Остаток берётся как полная оценка: в ядре очки задачи в работе
    считаются долей от остатка часов, а ОРБИТА уже применила факт
    и переоценки в snapshot. Подставляя остаток и нулевые затраты,
    получаем sp_effective, равный остатку очков при любых настройках.
    """

    rows = []

    for task_id, task in sorted(tasks.items()):
        rows.append(
            {
                "task_id": task_id,
                "Номер инициативы": str(task["initiative"]),
                "team_id": task["team"],
                "status": task["status"],
                "rung": float(task.get("priority") or 0.0),
                "estimation_sp": (
                    0.0
                    if task["status"] == "Done"
                    else float(task["remaining_sp"])
                ),
            }
        )

    return pd.DataFrame(
        rows,
        columns=[
            "task_id",
            "Номер инициативы",
            "team_id",
            "status",
            "rung",
            "estimation_sp",
        ],
    )


def _estimates_frame(tasks):
    """
    Остаток часов по задачам и производным ролям.
    """

    rows = []

    for task_id, task in sorted(tasks.items()):
        if task["status"] == "Done":
            continue

        keys = task_role_keys(task)

        for role, hours in sorted(task["remaining_hours"].items()):
            if hours > 0:
                rows.append(
                    {
                        "task_id": task_id,
                        "role": keys[role],
                        "estimated_hours": float(hours),
                    }
                )

    return pd.DataFrame(
        rows,
        columns=["task_id", "role", "estimated_hours"],
    )


def _engineers_frame(people, conditions):
    """
    Строки распределения инженеров по производным ролям.

    Одна строка — «инженер, команда, роль». Ставка нужна ядру только
    для базового фонда: реальные часы задаёт sprint_role_fund.
    """

    rows = []

    for person in sorted(people, key=lambda p: p["id"]):
        matched = sorted(
            key
            for key, (role, skills, engineer) in conditions.items()
            if _qualifies(person, role, skills, engineer)
        )

        # Роли, которых нет ни в одной задаче, тоже нужны: без них
        # человек исчезнет из состава команды в звёздной карте.
        matched += [
            role
            for role in sorted(person["roles"])
            if role not in conditions
        ]

        for team, rate in sorted(person["allocations"].items()):
            for key in matched:
                rows.append(
                    {
                        "engineer_id": person["id"],
                        "team_id": team,
                        "role": key,
                        "grade": person.get("grade", ""),
                        "skills_declared": ", ".join(person["skills"]),
                        "capacity_rate": float(rate),
                        "skills": list(person["skills"]),
                    }
                )

    return pd.DataFrame(
        rows,
        columns=[
            "engineer_id",
            "team_id",
            "role",
            "grade",
            "skills_declared",
            "capacity_rate",
            "skills",
        ],
    )


def _history_frame(data, actuals):
    """
    Фактическая производительность команд: исходная история и факты
    закрытых спринтов. Среднее по ней даёт ёмкость команды в очках.
    """

    rows = [
        {
            "team_id": row["team"],
            "velocity_achieved": float(row["achieved"]),
        }
        for row in data["history"]
    ]

    for report in sorted(actuals, key=lambda r: r["sprint"]):
        for team, achieved in sorted(report["teams"].items()):
            rows.append(
                {
                    "team_id": team,
                    "velocity_achieved": float(achieved),
                }
            )

    return pd.DataFrame(
        rows,
        columns=["team_id", "velocity_achieved"],
    )


def _dependencies_frame(data):
    """
    Связи задач. Столбцы читаются как есть: первая задача блокирует
    вторую. На официальном датасете это чтение не даёт ни одного
    противоречия с фактом, а разворот «depends on» даёт.
    """

    rows = [
        {
            "blocking_task_id": dependency["first"],
            "blocked_task_id": dependency["second"],
            "dependency_type": dependency["kind"],
        }
        for dependency in data["dependencies"]
    ]

    return pd.DataFrame(
        rows,
        columns=[
            "blocking_task_id",
            "blocked_task_id",
            "dependency_type",
        ],
    )


def _references(data):
    """
    Справочники датасета; недостающие — из значений по умолчанию.
    """

    references = {key: list(values) for key, values in data.get("references", {}).items()}
    references.setdefault("variance_reasons", list(VARIANCE_REASONS))
    references.setdefault("goal_options", list(GOAL_OPTIONS))

    return references


def core_dataset(data, config, actuals=None):
    """
    Состояние ОРБИТЫ в форме датасета ядра.
    """

    actuals = actuals or []
    tasks = snapshot(data, config, actuals)
    people = people_of(data, config)
    conditions = role_conditions(tasks)

    return Dataset(
        raw=pd.DataFrame(),
        tasks=_tasks_frame(tasks),
        estimates=_estimates_frame(tasks),
        spent_hours=pd.DataFrame(
            columns=["task_id", "role", "spent_hours"],
        ),
        engineers=_engineers_frame(people, conditions),
        team_history=_history_frame(data, actuals),
        dependencies=_dependencies_frame(data),
        role_aliases={},
        reference_lists=_references(data),
    )


def _sprint_funds(data, config, people, conditions, sprints):
    """
    Фонд каждой производной роли по спринтам и потолок часов команды.

    Часы берутся из ОРБИТЫ: capacities учитывает личную доступность,
    доли ставки между командами и согласованные передачи часов.
    """

    funds = {}
    limits = {}
    teams = {team for person in people for team in person["allocations"]}
    by_id = {person["id"]: person for person in people}

    for sprint in sprints:
        global_hours, team_hours = capacities(data, config, sprint)
        available = {}

        for (engineer_id, team), hours in team_hours.items():
            if hours > 0:
                available.setdefault(team, {})[engineer_id] = hours

        # Потолок задаётся для обеих областей пула: движок возьмёт ту,
        # в которой считает фонды, и состояние не зависит от правил решения.
        limits[("*", sprint)] = sum(global_hours.values())

        for team in sorted(teams):
            members = available.get(team, {})
            limits[(team, sprint)] = sum(members.values())

            for key, (role, skills, engineer) in sorted(conditions.items()):
                holders = [
                    hours
                    for engineer_id, hours in members.items()
                    if engineer_id in by_id
                    and _qualifies(by_id[engineer_id], role, skills, engineer)
                ]

                funds[(team, key, sprint)] = sum(holders)

    return funds, limits


def core_state(data, config, actuals=None, current_sprint=None):
    """
    Вход планировщика ядра из состояния ОРБИТЫ.

    Возвращает PlanningState, готовый для plan(state, solve).
    Правила решения сюда не передаются: они живут в SolveConfig.
    """

    actuals = actuals or []
    cutoff = (
        max((report["sprint"] for report in actuals), default=0)
        if current_sprint is None
        else current_sprint
    )

    if cutoff >= config["horizon"]:
        raise InputError("Горизонт прогноза исчерпан: планировать нечего")

    dataset = core_dataset(data, config, actuals)

    state = build_state(
        dataset,
        replace(
            PLANNER.input,
            focus_factor=float(config["safety"]),
            quarter_sprints=int(config["sprints"]),
            horizon_sprints=int(config["horizon"]),
        ),
        current_sprint=cutoff,
    )

    tasks = snapshot(data, config, actuals)
    people = people_of(data, config)
    conditions = role_conditions(tasks)

    funds, limits = _sprint_funds(
        data,
        config,
        people,
        conditions,
        range(cutoff + 1, config["horizon"] + 1),
    )

    not_before = {
        task_id: int(task["not_before"])
        for task_id, task in tasks.items()
        if task["status"] != "Done" and int(task.get("not_before", 1)) > 1
    }

    # Спринт закрытия по факту ОРБИТЫ: ядро по нему сравнивает план
    # с базой и отсчитывает зазор до зависимых задач.
    done_tasks = {
        task_id: int(tasks[task_id].get("completed_sprint") or 0)
        if task_id in tasks else sprint
        for task_id, sprint in state.done_tasks.items()
    }

    return replace(
        state,
        done_tasks=done_tasks,
        sprint_role_fund=funds,
        hours_limit=limits,
        not_before=not_before,
        missing_roles=_missing_roles(state, conditions),
    )


def _missing_roles(state, conditions):
    """
    Роли, которые некому выполнять и которые план не ограничивают.

    Сюда попадает только базовая роль, которой нет ни у одного
    инженера: такая работа выносится наружу и показывается риском.
    Роль, которую сузили требованием технологий или назначением,
    так выносить нельзя — иначе требование перестанет действовать,
    а по условию задачи стек проверяется до назначения человека.
    """

    return frozenset(
        role
        for role in state.missing_roles
        if not conditions.get(role, (role, (), None))[1]
        and not conditions.get(role, (role, (), None))[2]
    )
