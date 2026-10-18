"""Перевод результата ядра src/ в формат плана ОРБИТЫ.

Ядро считает доли задачи по спринтам и часы по ролям, но не знает
людей. Здесь доли превращаются в фрагменты интерфейса, а часы роли
раскладываются на конкретных инженеров потоком Эдмондса — Карпа.

Неудача раскладки не отменяет план: она попадает в validation
и в диагностику, потому что план по ролям остаётся верным,
а нераспределённые часы — это риск, который надо показать.
"""

import base64
import hashlib
import json
import logging
import threading
import zlib
from collections import OrderedDict, defaultdict
from dataclasses import asdict, replace

from src.planner import SolveConfig, plan as core_plan
from src.planner.replan import plan_changes, rescale_previous
from src.planner.result import PlanResult
from src.planner.rules import is_limited
from src.planner.validate import find_violations
from src.metrics import quarter_performance, sprint_performance
from src.planner.facts import SprintFact
from src.risks import find_risks, summarize
from src.store import make_baseline

from .bridge import core_state, role_conditions, task_role_keys, _qualifies
from .engine import PLANNER, capacities, people_of, snapshot, teams_of, velocity
from .flow import Network
from .importer import issue

logger = logging.getLogger(__name__)

EPS = 1e-6

# Статус ядра → статус, который понимает интерфейс.
BEYOND_HORIZON = "blocked"

# Правила решения для ОРБИТЫ: config/planner.yaml, как у консольного
# расчёта, с двумя отличиями для работы в интерфейсе.
#
# role_pool=team_borrow: специалисты организации общие, а часы, взятые
# командой сверх своего фонда, попадают в таблицу заимствований — из неё
# и вырастает заявка на помощь. Замыкание команд на себя отменяет
# 31 задачу из 37: шесть ролей есть не в каждой команде.
#
# Контрфакты («что если включить эту задачу») выключены: на официальном
# датасете они удваивают время расчёта, 10 с против 23 с, ради четырёх
# дополнительных фраз. Разбор причин включения, переноса и отмены даёт
# сам разбор плана, он остаётся на месте.
#
# early_finish: задачи квартала завершаются как можно раньше. Без него
# 23 задачи из 34 заканчиваются в шестом спринте — план выглядит так,
# будто всё делается в последний момент, и у зависимых работ нет запаса
# на просрочку. С ним в шестом спринте остаётся одна задача, а 22 работы
# из 34 укладываются в один спринт. Цена — 6 с против 20 с; повторный
# расчёт тех же условий берётся из кэша за сотые доли секунды.
SOLVE = replace(
    PLANNER.solve,
    counterfactuals=False,
    early_finish=True,
)


def _entry_status(task_plan, quarter):
    """
    Статус задачи для интерфейса.

    included — завершается в квартале;
    postponed — завершается позже квартала либо не завершается вовсе;
    cancelled — невозможна при любом плане.
    """

    if task_plan.status == "cancelled":
        return BEYOND_HORIZON

    if task_plan.status == "included":
        return "planned"

    if task_plan.forecast_sprint is not None:
        return "beyond"

    # Работа начата, но в горизонт не укладывается.
    return "partial" if task_plan.progress else BEYOND_HORIZON


def _reasons(task_plan, borrowed=None):
    """
    Причины решения простым языком, вместе с контрфактом
    и исполнителями из других команд.
    """

    reasons = []

    if task_plan.reason_text:
        reasons.append(task_plan.reason_text)

    counterfactual = task_plan.counterfactual or {}
    text = counterfactual.get("text")

    if text:
        reasons.append(text)

    if borrowed:
        reasons.append(borrowed)

    if not reasons:
        reasons.append("Работа помещается в квартал по текущим условиям")

    return reasons


# Решение по задаче для интерфейса: что делать с работой в квартале.
DECISIONS = {
    "include": "В квартал",
    "groundwork": "Задел: цель в квартале не завершится",
    "postpone": "Перенести на следующий квартал",
    "cancel": "Отменить или пересмотреть",
}


