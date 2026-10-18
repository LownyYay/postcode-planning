"""Изменения плана и их причины — у каждой задачи своя.

После пересчёта важно не только что сдвинулось, но и почему. Причина
ищется в том порядке, в каком её проверил бы руководитель:

1. задача закрыта фактом или сама отклонилась от плана спринта;
2. сдвинулась блокирующая задача — работает правило зазора зависимостей;
3. изменилась ёмкость её команды в SP;
4. в спринте, где задача потеряла объём, ресурс команды или роли
   забрала другая задача — называем её и её собственную причину;
5. иначе — общее условие пересчёта: отпуск, согласованная помощь,
   ручное изменение.

Вид изменения: schedule — сдвинулся срок или статус, done — задача
закрыта, assignments — только новые исполнители или объём по спринтам
при прежнем сроке. В интерфейсе они не смешиваются.
"""

from collections import defaultdict

EPS = 1e-6

# Доля SP или часы, меньше которых перестановку внутри спринта не считаем.
SP_TOLERANCE = 0.05
HOURS_TOLERANCE = 0.5


def _sprint(value):
    return "—" if value is None else f"С{value}"


def _work_signature(entry):
    return [(f["sprint"], round(f["sp"], 4),
             sorted((a["engineer"], a["role"], round(a["hours"], 4)) for a in f["assignments"]))
            for f in entry.get("fragments", [])]


def _sp_by_sprint(entry):
    result = defaultdict(float)
    for fragment in entry.get("fragments", []):
        result[fragment["sprint"]] += fragment["sp"]
    return result


def _hours_by_sprint(entry):
    result = defaultdict(lambda: defaultdict(float))
    for fragment in entry.get("fragments", []):
        for row in fragment["assignments"]:
            result[fragment["sprint"]][row["role"]] += row["hours"]
    return result


def _later(before, after):
    """Сдвиг срока позже: None (не завершается) позже любого спринта."""
    if before is None:
        return False
    return after is None or after > before


def _earlier(before, after):
    if after is None:
        return False
    return before is None or after < before


class _Explainer:
    def __init__(self, old, new, context):
        self.old = old or {"entries": {}, "tasks": {}}
        self.new = new
        self.context = context or {}
        self.deviations = self.context.get("deviations", {})
        self.capacity = self.context.get("capacity", {})
        self.sprint = self.context.get("sprint")
        self.cutoff = new.get("cutoff", 0)
        self.predecessors = defaultdict(list)
        for first, second in new.get("edges", []):
            self.predecessors[second].append(first)
        self.old_sp = {tid: _sp_by_sprint(e) for tid, e in self.old["entries"].items()}
        self.new_sp = {tid: _sp_by_sprint(e) for tid, e in new["entries"].items()}
        self.old_hours = {tid: _hours_by_sprint(e) for tid, e in self.old["entries"].items()}
        self.new_hours = {tid: _hours_by_sprint(e) for tid, e in new["entries"].items()}

    def team(self, tid):
        task = self.new["tasks"].get(tid) or self.old["tasks"].get(tid) or {}
        return task.get("team")

    def own(self, tid):
        """Собственная причина задачи: закрытие или отклонение спринта."""
        before = self.old["entries"].get(tid, {})
        after = self.new["entries"][tid]
        if after["status"] == "done" and before.get("status") != "done":
            if self.sprint:
                text = f"Закрыта по факту спринта {self.sprint}"
                if before.get("end") and before["end"] > self.sprint:
                    text += f" — раньше плана (план: С{before['end']})"
                return text
            return "Завершена"
        if tid in self.deviations:
            return f"Отклонение в спринте {self.sprint}: {self.deviations[tid]}"
        return None

    def blockers(self, tid):
        """Блокирующие задачи, срок которых сдвинулся позже."""
        parts = []
        for pred in sorted(self.predecessors.get(tid, [])):
            was = self.old["entries"].get(pred, {}).get("end")
            now = self.new["entries"].get(pred, {}).get("end")
            if not _later(was, now):
                continue
            text = f"ждёт {pred}: её завершение сдвинулось {_sprint(was)} → {_sprint(now)}"
            if now is not None:
                text += f", поэтому начать можно не раньше С{now + 2}"
            if pred in self.deviations:
                text += f" ({pred}: {self.deviations[pred]})"
            parts.append(text)
        return parts

    def team_capacity(self, tid, later):
        team = self.team(tid)
        if team not in self.capacity:
            return None
        before, after = self.capacity[team]
        # Ёмкость — средняя фактическая производительность: она падает,
        # когда команда отстала от плана спринта. Называем отставшие задачи.
        behind = sorted(other for other in self.deviations if self.team(other) == team)
        cause = f" — команда отстала от плана спринта {self.sprint}: {', '.join(behind)}" if behind else ""
        if later and after < before - EPS:
            return f"ёмкость {team} снизилась: {before:.2f} → {after:.2f} SP за спринт{cause}"
        if not later and after > before + EPS:
            return f"ёмкость {team} выросла: {before:.2f} → {after:.2f} SP за спринт"
        return None

    def competitors(self, tid, later):
        """
        Кто занял ресурс задачи (или освободил его).

        Смотрим спринты, где задача потеряла объём (при сдвиге позже)
        или получила (при сдвиге раньше), и ищем задачу с противоположным
        изменением в том же спринте: той же команды — по SP, любой —
        по часам общей роли.
        """
        team = self.team(tid)
        old_sp, new_sp = self.old_sp.get(tid, {}), self.new_sp.get(tid, {})
        old_hours, new_hours = self.old_hours.get(tid, {}), self.new_hours.get(tid, {})
        sign = 1 if later else -1
        best = {}
        for sprint in sorted(set(old_sp) | set(new_sp)):
            if sprint <= self.cutoff:
                continue
            lost_sp = sign * (old_sp.get(sprint, 0.0) - new_sp.get(sprint, 0.0))
            lost_roles = {
                role for role in set(old_hours.get(sprint, {})) | set(new_hours.get(sprint, {}))
                if sign * (old_hours.get(sprint, {}).get(role, 0.0) - new_hours.get(sprint, {}).get(role, 0.0)) > HOURS_TOLERANCE
            }
            if lost_sp <= SP_TOLERANCE and not lost_roles:
                continue
            for other in self.new["entries"]:
                if other == tid:
                    continue
                gained_sp = sign * (self.new_sp.get(other, {}).get(sprint, 0.0) - self.old_sp.get(other, {}).get(sprint, 0.0))
                if lost_sp > SP_TOLERANCE and self.team(other) == team and gained_sp > SP_TOLERANCE:
                    key = (other, sprint, f"ёмкость {team}")
                    best[key] = max(best.get(key, 0.0), gained_sp * 10)
                for role in lost_roles:
                    gained = sign * (self.new_hours.get(other, {}).get(sprint, {}).get(role, 0.0)
                                     - self.old_hours.get(other, {}).get(sprint, {}).get(role, 0.0))
                    if gained > HOURS_TOLERANCE:
                        key = (other, sprint, f"часы роли «{role}»")
                        best[key] = max(best.get(key, 0.0), gained)
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        parts, seen = [], set()
        for (other, sprint, what), _ in ranked:
            if other in seen:
                continue
            seen.add(other)
            if later:
                text = f"в С{sprint} {what} заняла {other}"
            else:
                text = f"в С{sprint} {what} освободила {other}"
            reason = self.own(other)
            if reason and other in self.deviations:
                text += f" ({other}: {self.deviations[other]})"
            elif reason:
                text += f" ({reason.lower()})"
            parts.append(text)
            if len(parts) == 2:
                break
        return parts

    def explain(self, tid, schedule_changed):
        before = self.old["entries"].get(tid, {})
        after = self.new["entries"][tid]
        own = self.own(tid)
        if own and (after["status"] == "done" or not schedule_changed):
            return [own]
        later = _later(before.get("end"), after["end"]) or (
            before.get("end") == after["end"] and _later(before.get("start"), after["start"]))
        reasons = []
        if own:
            reasons.append(own)
        if schedule_changed and not self.old["entries"].get(tid):
            reasons.append("Новая задача в плане")
        reasons += [r[0].upper() + r[1:] for r in self.blockers(tid)] if later else []
        if schedule_changed:
            capacity = self.team_capacity(tid, later)
            if capacity:
                reasons.append(capacity[0].upper() + capacity[1:])
            if not reasons or not own:
                reasons += [r[0].upper() + r[1:] for r in self.competitors(tid, later)]
        if after["status"] == "beyond" and before.get("status") == "planned":
            reasons.insert(0, f"Ушла за квартал: прогноз {_sprint(after['end'])}")
        elif after["status"] == "planned" and before.get("status") in ("beyond", "blocked", "partial"):
            reasons.insert(0, f"Вернулась в квартал: завершение {_sprint(after['end'])}")
        return reasons


