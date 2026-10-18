"""Данные спринта: обновлённый датасет, отчёты команд и итоговый пересчёт.

После каждого спринта приходят два источника:

- обновлённый датасет — тот же Excel, что и в начале квартала, но со срезом
  на конец спринта: статусы, накопленные часы, оценки, новые задачи и люди,
  строки производительности команд;
- отчёты команд об итогах спринта — их главы вносят в течение спринта.

Ни загрузка, ни отчёт план не пересчитывают. Итоговый пересчёт — отдельное
действие: он сливает датасет с данными проекта, собирает факт спринта и
один раз строит новый план.

Старое не удаляется. Задачи и люди, которых нет в новом датасете, остаются;
изменения из датасета корректируют существующие записи. Повторная загрузка
датасета за уже закрытый спринт корректирует этот спринт, а не создаёт
новый. Данные проекта хранят состояние на начало квартала (или на момент
появления задачи), а прогресс приходит фактами спринтов — так закрытые
спринты и база сравнения не переписываются задним числом.
"""

import re
from copy import deepcopy

from src.planner.facts import work_share

from .engine import EPS, snapshot, staffed_roles, teams_of

# Поля задачи, которые обновлённый датасет корректирует. Статус, часы
# и итог закрытия идут через факт спринта, а не через данные проекта.
TASK_FIELDS = ("title", "initiative", "team", "parent", "priority", "sp", "original_hh",
               "hours", "desired_result", "planned_start", "planned_end")

PERSON_FIELDS = ("roles", "skills", "grade", "allocations")

# Отставание от плана спринта меньше этой доли отклонением не считается.
PLAN_TOLERANCE = 0.1


def _h(value):
    """Часы для текста: до десятых, с запятой."""
    return f"{round(value, 1):g}".replace(".", ",")


def _transferred(state):
    """Задачи, команду которых поменяла согласованная заявка."""
    moved = set()
    for request in state.get("requests", []):
        if request.get("status") == "approved" and request.get("kind") in ("transfer", "exchange"):
            payload = request.get("payload", {})
            moved.update(filter(None, (payload.get("task_id"), payload.get("other_task_id"))))
    return moved


def merge_dataset(state, fresh):
    """
    Данные проекта, скорректированные обновлённым датасетом.

    Новые задачи входят в данные не начатыми: их прогресс, как у всех,
    приходит фактом спринта. У существующих обновляются оценки и описание;
    команда не меняется, если её сменила согласованная заявка. Люди
    добавляются и обновляются, отсутствующие в датасете остаются.
    """

    merged = deepcopy(state["dataset"])
    notes = []
    moved = _transferred(state)
    tasks = {task["id"]: task for task in merged["tasks"]}

    for task in fresh["tasks"]:
        current = tasks.get(task["id"])

        if current is None:
            added = deepcopy(task)
            added.update(status="ToDo", spent={}, actual_end="", result="", source_row=None)
            merged["tasks"].append(added)
            tasks[task["id"]] = added
            continue

        for key in TASK_FIELDS:
            if key == "team" and task["id"] in moved and current["team"] != task["team"]:
                notes.append(f"{task['id']}: команда оставлена {current['team']} — её сменила согласованная заявка, "
                             f"в датасете {task['team']}")
                continue
            current[key] = deepcopy(task[key])

    people = {person["id"]: person for person in merged["engineers"]}

    for person in fresh["engineers"]:
        current = people.get(person["id"])

        if current is None:
            merged["engineers"].append(deepcopy(person))
        else:
            for key in PERSON_FIELDS:
                current[key] = deepcopy(person[key])

    known = {(d["first"], d["second"]) for d in merged["dependencies"]}
    merged["dependencies"].extend(deepcopy(d) for d in fresh["dependencies"]
                                  if (d["first"], d["second"]) not in known)
    merged["references"] = deepcopy(fresh.get("references", merged.get("references", {})))
    merged["issues"] = deepcopy(fresh["issues"])

    return merged, notes


def _history_rows(state, fresh, sprint):
    """
    Строки производительности команд, которые датасет добавил к спринту.

    Новые — те, что не были в исходных данных и не ушли в факт
    предыдущих спринтов. Если у команды их несколько, берётся последняя.
    """

    used = {(row["team"], row["date"]) for row in state["dataset"]["history"]}

    for report in state["actuals"]:
        if report["sprint"] < sprint:
            used.update((row["team"], row["date"]) for row in report.get("history_rows", []))

    latest = {}

    for row in fresh.get("history", []):
        if (row["team"], row["date"]) in used:
            continue
        if row["team"] not in latest or row["date"] > latest[row["team"]]["date"]:
            latest[row["team"]] = row

    return latest


def _spent_this_sprint(task, row):
    """Потрачено за спринт: накопленные часы датасета минус уже учтённые."""
    spent, corrections = {}, []

    for role, total in row["spent"].items():
        delta = total - task["spent"].get(role, 0)

        if delta > EPS:
            spent[role] = round(delta, 6)
        elif delta < -EPS:
            corrections.append(f"{role}: в датасете {_h(total)} ч, уже учтено {_h(task['spent'][role])} ч")

    return spent, corrections