def _borrowed_text(task, fragments, people, transfers):
    """
    Кто из других команд работает над задачей: «ENG-401 (Team-L) — Разработчик Java 42 ч».
    Помощь, согласованная главами, помечается отдельно.
    """

    hours = defaultdict(float)
    agreed = set()

    for fragment in fragments:
        for row in fragment["assignments"]:
            person = people.get(row["engineer"])

            if person is None or person["allocations"].get(task["team"], 0) > 0:
                continue

            hours[row["engineer"], row["role"]] += row["hours"]

            if any(
                t["engineer"] == row["engineer"] and t["to_team"] == task["team"]
                and t["start"] <= fragment["sprint"] <= t["end"]
                for t in transfers
            ):
                agreed.add(row["engineer"])

    if not hours:
        return None

    parts = []

    for (engineer, role), value in sorted(hours.items(), key=lambda item: -item[1]):
        teams = ", ".join(sorted(people[engineer]["allocations"]))
        note = ", согласованная помощь" if engineer in agreed else ""
        parts.append(f"{engineer} ({teams}{note}) — {role} {value:.0f} ч")

    return "Часы других команд: " + "; ".join(parts) + "."


def _assign_people(state, result, data, config, tasks, cutoff, solve):
    """
    Часы ролей по спринтам → конкретные инженеры.

    Идём по спринтам, внутри спринта — по задачам в порядке начала
    работы и приоритета. Остаток часов человека общий: он не может
    быть занят дважды в одном спринте.

    Часы, взятые сверх фонда своей команды, раскладываются на
    инженеров других команд — это и есть заимствование из плана.
    """

    conditions = role_conditions(tasks)
    people = {person["id"]: person for person in people_of(data, config)}
    assignments = defaultdict(list)
    shortfalls = []
    external = defaultdict(float)

    def holders(key):
        role, skills, engineer = conditions.get(key, (key, (), None))

        return sorted(
            person_id
            for person_id, person in people.items()
            if _qualifies(person, role, skills, engineer)
        )

    eligible_by_role = {key: holders(key) for key in conditions}

    def tightness(task_id):
        """
        Насколько узок круг исполнителей самой требовательной роли задачи.
        """

        counts = [
            len(eligible_by_role.get(key, ()))
            for key in state.tasks[task_id].remaining_hours
        ]

        return min(counts) if counts else 0

    # Сначала работы с самым узким кругом исполнителей. Инженер, жёстко
    # назначенный на задачу, годится и для обычной роли других задач:
    # без этого порядка они разберут его часы первыми, и назначенная
    # работа останется без исполнителя.
    order = sorted(
        result.tasks,
        key=lambda task_id: (
            tightness(task_id),
            result.tasks[task_id].sprint_start or 0,
            -state.tasks[task_id].rung,
            task_id,
        ),
    )

    # Кто вёл работу роли в задаче в прошлом спринте: ему она достаётся
    # первой, чтобы исполнитель не менялся от спринта к спринту.
    previous_people = defaultdict(set)

    for sprint in range(cutoff + 1, state.horizon + 1):
        global_hours, _ = capacities(data, config, sprint)
        # Одна сеть на спринт: работы добавляются по порядку, и поздняя
        # может перетянуть человека у ранней, если та обойдётся другими.
        network = Network(global_hours)
        needed = {}
        # Своя команда задачи — люди с долей в ней и те, кто пришёл
        # по согласованной помощи на этот спринт. Чужие подключаются
        # только к нехватке: иначе работу команды делали бы люди
        # других команд, а свои простаивали.
        home = defaultdict(set)

        for person in people.values():
            for team in person["allocations"]:
                home[team].add(person["id"])

        for transfer in config["transfers"]:
            if transfer["start"] <= sprint <= transfer["end"]:
                home[transfer["to_team"]].add(transfer["engineer"])

        for task_id in order:
            planned = result.tasks[task_id].hours_by_sprint.get(sprint)

            if not planned:
                continue

            # Роли без единого носителя выполняются вне штата: ядро
            # их не ограничивает, и раскладывать на людей нечего.
            for key, hours in sorted(planned.items()):
                if not is_limited(state, solve, key):
                    base, _, _ = conditions.get(key, (key, (), None))
                    external[base] += hours
                    continue

                network.add(
                    (task_id, key),
                    hours,
                    eligible_by_role.get(key) or holders(key),
                    home[tasks[task_id]["team"]],
                    previous_people.get((task_id, key)),
                )
                needed[task_id] = needed.get(task_id, 0.0) + hours

        network.widen(1)
        network.widen()
        placed = defaultdict(float)
        current_people = defaultdict(set)

        for row in network.rows():
            task_id, key = row["role"]
            base, _, _ = conditions.get(key, (key, (), None))
            placed[task_id] += row["hours"]
            current_people[(task_id, key)].add(row["engineer"])
            assignments[(task_id, sprint)].append(
                {
                    "engineer": row["engineer"],
                    "role": base,
                    "hours": row["hours"],
                }
            )

        for task_id, hours in needed.items():
            if placed[task_id] < hours - 1e-7:
                shortfalls.append(
                    f"{task_id}, спринт {sprint}: на людей разложено "
                    f"{placed[task_id]:.1f} ч из {hours:.1f} ч"
                )

        previous_people.update(current_people)

    return assignments, shortfalls, dict(sorted(external.items()))