def diff_plans(old, new, cause="Изменились ограничения", context=None):
    """
    Изменения плана с причиной у каждой задачи.

    cause — общее условие пересчёта (факт спринта, отпуск, помощь,
    ручное изменение); context — что известно о пересчёте по факту:
    sprint, deviations (задача → отклонение спринта), capacity
    (команда → (было, стало) SP за спринт).
    """

    explainer = _Explainer(old, new, context)
    fact = bool(context and context.get("sprint"))
    cutoff = new.get("cutoff", 0)
    changes = []
    for tid, after in new["entries"].items():
        before = old["entries"].get(tid, {}) if old else {}
        # Работа, начатая в закрытом спринте, продолжается со следующего:
        # это не сдвиг начала. И закрытые спринты в сравнении не участвуют.
        signature = lambda entry: [f for f in _work_signature(entry) if f[0] > cutoff]
        assignments_changed = signature(before) != signature(after)
        start = before.get("start")
        if start is not None and start <= cutoff and before.get("status") != "done":
            start = cutoff + 1
        schedule_changed = (start, before.get("end"), before.get("status")) != (
            after["start"], after["end"], after["status"])
        if not assignments_changed and not schedule_changed:
            continue
        reasons = explainer.explain(tid, schedule_changed)
        if not schedule_changed and not reasons:
            reasons = ["Изменились исполнители или объём работы по спринтам, срок прежний"]
        if not fact:
            # Общее условие пересчёта — главная причина: отпуск, помощь, ручное изменение.
            reasons = [cause] + reasons
        elif not reasons:
            reasons = [cause]
        if after["reasons"] and after["status"] in ("beyond", "blocked", "partial"):
            reasons.append(after["reasons"][-1])
        if after["status"] == "done" and before.get("status") != "done":
            kind = "done"
        else:
            kind = "schedule" if schedule_changed else "assignments"
        changes.append(dict(
            task_id=tid, initiative=new["tasks"][tid]["initiative"],
            kind=kind,
            before={k: before.get(k) for k in ("start", "end", "status")},
            after={k: after[k] for k in ("start", "end", "status")},
            assignments_changed=assignments_changed,
            reasons=reasons,
            reason=". ".join(r.rstrip(".") for r in reasons) + "."))
    order = {"schedule": 0, "done": 1, "assignments": 2}
    return sorted(changes, key=lambda c: (order[c["kind"]], c["task_id"]))
