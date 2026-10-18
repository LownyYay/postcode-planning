"""Операции над проектом: создание, фиксация базы и факт спринта."""

from copy import deepcopy
from datetime import date

from .engine import (ABSENCE_REASONS, HOURS_PER_SPRINT, PLANNER, default_config, diff_plans, known_roles,
                     snapshot, skills_map, sprint_dates, teams_of, capacities)
from .present import build_plan, core_baseline, quarter_metrics
from .sprint_data import consequences, sprint_review
from .importer import InputError, import_xlsx, number, role_name
from .store import ConflictError


def numeric_map(value, label, known=()):
    if not isinstance(value, dict):
        raise InputError(f"{label}: требуется объект роль → число")
    result = {}
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip():
            raise InputError(f"{label}: пустое имя роли")
        role = role_name(key, known)
        result[role] = result.get(role, 0) + number(val, label)
    return {k: v for k, v in result.items() if v > 0}


def optional_number(value, label):
    return None if value is None or value == "" else number(value, label)


def check_outcome(outcome, known=()):
    """
    Итог задачи за спринт. Пустой остаток — не ошибка: его посчитает
    правило ядра по потраченным часам (engine.remaining_after).
    """
    outcome["remaining_sp"] = optional_number(outcome.get("remaining_sp"), "Остаток SP")
    hours = outcome.get("remaining_hours")
    outcome["remaining_hours"] = numeric_map(hours, "Остаток часов", known) if hours else None
    outcome["spent_hours"] = numeric_map(outcome.get("spent_hours") or {}, "Фактические часы спринта", known)
    if outcome["status"] == "Done":
        if outcome["remaining_sp"] or outcome["remaining_hours"]:
            raise InputError("У завершённой задачи остаток SP и часов должен быть нулевым")
        outcome["remaining_sp"], outcome["remaining_hours"] = 0.0, {}
    elif outcome["remaining_sp"] is not None and outcome["remaining_sp"] <= 0:
        raise InputError("Остаток SP незавершённой задачи должен быть положительным или пустым — тогда он считается по часам")
    return outcome