def _entries(state, result, tasks, assignments, quarter, people=None, transfers=()):
    """
    Записи плана по каждой задаче, включая уже закрытые.
    """

    people = people or {}
    entries = {}

    for task_id, task in sorted(tasks.items()):
        if task["status"] == "Done":
            entries[task_id] = {
                "id": task_id,
                "start": None,
                "end": task["completed_sprint"],
                "status": "done",
                "fragments": [],
                "waits": [],
                "reasons": [
                    "Завершение подтверждено фактом"
                    if task["completed_sprint"]
                    else "Завершено в исходных данных до планируемого периода"
                ],
            }
            continue

        task_plan = result.tasks.get(task_id)

        if task_plan is None:
            # Задача без остатка часов и очков в модель не попадает.
            entries[task_id] = {
                "id": task_id,
                "start": None,
                "end": None,
                "status": BEYOND_HORIZON,
                "fragments": [],
                "waits": [],
                "reasons": [
                    "Подтвердите положительный остаток SP и часов: "
                    "исчерпанная оценка не означает завершение"
                ],
            }
            continue

        fragments = [
            {
                "sprint": sprint,
                "fraction": fraction,
                "sp": task_plan.sp_by_sprint.get(sprint, 0.0),
                "assignments": assignments.get((task_id, sprint), []),
            }
            for sprint, fraction in sorted(task_plan.progress.items())
        ]

        entries[task_id] = {
            "id": task_id,
            "start": task_plan.sprint_start,
            "end": task_plan.forecast_sprint,
            "status": _entry_status(task_plan, quarter),
            "fragments": fragments,
            "waits": [],
            "decision": task_plan.details.get("decision"),
            "reasons": _reasons(task_plan, _borrowed_text(task, fragments, people, transfers)),
        }

    return entries


def _initiatives(state, result, tasks, quarter):
    """
    Итоги по инициативам в форме интерфейса.
    """

    rows = []

    for initiative_id, initiative in sorted(result.initiatives.items()):
        members = [
            task_id
            for task_id, task in tasks.items()
            if str(task["initiative"]) == initiative_id
        ]

        actual_done = bool(members) and all(
            tasks[task_id]["status"] == "Done" for task_id in members
        )

        if initiative.done_in_quarter:
            risk = "ok"
        elif initiative.forecast_sprint is None:
            risk = "unknown"
        else:
            risk = "beyond"

        rows.append(
            {
                "id": initiative_id,
                "title": tasks[members[0]]["title"] if members else initiative_id,
                "task_ids": sorted(members),
                "end": initiative.forecast_sprint,
                "actual_done": actual_done,
                "forecast_in_quarter": initiative.done_in_quarter,
                "risk": risk,
                "reason": initiative.reason_text or "",
            }
        )

    return rows