def _from_dataset(tid, row, task, staffed):
    """
    Итог задачи за спринт по обновлённому датасету.

    Остаток роли штата — смета минус накопленные часы датасета. Роль без
    носителей в штате, по которой в спринте часов нет, уменьшается в той
    же доле, в какой продвинулась работа штата (правило ядра, spend_hours):
    внешние исполнители идут вместе с командой. Очки — в той же доле,
    что и часы (inprogress_sp = by_hours).
    """
    spent, corrections = _spent_this_sprint(task, row)

    if row["status"] == "Done":
        remaining_sp, hours = 0.0, {}
    else:
        external = {role for role in row["hours"] if role not in staffed}
        share = work_share(task["remaining_hours"], spent, external)
        left = {}
        for role, est in row["hours"].items():
            if role in external and role not in spent:
                left[role] = task["remaining_hours"].get(role, 0.0) * (1 - share)
            else:
                left[role] = est - row["spent"].get(role, 0)
        hours = {role: round(h, 6) for role, h in left.items() if h > EPS}
        total = sum(row["hours"].values())
        remaining_sp = row["sp"] * sum(hours.values()) / total if hours and total > EPS else None
        hours = hours or None

    outcome = dict(task_id=tid, status=row["status"], remaining_sp=remaining_sp,
                   remaining_hours=hours, spent_hours=spent)

    if row.get("result"):
        outcome["result"] = row["result"]

    return outcome, corrections


def collect_report(state, sprint, fresh, drafts):
    """
    Факт спринта из обновлённого датасета и отчётов команд.

    По каждой незавершённой задаче: если она есть в датасете — статус,
    потраченные за спринт часы и остаток берутся из него, это официальный
    срез; иначе — из отчёта команды; иначе задача считается без изменений.
    SP команды — строка производительности из датасета, иначе отчёт главы.

    Возвращает (report, info). info — источники, расхождения датасета
    с отчётами и команды без данных о SP; по нему интерфейс показывает
    готовность к пересчёту.
    """

    previous = [r for r in state["actuals"] if r["sprint"] < sprint]
    before = snapshot(state["dataset"], state["config"], previous)
    staffed = staffed_roles(state["dataset"], state["config"])
    rows = {task["id"]: task for task in (fresh or {}).get("tasks", [])}
    reported = {o["task_id"]: (team, o) for team, draft in drafts.items() for o in draft.get("tasks", [])}

    outcomes, sources, discrepancies, corrections, untouched = [], {}, [], [], []

    for tid, task in sorted(before.items()):
        if task["status"] == "Done":
            continue

        row = rows.get(tid)
        team_report = reported.get(tid)

        if row is not None:
            outcome, fixes = _from_dataset(tid, row, task, staffed)
            corrections.extend(f"{tid}, {text}" for text in fixes)
            sources[tid] = "dataset"

            if team_report is not None:
                team, own = team_report
                if own["status"] != outcome["status"]:
                    discrepancies.append(dict(task_id=tid, team=team,
                        text=f"статус: в датасете {outcome['status']}, в отчёте команды {own['status']}"))
                ours, theirs = sum(outcome["spent_hours"].values()), sum((own.get("spent_hours") or {}).values())
                if abs(ours - theirs) > 0.5:
                    discrepancies.append(dict(task_id=tid, team=team,
                        text=f"потрачено за спринт: по датасету {_h(ours)} ч, в отчёте команды {_h(theirs)} ч"))
        elif team_report is not None:
            outcome = {key: deepcopy(team_report[1].get(key)) for key in
                       ("task_id", "status", "remaining_sp", "remaining_hours", "spent_hours")}
            sources[tid] = "report"
        else:
            outcome = dict(task_id=tid, status=task["status"], remaining_sp=None,
                           remaining_hours=None, spent_hours={})
            sources[tid] = "none"
            untouched.append(tid)

        outcomes.append(outcome)

    history = _history_rows(state, fresh or {}, sprint)
    teams, team_sources, missing = {}, {}, []

    for team in teams_of(state["dataset"]):
        if team in history:
            teams[team] = history[team]["achieved"]
            team_sources[team] = "dataset"
        elif team in drafts:
            teams[team] = drafts[team]["achieved_sp"]
            team_sources[team] = "report"
        else:
            missing.append(team)
            team_sources[team] = "none"

    notes = [f"{team}: {draft['note']}" for team, draft in sorted(drafts.items()) if draft.get("note")]
    report = dict(sprint=sprint, teams=teams, tasks=outcomes, note="; ".join(notes),
                  history_rows=[dict(team=r["team"], date=r["date"]) for r in history.values()])
    info = dict(sources=sources, team_sources=team_sources, missing_teams=missing,
                discrepancies=discrepancies, corrections=corrections, untouched=untouched)

    return report, info