def validate_config(data, config):
    try:
        date.fromisoformat(config["start_date"])
    except (ValueError, TypeError):
        raise InputError("Укажите дату начала в формате YYYY-MM-DD") from None
    observed_dates = [h['date'] for h in data['history'] if h.get('date')]
    observed_dates += [t['actual_end'] for t in data['tasks'] if t['status'] == 'Done' and t.get('actual_end')]
    if observed_dates and config['start_date'] <= max(observed_dates):
        raise InputError('Новый период должен начинаться после последнего исходного факта: ' + max(observed_dates))
    expected = (PLANNER.input.quarter_sprints, PLANNER.input.horizon_sprints, PLANNER.input.focus_factor)
    if (config["sprints"], config["horizon"], config["safety"]) != expected:
        raise InputError("Параметры квартала расходятся с config/planner.yaml: "
                         f"{expected[0]} спринтов, горизонт {expected[1]}, коэффициент {expected[2]:g}")
    horizon = config["horizon"]
    people = {p["id"]: p for p in data["engineers"]}
    tasks = {t["id"]: t for t in data["tasks"]}
    for tid, start in config.get('not_before', {}).items():
        if tid not in tasks or type(start) is not int or not 1 <= start <= horizon:
            raise InputError(f'Начало задачи должно быть внутри 1–{horizon} спринтов')
    for tid, roles in config.get('assignments', {}).items():
        if tid not in tasks or not isinstance(roles, dict):
            raise InputError('Неверное ручное назначение')
        for role, eid in roles.items():
            if eid not in people or role not in set(people[eid]['roles'] + config['role_overrides'].get(eid, [])):
                raise InputError(f'{tid}: у выбранного сотрудника не подтверждена роль {role}')
    for eid, values in config["availability"].items():
        if eid not in people or not isinstance(values, dict):
            raise InputError("Неизвестный инженер или неверная доступность")
        for sprint, hours in values.items():
            if str(sprint) not in {str(s) for s in range(1, horizon + 1)}:
                raise InputError(f"Спринт доступности должен быть от 1 до {horizon}")
            value = number(hours, "Доступные часы")
            if value > HOURS_PER_SPRINT * sum(people[eid]["allocations"].values()) + 1e-6:
                raise InputError(f"{eid}: доступность превышает ставку")
            values[sprint] = value
    for absence in config.get("absences", []):
        if not isinstance(absence, dict) or absence.get("engineer") not in people:
            raise InputError("Отсутствие: неизвестный сотрудник")
        try:
            start, end = date.fromisoformat(absence["start"]), date.fromisoformat(absence["end"])
        except (KeyError, TypeError, ValueError):
            raise InputError("Отсутствие: укажите даты в формате ГГГГ-ММ-ДД") from None
        if start > end:
            raise InputError("Отсутствие: дата окончания раньше даты начала")
        if (end - start).days > 120:
            raise InputError("Отсутствие длиннее 120 дней: для увольнения или долгого перерыва измените состав команды")
        if absence.get("reason") not in ABSENCE_REASONS:
            raise InputError("Отсутствие: выберите причину — " + ", ".join(ABSENCE_REASONS))
    for eid, roles in config["role_overrides"].items():
        if eid not in people or not isinstance(roles, list) or any(not isinstance(r, str) or not r.strip() for r in roles):
            raise InputError("Укажите известного инженера и список подтверждённых ролей")
        config["role_overrides"][eid] = sorted(set(role_name(r, known_roles(data)) for r in roles))
    for tid, override in config["task_overrides"].items():
        if tid not in tasks or not isinstance(override, dict):
            raise InputError("Неизвестная задача")
        if set(override) - {"remaining_sp", "remaining_hours", "priority", "required_skills", "after_sprint"}:
            raise InputError("Неизвестное поле переоценки")
        for key in ("remaining_sp", "priority"):
            if key in override:
                override[key] = number(override[key], key)
        if "remaining_hours" in override:
            override["remaining_hours"] = numeric_map(override["remaining_hours"], "Остаток часов", known_roles(data, config))
        if "required_skills" in override:
            value = override["required_skills"]
            if not isinstance(value, dict) or any(not isinstance(v, list) or any(not isinstance(x, str) for x in v) for v in value.values()):
                raise InputError("Требования навыков: объект роль → список навыков")
    for transfer in config["transfers"]:
        required = {"engineer", "from_team", "to_team", "hours", "start", "end"}
        if not isinstance(transfer, dict) or set(transfer) != required:
            raise InputError("Неверные поля передачи часов")
        if transfer["engineer"] not in people or transfer["from_team"] == transfer["to_team"]:
            raise InputError("Неверный инженер или одинаковые команды")
        if transfer["from_team"] not in teams_of(data) or transfer["to_team"] not in teams_of(data):
            raise InputError("Неизвестная команда")
        if type(transfer["start"]) is not int or type(transfer["end"]) is not int or not 1 <= transfer["start"] <= transfer["end"] <= horizon:
            raise InputError(f"Интервал передачи часов должен быть внутри 1–{horizon} спринтов")
        transfer["hours"] = number(transfer["hours"], "Передаваемые часы")
        if not transfer["hours"]:
            raise InputError("Передайте положительное число часов")
    for sprint in range(1, horizon + 1):
        capacities(data, config, sprint)