def _usage(data, config, resources, entries, tasks, cutoff, horizon):
    """
    Загрузка команд в очках и людей в часах по спринтам.
    """

    rows = []

    for sprint in range(cutoff + 1, horizon + 1):
        global_hours, _ = capacities(data, config, sprint)
        team_sp = defaultdict(float)
        person_hours = defaultdict(float)

        for task_id, entry in entries.items():
            for fragment in entry["fragments"]:
                if fragment["sprint"] != sprint:
                    continue

                team_sp[tasks[task_id]["team"]] += fragment["sp"]

                for row in fragment["assignments"]:
                    person_hours[row["engineer"]] += row["hours"]

        rows.append(
            {
                "sprint": sprint,
                "teams": {
                    team: {
                        "used": team_sp.get(team, 0.0),
                        "capacity": row["capacity"],
                    }
                    for team, row in resources.items()
                },
                "people": {
                    person_id: {
                        "used": person_hours.get(person_id, 0.0),
                        "capacity": hours,
                    }
                    for person_id, hours in global_hours.items()
                },
            }
        )

    return rows


def orbit_plan(state, result, data, config, actuals=None, previous=None, solve=None):
    """
    План ядра в формате ОРБИТЫ.

    Нарушения независимой проверки возвращаются в validation,
    а не выбрасываются: сервер должен показать план с пометкой,
    а не упасть на старте.
    """

    actuals = actuals or []

    if solve is None:
        solve = SolveConfig(**result.meta.solve_config)

    cutoff = max((report["sprint"] for report in actuals), default=0)
    tasks = snapshot(data, config, actuals)
    resources = velocity(data, config, actuals)

    assignments, shortfalls, external = _assign_people(
        state,
        result,
        data,
        config,
        tasks,
        cutoff,
        solve,
    )

    people = {person["id"]: person for person in people_of(data, config)}
    entries = _entries(state, result, tasks, assignments, state.quarter, people, config["transfers"])

    diagnostics = [
        issue("core_warning", text.split(":")[0], text)
        for text in list(state.warnings) + list(result.meta.warnings)
    ]

    diagnostics += [
        issue(
            "unassigned_hours",
            text.split(",")[0],
            text,
            action="Проверьте доступность и компетенции инженеров",
        )
        for text in shortfalls
    ]

    diagnostics += [
        issue(
            "external_role",
            role,
            f"Роль «{role}»: {hours:.0f} ч за горизонт выполняются вне "
            f"штата — в профилях инженеров её нет ни у кого",
            action="Наймите специалиста или подтвердите роль сотруднику",
        )
        for role, hours in external.items()
    ]

    plan = {
        "entries": entries,
        "resources": resources,
        "usage": _usage(
            data,
            config,
            resources,
            entries,
            tasks,
            cutoff,
            config["horizon"],
        ),
        "initiatives": _initiatives(state, result, tasks, state.quarter),
        "cutoff": cutoff,
        "tasks": tasks,
        "diagnostics": diagnostics,
        "edges": [list(edge) for edge in state.dependencies],
        "frozen": []
        if previous is None
        else previous.get("frozen", [])
        + [
            dict(task_id=task_id, team=previous["tasks"][task_id]["team"], **fragment)
            for task_id, entry in previous["entries"].items()
            for fragment in entry["fragments"]
            if fragment["sprint"] <= cutoff
        ],
        # Время расчёта в план не кладём: план целиком уходит
        # в отпечаток состояния, по которому собираются согласования
        # глав, и плавающее число сбрасывало бы их голоса.
        "engine": {
            "status": result.meta.status,
            "solver": result.meta.solver,
        },
    }

    plan["validation"] = find_violations(result, state, solve) + shortfalls

    return plan


# Расчёт ядра по одним и тем же условиям даёт один и тот же результат,
# поэтому его можно не повторять. Сборка плана и раскладка людей
# остаются за кэшем: они занимают сотые доли секунды и зависят
# от предыдущего плана, который у каждого вызова свой.
_CACHE = OrderedDict()
_CACHE_LIMIT = 16
_CACHE_LOCK = threading.Lock()