def dataset_summary(state, fresh, sprint):
    """
    Что изменит загруженный датасет — до пересчёта, чтобы глава мог проверить.
    """

    previous = [r for r in state["actuals"] if r["sprint"] < sprint]
    before = snapshot(state["dataset"], state["config"], previous)
    known_people = {p["id"] for p in state["dataset"]["engineers"]}
    status_changes, spent_total, closed = [], 0.0, []

    for task in fresh["tasks"]:
        current = before.get(task["id"])
        if current is None or current["status"] == "Done":
            continue
        if task["status"] != current["status"]:
            status_changes.append(dict(task_id=task["id"], before=current["status"], after=task["status"]))
            if task["status"] == "Done":
                closed.append(task["id"])
        spent, _ = _spent_this_sprint(current, task)
        spent_total += sum(spent.values())

    history = _history_rows(state, fresh, sprint)

    return dict(
        tasks=len(fresh["tasks"]),
        new_tasks=sorted(t["id"] for t in fresh["tasks"] if t["id"] not in before),
        missing_tasks=sorted(set(before) - {t["id"] for t in fresh["tasks"]}),
        new_people=sorted(p["id"] for p in fresh["engineers"] if p["id"] not in known_people),
        status_changes=status_changes,
        closed=closed,
        spent_hours=round(spent_total, 1),
        velocity={team: row["achieved"] for team, row in sorted(history.items())},
    )


def sprint_review(sprint, old_plan, new_plan, report, before):
    """
    Какие отклонения спринта вызвали изменения плана.

    Сравнивает план спринта из прежней версии с фактом: что не завершилось
    в срок, что не начато, где потрачено меньше или больше плана, что
    закрыто раньше. И какие квартальные цели оказались под угрозой.
    """

    deviations = []

    for outcome in report["tasks"]:
        tid = outcome["task_id"]
        entry = old_plan["entries"].get(tid, {})
        planned = [f for f in entry.get("fragments", []) if f["sprint"] == sprint]
        planned_hours = sum(a["hours"] for f in planned for a in f["assignments"])
        spent = sum((outcome.get("spent_hours") or {}).values())
        done = outcome["status"] == "Done"
        end = entry.get("end")
        team = before.get(tid, {}).get("team", "")

        if end == sprint and not done:
            text = f"должна была завершиться в спринте {sprint}, не завершена"
            if planned_hours > EPS:
                text += f": потрачено {_h(spent)} ч из {_h(planned_hours)} ч плана спринта"
        elif done and end is not None and end > sprint:
            text = f"завершена раньше плана (план: спринт {end})"
        elif planned_hours > EPS and spent <= EPS and not done:
            text = f"работа по плану спринта не начата: запланировано {_h(planned_hours)} ч"
        elif planned_hours > EPS and spent < (1 - PLAN_TOLERANCE) * planned_hours and not done:
            text = f"выполнено {_h(spent)} ч из {_h(planned_hours)} ч плана спринта"
        elif planned_hours <= EPS and spent > EPS:
            text = f"работа вне плана спринта: потрачено {_h(spent)} ч"
        else:
            continue

        deviations.append(dict(task_id=tid, team=team, text=text))

    old_goals = {i["id"]: i for i in old_plan.get("initiatives", [])}
    new_goals = {i["id"]: i for i in new_plan.get("initiatives", [])}
    lost = sorted(i for i, goal in new_goals.items()
                  if not goal["forecast_in_quarter"] and old_goals.get(i, {}).get("forecast_in_quarter"))
    gained = sorted(i for i, goal in new_goals.items()
                    if goal["forecast_in_quarter"] and not old_goals.get(i, {}).get("forecast_in_quarter", True))

    return dict(sprint=sprint, deviations=deviations, goals_lost=lost, goals_gained=gained)


def consequences(changes, review):
    """
    Что потянуло за собой каждое отклонение спринта.

    Задача попадает в последствия отклонения, если оно названо в причине
    её сдвига: ждёт отставшую задачу или уступила ей место в спринте.
    """

    result = {}
    for deviation in review["deviations"]:
        tid = deviation["task_id"]
        named = re.compile(rf"(?<![\w-]){re.escape(tid)}(?![\w-])")
        affected = [dict(task_id=c["task_id"], before=c["before"]["end"], after=c["after"]["end"])
                    for c in changes
                    if c.get("kind") == "schedule" and c["task_id"] != tid and named.search(c["reason"])]
        if affected:
            result[tid] = affected
    return result


def replace_initial(state, fresh):
    """
    Новый исходный датасет до фиксации базы: квартал ещё не начался,
    поэтому статусы, часы и история производительности берутся из него
    целиком. Задачи и люди, добавленные в сервисе, остаются.
    """

    merged, notes = merge_dataset(state, fresh)
    tasks = {task["id"]: task for task in merged["tasks"]}

    for task in fresh["tasks"]:
        for key in ("status", "spent", "actual_end", "result"):
            tasks[task["id"]][key] = deepcopy(task[key])

    merged["history"] = deepcopy(fresh["history"])
    merged["source"] = deepcopy(fresh["source"])
    merged["name"] = fresh["name"]

    return merged, notes
