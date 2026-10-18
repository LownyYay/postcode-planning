"""Состояние команд: остатки задач, люди, ёмкости, звёздная карта и метрики.

Расчёт плана живёт в present.py и опирается на ядро src/. Здесь
остались общие данные, которыми пользуются и расчёт, и интерфейс.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from statistics import mean

import pandas as pd

from src.planner.config import load_planner_config
from src.star_map import analyze_bus_factor, role_bus_factor
from src.planner.facts import MIN_REMAINING_SHARE, spend_hours

from .changes import diff_plans  # noqa: F401 — общий вход для сервиса
from .importer import InputError

EPS = 1e-6

# Параметры входа — те же, что у консольного расчёта ядра: config/planner.yaml.
PLANNER = load_planner_config()
HOURS_PER_SPRINT = PLANNER.input.hours_per_sprint


def default_config():
    return {"start_date": "2026-10-05", "sprints": PLANNER.input.quarter_sprints,
            "horizon": PLANNER.input.horizon_sprints, "safety": PLANNER.input.focus_factor,
            "availability": {}, "absences": [], "role_overrides": {},
            "task_overrides": {}, "transfers": [], "assignments": {}, "not_before": {}}


def teams_of(data):
    return sorted({t["team"] for t in data["tasks"]} | {h["team"] for h in data["history"]} |
                  {team for p in data["engineers"] for team in p["allocations"]})


def initial_sp(task):
    """
    Очки задачи на начало квартала — то же правило, что в ядре
    (src/planner/state.py, inprogress_sp).

    При inprogress_sp = by_hours задача в работе стоит долю оценки,
    равную доле оставшихся часов: у MOB-7011 выработаны все 165 часов,
    и полные 5 SP списывались бы из ёмкости за работу, которой уже нет.
    """

    if task["status"] == "Done":
        return 0.0
    if task["status"] != "InProgress" or PLANNER.input.inprogress_sp == "full":
        return float(task["sp"])
    total = sum(task["hours"].values())
    if total <= EPS:
        return float(task["sp"])
    return float(task["sp"]) * sum(task["remaining_hours"].values()) / total


def staffed_roles(data, config):
    """Роли, у которых есть хотя бы один носитель в штате."""
    return {r for p in people_of(data, config) for r in p["roles"]}


def snapshot(data, config, actuals):
    tasks = {t["id"]: deepcopy(t) for t in data["tasks"]}
    for task in tasks.values():
        task["remaining_hours"] = {
            r: max(0.0, h - (task["spent"].get(r, 0) if task["status"] == "InProgress" else 0))
            for r, h in task["hours"].items() if task["status"] != "Done"
        }
        task["remaining_hours"] = {r: h for r, h in task["remaining_hours"].items() if h > EPS}
        task["remaining_sp"] = initial_sp(task)
        task["completed_sprint"] = 0 if task["status"] == "Done" else None
        override = config["task_overrides"].get(task["id"], {})
        for key in ("remaining_sp", "remaining_hours", "required_skills", "priority"):
            if key in override and task["status"] != "Done":
                task[key] = deepcopy(override[key])
    staffed = staffed_roles(data, config) if actuals else set()
    for report in sorted(actuals, key=lambda r: r["sprint"]):
        for outcome in report["tasks"]:
            task = tasks[outcome["task_id"]]
            spent = outcome.get("spent_hours") or {}
            for role, hours in spent.items():
                task["spent"][role] = task["spent"].get(role, 0) + hours
            task["status"] = outcome["status"]
            task["remaining_sp"], task["remaining_hours"] = remaining_after(task, outcome, spent, staffed)
            task["result"] = outcome.get("result", "")
            if outcome["status"] == "Done":
                if task["completed_sprint"] is None:
                    task["completed_sprint"] = report["sprint"]
            else:
                task["completed_sprint"] = None
    cutoff = max((r["sprint"] for r in actuals), default=0)
    for tid, override in config["task_overrides"].items():
        if override.get("after_sprint", 0) == cutoff and tasks[tid]["status"] != "Done":
            for key in ("remaining_sp", "remaining_hours", "required_skills", "priority"):
                if key in override:
                    tasks[tid][key] = deepcopy(override[key])
    for tid, task in tasks.items():
        task["manual_assignments"] = deepcopy(config.get("assignments", {}).get(tid, {}))
        task["not_before"] = config.get("not_before", {}).get(tid, 1)
    return tasks


def remaining_after(task, outcome, spent, staffed=None):
    """
    Остаток задачи после факта спринта.

    Оценка главы важнее правила: если она указана, берётся она.
    Пустой остаток считается по правилу ядра (src/planner/facts.py):
    остаток роли минус потраченное; если часы кончились, а задача
    не закрыта, — 10 % сметы по каждой роли. Очки уменьшаются
    в той же доле, что и часы.

    Роли без носителей в штате (staffed их не содержит) выполняются
    вне штата, и их часов в факте обычно нет. Их остаток уменьшается
    в той же доле, в какой продвинулась работа штата, — иначе задача,
    сделанная ровно по плану, выглядела бы отстающей.
    """

    if outcome["status"] == "Done":
        return 0.0, {}
    before_hours = task["remaining_hours"]
    hours = outcome.get("remaining_hours")
    if not hours:
        external = set(before_hours) - staffed if staffed is not None else set()
        hours = spend_hours(before_hours, spent, external)
        if not hours:
            hours = {role: h * MIN_REMAINING_SHARE for role, h in task["hours"].items() if h > EPS}
    sp = outcome.get("remaining_sp")
    if sp is None:
        before = sum(before_hours.values())
        sp = task["remaining_sp"] * sum(hours.values()) / before if before > EPS else task["remaining_sp"]
        sp = min(sp, task["remaining_sp"])
    return float(sp), deepcopy(hours)


def known_roles(data, config=None):
    """Все роли, которые уже встречаются у людей и в оценках задач."""
    roles = {r for p in data["engineers"] for r in p["roles"]}
    roles |= {r for t in data["tasks"] for r in t["hours"]}
    if config:
        roles |= {r for values in config.get("role_overrides", {}).values() for r in values}
    return sorted(roles)


def people_of(data, config):
    people = deepcopy(data["engineers"])
    for person in people:
        person["roles"] = sorted(set(person["roles"] + config["role_overrides"].get(person["id"], [])))
    return people


def velocity(data, config, actuals):
    history = defaultdict(list)
    for row in data["history"]:
        history[row["team"]].append(row["achieved"])
    for report in actuals:
        for team, achieved in report["teams"].items():
            history[team].append(achieved)
    return {team: {"samples": values, "average": mean(values) if values else 0.0,
                   "capacity": (mean(values) * config["safety"]) if values else 0.0}
            for team in teams_of(data) for values in [history[team]]}


# Спринт — две недели, десять рабочих дней. Неделя отпуска снимает
# половину часов человека в этом спринте, день болезни — десятую часть.
WORKDAYS_PER_SPRINT = 10

ABSENCE_REASONS = ("Отпуск", "Болезнь", "Обучение", "Другое")


def sprint_bounds(config, sprint):
    """Первый и последний календарный день спринта."""
    start = date.fromisoformat(config["start_date"]) + timedelta(days=14 * (sprint - 1))
    return start, start + timedelta(days=13)


def workdays(first, last):
    """Рабочие дни (пн–пт) в отрезке включительно."""
    days, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            days += 1
        day += timedelta(days=1)
    return days


def absent_days(config, engineer, sprint):
    """Сколько рабочих дней спринта сотрудник отсутствует — по всем его отсутствиям."""
    first, last = sprint_bounds(config, sprint)
    days = 0
    for absence in config.get("absences", []):
        if absence["engineer"] != engineer:
            continue
        start, end = date.fromisoformat(absence["start"]), date.fromisoformat(absence["end"])
        if start <= last and end >= first:
            days += workdays(max(start, first), min(end, last))
    return min(days, WORKDAYS_PER_SPRINT)


def absence_impact(data, config, absence):
    """Потерянные часы по спринтам для одного отсутствия."""
    person = next((p for p in data["engineers"] if p["id"] == absence["engineer"]), None)
    if person is None:
        return {}
    rate = sum(person["allocations"].values())
    start, end = date.fromisoformat(absence["start"]), date.fromisoformat(absence["end"])
    impact = {}
    for sprint in range(1, config["horizon"] + 1):
        first, last = sprint_bounds(config, sprint)
        if start <= last and end >= first:
            days = workdays(max(start, first), min(end, last))
            if days:
                base = config["availability"].get(person["id"], {}).get(str(sprint), HOURS_PER_SPRINT * rate)
                impact[str(sprint)] = round(base * days / WORKDAYS_PER_SPRINT, 1)
    return impact


def capacities(data, config, sprint):
    global_hours, team_hours = {}, {}
    for p in data["engineers"]:
        total_rate = sum(p["allocations"].values())
        available = config["availability"].get(p["id"], {}).get(str(sprint), HOURS_PER_SPRINT * total_rate)
        # Отпуск, болезнь: часы уменьшаются в доле пропущенных рабочих дней.
        available *= 1 - absent_days(config, p["id"], sprint) / WORKDAYS_PER_SPRINT
        global_hours[p["id"]] = available
        for team, rate in p["allocations"].items():
            team_hours[p["id"], team] = available * rate / total_rate if total_rate else 0.0
    for transfer in config["transfers"]:
        if transfer["start"] <= sprint <= transfer["end"]:
            eid, donor, target, hours = transfer["engineer"], transfer["from_team"], transfer["to_team"], transfer["hours"]
            if hours > team_hours.get((eid, donor), 0) + EPS and absent_days(config, eid, sprint):
                # Помощь согласована раньше, чем стало известно об отсутствии:
                # человек отдаёт столько, сколько у него осталось.
                hours = team_hours.get((eid, donor), 0)
            if hours > team_hours.get((eid, donor), 0) + EPS:
                raise InputError(f"{eid}, спринт {sprint}: нельзя передать {hours:g} ч, доступно {team_hours.get((eid, donor), 0):g} ч в {donor}")
            team_hours[eid, donor] = team_hours.get((eid, donor), 0) - hours
            team_hours[eid, target] = team_hours.get((eid, target), 0) + hours
    return global_hours, team_hours


def skills_map(data, config, plan, sprint=1):
    """
    Звёздная карта по текущему плану.

    cells — Bus Factor технологий внутри команд: носители, их свободные
    часы в спринте и работы, которые носители ведут по плану;
    roles — Bus Factor ролей по организации (src/star_map.role_bus_factor):
    без единственного носителя роли встают все её работы;
    teams — профили команд: ёмкость, покрытие ролей своими людьми,
    часы других команд в работах команды;
    people — доступность и загрузка людей в спринте;
    org — Bus Factor технологий по организации и уникальные профили.
    """
    people = people_of(data, config)
    global_cap, team_cap = capacities(data, config, sprint)
    cutoff = plan.get("cutoff", 0)
    quarter = config["sprints"]
    load = defaultdict(float)
    team_load = defaultdict(float)
    # Работы, которые человек ведёт по плану в незакрытых спринтах.
    assigned = defaultdict(set)
    borrowed = defaultdict(float)
    team_sp = defaultdict(float)
    members_of = {team: {p["id"] for p in people if p["allocations"].get(team, 0) > 0} for team in teams_of(data)}
    for tid, entry in plan["entries"].items():
        team = plan["tasks"][tid]["team"]
        for fragment in entry["fragments"]:
            if fragment["sprint"] <= cutoff:
                continue
            if fragment["sprint"] == sprint:
                team_sp[team] += fragment["sp"]
            for a in fragment["assignments"]:
                assigned[a["engineer"]].add(tid)
                if fragment["sprint"] <= quarter and a["engineer"] not in members_of.get(team, ()):
                    borrowed[team] += a["hours"]
                if fragment["sprint"] == sprint:
                    load[a["engineer"]] += a["hours"]
                    team_load[a["engineer"], team] += a["hours"]

    def related(holders, team=None):
        tasks = {tid for p in holders for tid in assigned[p["id"]]}
        return sorted(tasks, key=lambda tid: (plan["tasks"][tid]["team"] != team, tid))

    cells = []
    for team in teams_of(data):
        members = [p for p in people if p["allocations"].get(team, 0) > 0]
        for skill in sorted({s for p in members for s in p["skills"]}):
            holders = [p for p in members if skill in p["skills"]]
            cells.append(dict(team=team, skill=skill, bf=len(holders), holders=[p["id"] for p in holders],
                available_hours=sum(team_cap.get((p["id"], team), 0) for p in holders),
                free_hours=sum(max(0, min(global_cap[p["id"]] - load[p["id"]], team_cap.get((p["id"], team), 0) - team_load[p["id"], team])) for p in holders),
                related_tasks=related(holders, team)))

    open_tasks = [dict(id=t["id"], team=t["team"], initiative=t["initiative"], remaining_hours=t["remaining_hours"])
                  for t in plan["tasks"].values() if t["status"] != "Done"]
    roles = role_bus_factor(people, open_tasks, HOURS_PER_SPRINT)
    goals = {i["id"]: i for i in plan.get("initiatives", [])}
    for row in roles:
        row["available_hours"] = sum(global_cap.get(eid, 0.0) for eid in row["holders"])
        row["planned_hours"] = sum(
            a["hours"] for entry in plan["entries"].values() for f in entry["fragments"]
            if f["sprint"] == sprint for a in f["assignments"] if a["role"] == row["role"])
        # Что встанет без единственного носителя: цели квартала, у которых
        # есть открытая работа этой роли, — другого исполнителя роли нет.
        row["goals_at_risk"] = sorted(i for i in row["initiatives"]
                                      if goals.get(i, {}).get("forecast_in_quarter")) if row["bf"] == 1 else []

    teams = {}
    for team in teams_of(data):
        members = [p for p in people if p["allocations"].get(team, 0) > 0]
        demand = defaultdict(float)
        for task in open_tasks:
            if task["team"] == team:
                for role, hours in task["remaining_hours"].items():
                    demand[role] += hours
        coverage = []
        for role in sorted(set(demand) | {r for p in members for r in p["roles"]}):
            own = [p["id"] for p in members if role in p["roles"]]
            coverage.append(dict(role=role, holders=own, bf=len(own),
                                 own_hours=sum(team_cap.get((eid, team), 0.0) for eid in own),
                                 demand_hours=demand.get(role, 0.0)))
        resource = plan.get("resources", {}).get(team, {})
        teams[team] = dict(
            members=[p["id"] for p in members],
            capacity_sp=resource.get("capacity", 0.0),
            planned_sp=team_sp.get(team, 0.0),
            open_tasks=sorted(t["id"] for t in open_tasks if t["team"] == team),
            roles=sorted(coverage, key=lambda r: (-r["demand_hours"], r["role"])),
            borrowed_hours=borrowed.get(team, 0.0),
            single=[r["role"] for r in coverage if r["bf"] == 1 and r["demand_hours"] > 0],
            missing=[r["role"] for r in coverage if r["bf"] == 0 and r["demand_hours"] > 0],
        )

    single_roles = {eid: [r["role"] for r in roles if r["bf"] == 1 and eid in r["holders"]] for eid in (p["id"] for p in people)}
    return dict(cells=cells, roles=roles, teams=teams, sprint=sprint,
                people=[dict(p, available=global_cap[p["id"]], used=load[p["id"]],
                             single_roles=single_roles.get(p["id"], []),
                             tasks=sorted(assigned[p["id"]])) for p in people],
                org=organization_map(people))


def organization_map(people):
    """
    Звёздная карта ядра (src/star_map.py) по текущему составу:
    Bus Factor каждой технологии по всей организации и уникальные
    специалисты — те, кого DBSCAN не отнёс ни к одной группе похожих
    профилей. Совместитель считается одним человеком.
    """

    rows = [dict(engineer_id=p["id"], team_id=team, role=(p["roles"] or [""])[0], grade=p.get("grade", ""),
                 skills_declared=", ".join(p["skills"]), capacity_rate=rate, skills=list(p["skills"]))
            for p in people for team, rate in sorted(p["allocations"].items())]
    if not rows:
        return dict(bus_factor=[], unique=[], clusters={})
    clustered, unique, report = analyze_bus_factor(pd.DataFrame(rows))
    holders = defaultdict(list)
    for p in people:
        for skill in p["skills"]:
            holders[skill].append(p["id"])
    return dict(
        bus_factor=[dict(skill=skill, bf=count, holders=sorted(holders[skill]))
                    for skill, count in sorted(report.items(), key=lambda item: (item[1], item[0]))],
        unique=sorted(unique["engineer_id"].tolist()),
        clusters={row.engineer_id: int(row.cluster_id) for row in clustered.itertuples(index=False)},
    )


def sprint_dates(config):
    start = date.fromisoformat(config["start_date"])
    return [dict(number=s, start=(start + timedelta(days=14 * (s - 1))).isoformat(),
                 end=(start + timedelta(days=14 * s - 1)).isoformat(), forecast=s > config["sprints"])
            for s in range(1, config["horizon"] + 1)]