def conditions_key(data, config, actuals, solve, previous=None):
    """
    Отпечаток условий расчёта: данные, настройки, факты, правила
    и доли предыдущего плана, если включена стабильность.
    """

    payload = json.dumps(
        [data, config, actuals or [], solve.__dict__, previous],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_core(data, config, actuals, solve, previous=None):
    """
    Состояние и решение ядра, посчитанные один раз на набор условий.

    previous — план ядра до факта спринта. С ним включается уровень
    стабильности: оставшаяся работа раскладывается как можно ближе
    к прежнему плану, как в src/planner/replan.py.
    """

    progress = None if previous is None else {
        task_id: task.progress
        for task_id, task in sorted(previous.tasks.items())
        if task.progress
    }
    key = conditions_key(data, config, actuals, solve, progress)

    with _CACHE_LOCK:
        found = _CACHE.get(key)

        if found is not None:
            _CACHE.move_to_end(key)

            return found

    state = core_state(data, config, actuals)
    previous_progress = (
        None if previous is None
        else rescale_previous(previous, state, state) or None
    )
    result = core_plan(state, solve, previous_progress=previous_progress)

    with _CACHE_LOCK:
        _CACHE[key] = (state, result)
        _CACHE.move_to_end(key)

        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)

    return state, result


def _engine_row(result):
    objective = result.meta.objective

    return dict(
        engine=result.meta.engine,
        status=result.meta.status,
        initiatives_done=int(objective.get("initiatives_done", result.initiatives_done)),
        tasks_included=int(objective.get("tasks_included", len(result.included_task_ids))),
        borrowed_hours=float(objective.get("borrowed_hours", 0.0)),
    )


def _engines(state, result, solve):
    """
    Сравнение с жадным планом по тем же условиям (src/planner/compare.py).

    Жадный план считается за доли секунды и служит нижней границей:
    точный не может завершить меньше инициатив.
    """

    try:
        greedy = core_plan(state, replace(solve, engine="greedy"))
    except Exception as error:  # noqa: BLE001
        logger.warning("Жадный план не рассчитан: %s", error)
        return None

    return dict(exact=_engine_row(result), greedy=_engine_row(greedy))


def explain_plan(data, config, actuals):
    """
    Подробный разбор с контрфактами: что будет, если принудительно
    включить отложенную задачу или спасти инициативу.

    Считается по запросу: контрфакты — повторные решения модели,
    это ещё 20–40 секунд. Результат кэшируется вместе с планом.
    """

    solve = replace(SOLVE, counterfactuals=True)
    _, result = cached_core(data, config, actuals, solve)

    # Вывод контрфакта ядро уже вписало в причину решения; отдельно
    # отдаём, чем он кончился и какие инициативы стоит включение.
    def outcome(item):
        found = item.counterfactual or {}
        return dict(
            status=found.get("status"),
            lost=[row["id"] for row in found.get("lost", [])],
            gained=list(found.get("gained", [])),
        )

    return dict(
        tasks=[
            dict(id=task_id, status=task.status, reason=task.reason_text, counterfactual=outcome(task))
            for task_id, task in sorted(result.tasks.items())
            if task.counterfactual
        ],
        initiatives=[
            dict(id=initiative_id, done=item.done_in_quarter, reason=item.reason_text, counterfactual=outcome(item))
            for initiative_id, item in sorted(result.initiatives.items())
            if item.counterfactual or not item.done_in_quarter
        ],
    )


# Стресс-сценарии: тот же вход, другие допущения. Показывают, что
# план сделает, если ресурса станет меньше, — какие цели он сохранит,
# какие работы перенесёт или предложит отменить и почему.
SCENARIOS = (
    ("capacity", "Ёмкость команд на 20 % ниже",
     "Средняя производительность × 0,8 × 0,8: запас на просадку команд."),
    ("no_external", "Роли вне штата не закрыты",
     "Работы ролей, которых нет ни у одного инженера, выполнить некому — такие задачи отменяются."),
    ("no_borrowing", "Без помощи между командами",
     "Каждая команда работает только своими людьми."),
)


def _scenario_input(key, config):
    solve = replace(SOLVE, counterfactuals=False)

    if key == "capacity":
        return dict(config, safety=config["safety"] * 0.8), solve

    if key == "no_external":
        return config, replace(solve, missing_roles="block")

    return config, replace(solve, role_pool="team")