class Service:
    def __init__(self, store):
        self.store = store

    def create(self, data, config=None):
        config = config or default_config()
        validate_config(data, config)
        plan = build_plan(data, config)
        return self.store.create(dict(dataset=data, config=config, actuals=[], baseline=None, plan=plan, changes=[]))

    def initial(self, path):
        data = import_xlsx(path, 'Обновлённый датасет хакатона')
        for project in reversed(self.store.projects()):
            state = self.store.get(project['id'])
            if state['dataset'].get('source', {}).get('sha256') == data['source']['sha256']:
                return state
        return self.create(data)

    def public(self, state):
        return dict(state, projects=self.store.projects(), versions=self.store.versions(state["project_id"]),
                    skills=skills_map(state["dataset"], state["config"], state["plan"], min(12, state["plan"]["cutoff"] + 1)),
                    metrics=quarter_metrics(state),
                    dates=sprint_dates(state["config"]))

    def load(self, body):
        state = self.store.get(body["project_id"])
        if state["revision"] != body.get("revision"):
            raise ConflictError("План изменился. Обновите страницу")
        return state

    def approve(self, body, state=None):
        state = self.load(body) if state is None else state
        if state["baseline"]:
            raise InputError("Первоначальная база уже зафиксирована")
        core = core_baseline(state["plan"])
        if core is None:
            raise InputError("План не рассчитан ядром — пересчитайте его перед фиксацией")
        planned = core.planned_sp_by_sprint()
        sprint_sp = {str(s): sum(planned.get(s, {}).values()) for s in range(1, state["config"]["sprints"] + 1)}
        # Данные и настройки на момент фиксации: по ним ядро считает
        # зачёт SP по факту — тем же входом, что и у базового плана.
        state["baseline"] = dict(goal_ids=list(core.planned_initiatives), sprint_sp=sprint_sp,
                                 plan=deepcopy(state["plan"]), revision=state["revision"],
                                 dataset=deepcopy(state["dataset"]), config=deepcopy(state["config"]),
                                 rule="Квартальные цели — инициативы, которые исходный план завершает в квартале")
        return self.store.save(state, body["revision"], "Зафиксирован первоначальный план")

    def actual(self, body, state=None):
        state = self.load(body) if state is None else state
        if not state["baseline"]:
            raise InputError("Сначала зафиксируйте первоначальный план")
        report = deepcopy(body.get("report", {}))
        sprint = report.get("sprint")
        cutoff = state["plan"]["cutoff"]
        if type(sprint) is not int or not 1 <= sprint <= state["config"]["sprints"] or sprint not in (cutoff, cutoff + 1):
            raise InputError("Закрывайте спринты по порядку; исправить можно последний закрытый спринт")
        previous_reports = [r for r in state["actuals"] if r["sprint"] < sprint]
        before = snapshot(state["dataset"], state["config"], previous_reports)
        if set(report.get("teams", {})) != set(teams_of(state["dataset"])):
            raise InputError("Укажите фактические SP каждой команды, включая нули")
        report["teams"] = {team: number(value, f"Факт SP {team}") for team, value in report["teams"].items()}
        outcomes = report.get("tasks")
        if not isinstance(outcomes, list):
            raise InputError("tasks должен быть списком итогов задач")
        required = {tid for tid, task in before.items() if task["status"] != "Done"}
        ids = [outcome.get("task_id") for outcome in outcomes]
        if len(set(ids)) != len(ids) or set(ids) != required:
            raise InputError("Подтвердите состояние всех незавершённых задач; повторные или неизвестные ID недопустимы")
        for outcome in outcomes:
            status = outcome.get("status")
            if status not in ("Done", "InProgress", "ToDo"):
                raise InputError("Неизвестный статус факта")
            check_outcome(outcome, known_roles(state["dataset"], state["config"]))
        report["note"] = str(report.get("note", ""))[:2000]
        report["history_rows"] = list(report.get("history_rows", []))
        report = {k: report[k] for k in ("sprint", "teams", "tasks", "note", "history_rows")}
        current = next((r for r in state["actuals"] if r["sprint"] == sprint), None)
        if current == report:
            return state  # Идемпотентное повторение не создаёт второй факт/версию.
        state["actuals"] = previous_reports + [report]
        config = deepcopy(state["config"])
        # Факт заменяет остатки, но сохраняет решения главы о приоритете и навыках.
        for tid in ids:
            override = config["task_overrides"].get(tid, {})
            retained = {k: deepcopy(override[k]) for k in ("priority", "required_skills") if k in override}
            if retained:
                config["task_overrides"][tid] = dict(retained, after_sprint=sprint)
            else:
                config["task_overrides"].pop(tid, None)
        plan = build_plan(state["dataset"], config, state["actuals"], state["plan"], baseline=state["baseline"])
        changes, capacity = [], {}
        for team, row in plan["resources"].items():
            old = state["plan"]["resources"][team]["capacity"]
            if abs(old - row["capacity"]) > 1e-6:
                changes.append(f"{team}: {old:.2f} → {row['capacity']:.2f} SP")
                capacity[team] = (old, row["capacity"])
        cause = f"Факт спринта {sprint}; " + ("; ".join(changes) or "уточнены статусы и остатки")
        # Какие отклонения спринта вызвали изменения: сравнение плана
        # спринта с фактом и цели, которые ушли за квартал. По ним же
        # у каждой сдвинувшейся задачи находится своя причина.
        review = sprint_review(sprint, state["plan"], plan, report, before)
        context = dict(sprint=sprint, capacity=capacity,
                       deviations={d["task_id"]: d["text"] for d in review["deviations"]})
        state["changes"] = diff_plans(state["plan"], plan, cause, context)
        review.update(capacity=changes, dataset=body.get("dataset"), info=body.get("info"),
                      consequences=consequences(state["changes"], review))
        state.setdefault("reviews", {})[str(sprint)] = review
        state.update(plan=plan, config=config, plan_stale=None)
        label = "исправлен" if current is not None else "закрыт"
        return self.store.save(state, body["revision"], f"Спринт {sprint} {label}: итоговый пересчёт")