def stress_scenarios(data, config, actuals, plan=None):
    """
    Стресс-сценарии по текущим условиям: сколько целей план сохраняет
    и что предлагает перенести или отменить. План проекта не меняется.

    plan — текущий план ОРБИТЫ: с ним сравниваются цели сценариев.
    """

    current = unpack_core(plan)

    if current is None:
        _, current = cached_core(data, config, actuals, SOLVE)

    base = {i for i, item in current.initiatives.items() if item.done_in_quarter}
    rows = []

    for key, title, note in SCENARIOS:
        scenario_config, solve = _scenario_input(key, config)

        try:
            _, result = cached_core(data, scenario_config, actuals, solve)
        except Exception as error:  # noqa: BLE001 — сценарий не должен ронять ответ
            rows.append(dict(key=key, title=title, note=note, error=str(error)))
            continue

        done = {i for i, item in result.initiatives.items() if item.done_in_quarter}
        rows.append(dict(
            key=key, title=title, note=note,
            initiatives_done=len(done), initiatives_total=len(result.initiatives),
            tasks_included=len(result.included_task_ids), tasks_total=len(result.tasks),
            lost=sorted(base - done), gained=sorted(done - base),
            initiatives=[
                dict(id=i, rung=item.rung, forecast=item.forecast_sprint, reason=item.reason_text or "")
                for i, item in sorted(result.initiatives.items(), key=lambda pair: (-pair[1].rung, pair[0]))
                if not item.done_in_quarter
            ],
            tasks=[
                dict(id=t, status=item.status, decision=item.details.get("decision"),
                     forecast=item.forecast_sprint, reason=item.reason_text or "")
                for t, item in sorted(result.tasks.items())
                if item.status != "included"
            ],
        ))

    return dict(base_done=len(base), scenarios=rows)


def pack_core(result):
    """
    План ядра для хранения в версии ОРБИТЫ: сжатый JSON.

    Время расчёта обнуляется: план целиком входит в отпечаток,
    по которому собираются согласия глав, и плавающее число
    сбрасывало бы их голоса при повторном расчёте тех же условий.
    """

    stable = replace(result, meta=replace(result.meta, elapsed_seconds=0.0))
    raw = zlib.compress(stable.to_json().encode("utf-8"), 9)

    return base64.b64encode(raw).decode("ascii")


def unpack_core(plan):
    """
    План ядра из версии ОРБИТЫ; None для старых версий без него.
    """

    packed = (plan or {}).get("core")

    if not packed:
        return None

    return PlanResult.from_json(zlib.decompress(base64.b64decode(packed)).decode("utf-8"))


def _analysis(result, state):
    """
    Разбор плана ядра для интерфейса: узкие места, заимствования,
    спрос готовых работ на следующий спринт.
    """

    demand = defaultdict(lambda: dict(ready_hours=0.0, planned_hours=0.0, fund_hours=0.0))
    sprint = None

    for row in result.next_sprint_demand:
        sprint = row.sprint
        cell = demand[row.role]
        cell["ready_hours"] += row.ready_hours
        cell["planned_hours"] += row.planned_hours

    for role, cell in demand.items():
        cell["fund_hours"] = state.global_fund(role, sprint) if sprint else 0.0

    borrowing = defaultdict(float)

    for row in result.borrowing:
        borrowing[row.team, row.role] += row.hours

    return {
        "bottlenecks": [asdict(row) for row in result.load.bottlenecks],
        "borrowing": [
            dict(team=team, role=role, hours=hours)
            for (team, role), hours in sorted(borrowing.items())
        ],
        "demand": [
            dict(sprint=sprint, role=role, **cell)
            for role, cell in sorted(demand.items())
        ],
        "objective": dict(result.meta.objective),
    }


def _failed_plan(data, config, actuals, previous, error):
    """
    План-заглушка, когда расчёт не удался.

    Сервер обязан остаться на ногах и показать причину: исключение
    на старте роняло процесс, а неудача расчёта — это состояние
    данных, а не отказ программы.
    """

    actuals = actuals or []
    cutoff = max((report["sprint"] for report in actuals), default=0)
    tasks = snapshot(data, config, actuals)
    resources = velocity(data, config, actuals)
    text = f"План не рассчитан: {error}"

    entries = {
        task_id: {
            "id": task_id,
            "start": None,
            "end": task["completed_sprint"] if task["status"] == "Done" else None,
            "status": "done" if task["status"] == "Done" else BEYOND_HORIZON,
            "fragments": [],
            "waits": [],
            "reasons": [text],
        }
        for task_id, task in sorted(tasks.items())
    }

    return {
        "entries": entries,
        "resources": resources,
        "usage": [],
        "initiatives": [],
        "cutoff": cutoff,
        "tasks": tasks,
        "diagnostics": [
            issue(
                "cycle" if "Цикл" in str(error) else "engine_failure",
                str(error).split(":")[-1].strip()
                if "Цикл" in str(error)
                else "Планировщик",
                text,
                "error",
                "Исправьте направление или состав связей"
                if "Цикл" in str(error)
                else "Проверьте связи задач, роли и доступность людей",
            )
        ],
        "edges": [],
        "frozen": [] if previous is None else previous.get("frozen", []),
        "engine": {"status": "failed", "solver": None},
        "validation": [text],
        "risks": [],
        "risk_summary": {},
        "analysis": {},
    }


def build_plan(data, config, actuals=None, previous=None, solve=None, baseline=None):
    """
    Расчёт плана ядром src/ в формате ОРБИТЫ.

    previous — прежний план ОРБИТЫ. Если с него закрылся спринт или база
    уже зафиксирована, включается стабильность ядра. baseline — зафиксированная база:
    с ней план получает изменения относительно базы, а риски — жёлтый
    алерт о каскадном сдвиге.

    Исключений не бросает: неудача возвращается планом с непустым
    validation, иначе сервер падает на старте.
    """

    solve = solve or SOLVE
    cutoff = max((report["sprint"] for report in actuals or []), default=0)
    previous_core = unpack_core(previous)
    # Стабильность — после закрытия спринта и после фиксации базы: план,
    # от которого считаются показатели, не должен перетасовываться из-за
    # отпуска или согласованной помощи сильнее, чем нужно. До фиксации
    # базы план каждый раз считается заново.
    replanned = previous_core is not None and (
        previous.get("cutoff", 0) < cutoff or bool(baseline)
    )

    try:
        state, result = cached_core(
            data, config, actuals, solve,
            previous_core if replanned else None,
        )
        baseline_core = unpack_core((baseline or {}).get("plan"))

        if baseline_core is not None:
            result = replace(
                result,
                changes=plan_changes(
                    baseline_core,
                    previous_core or baseline_core,
                    result,
                    state,
                ),
            )
    except Exception as error:  # noqa: BLE001 — причина уходит в интерфейс
        logger.warning("Расчёт не удался: %s", error)

        return _failed_plan(data, config, actuals, previous, error)

    plan = orbit_plan(state, result, data, config, actuals, previous, solve)

    # Риски — надстройка над готовым планом: их сбой не отменяет план.
    try:
        risks = find_risks(result, state, solve)
    except Exception as error:  # noqa: BLE001
        logger.warning("Риски не рассчитаны: %s", error)
        risks = []
        plan["diagnostics"].append(issue(
            "risks_failed", "Риски", f"Риски плана не рассчитаны: {error}", "error",
            "Сообщите разработчикам: план верен, но алерты не показаны",
        ))
    plan["core"] = pack_core(result)
    plan["risks"] = [asdict(risk) for risk in risks]
    plan["risk_summary"] = summarize(risks)
    plan["analysis"] = _analysis(result, state)
    plan["analysis"]["engines"] = _engines(state, result, solve)
    plan["engine"]["stability"] = replanned

    return plan


def core_baseline(plan):
    """
    База сравнения ядра из плана ОРБИТЫ; None для старых версий.
    """

    core = unpack_core(plan)

    return None if core is None else make_baseline(core)


def _sprint_fact(report, baseline_state, role_keys=None):
    """
    Факт спринта ОРБИТЫ в форме ядра: потраченные часы и закрытые задачи.

    Задачи, добавленные после фиксации базы, в зачёт не входят:
    плановых SP для них нет, сравнивать не с чем. Часы роли переводятся
    в имя роли базы: у задачи с обязательными навыками или назначенным
    исполнителем роль в ядре называется иначе (bridge.role_key).
    """

    known = baseline_state.tasks
    role_keys = role_keys or {}
    hours = {}

    for outcome in report["tasks"]:
        if outcome["task_id"] not in known:
            continue

        keys = role_keys.get(outcome["task_id"], {})

        for role, value in (outcome.get("spent_hours") or {}).items():
            key = (outcome["task_id"], keys.get(role, role))
            hours[key] = hours.get(key, 0.0) + float(value)

    return SprintFact(
        sprint=report["sprint"],
        hours=hours,
        closed=frozenset(
            outcome["task_id"]
            for outcome in report["tasks"]
            if outcome["status"] == "Done" and outcome["task_id"] in known
        ),
    )


_METRICS = OrderedDict()
_METRICS_LOCK = threading.Lock()


def quarter_metrics(project):
    """
    Показатели выполнения по правилам ядра (src/metrics.py).

    Квартал: доля инициатив, которые база обещала завершить, —
    фактически завершённых и завершаемых по текущему прогнозу.
    Спринт: SP, зачтённые по потраченным часам и закрытиям, против
    плановых SP базы. SP, заявленные главами, показываются рядом
    для сверки, в процент не входят.
    """

    sprints = project["config"]["sprints"]
    reports = {report["sprint"]: report for report in project["actuals"]}
    baseline = project.get("baseline")
    rows = [
        dict(sprint=s, planned=0.0, actual=None, percent=None,
             reported=sum(reports[s]["teams"].values()) if s in reports else None, by_team={})
        for s in range(1, sprints + 1)
    ]
    empty = dict(goal_count=0, actual_done=0, forecast_done=0, actual_percent=None,
                 forecast_percent=None, goals=[], done=[], forecast=[], sprints=rows)

    core = core_baseline((baseline or {}).get("plan"))
    current = unpack_core(project["plan"])

    if core is None or current is None:
        return empty

    key = (project.get("project_id"), project.get("revision"), hashlib.sha256(
        json.dumps([baseline.get("revision"), project["actuals"]], sort_keys=True, default=str).encode("utf-8")
    ).hexdigest())

    with _METRICS_LOCK:
        if key in _METRICS:
            return _METRICS[key]

    try:
        base_data = baseline.get("dataset", project["dataset"])
        base_config = baseline.get("config", project["config"])
        baseline_state = core_state(base_data, base_config, [])
        role_keys = {
            task_id: task_role_keys(task)
            for task_id, task in snapshot(base_data, base_config, []).items()
            if task["status"] != "Done"
        }
        facts = [_sprint_fact(r, baseline_state, role_keys)
                 for r in sorted(project["actuals"], key=lambda r: r["sprint"])]
        performance = {row.sprint: row for row in sprint_performance(core, baseline_state, facts)}
        current_state = core_state(project["dataset"], project["config"], project["actuals"])
        quarter = quarter_performance(core, current, current_state)
    except Exception as error:  # noqa: BLE001 — показатели не должны ронять просмотр
        logger.warning("Показатели не рассчитаны: %s", error)
        return empty

    planned = core.planned_sp_by_sprint()

    for row in rows:
        row["planned"] = sum(planned.get(row["sprint"], {}).values())
        found = performance.get(row["sprint"])

        if found is not None:
            row["actual"] = found.done_sp
            row["percent"] = None if found.ratio is None else 100 * found.ratio
            row["by_team"] = {team: dict(planned=p, actual=a) for team, (p, a) in found.by_team.items()}

    count = len(quarter.planned)
    metrics = dict(
        goal_count=count,
        actual_done=len(quarter.done),
        forecast_done=len(quarter.forecast),
        actual_percent=None if quarter.actual_ratio is None else 100 * quarter.actual_ratio,
        forecast_percent=None if quarter.forecast_ratio is None else 100 * quarter.forecast_ratio,
        goals=list(quarter.planned),
        done=list(quarter.done),
        forecast=list(quarter.forecast),
        sprints=rows,
    )

    with _METRICS_LOCK:
        _METRICS[key] = metrics

        while len(_METRICS) > _CACHE_LIMIT:
            _METRICS.popitem(last=False)

    return metrics
