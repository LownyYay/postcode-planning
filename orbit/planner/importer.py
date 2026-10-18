"""Чтение официального XLSX через импортёр и валидатор ядра.

Разбор листа, нормализация ролей и проверка качества данных живут
в src/: сервер и консольный расчёт читают Excel одним кодом. Здесь —
перевод таблиц ядра в состояние ОРБИТЫ и проверки, которые нужны
только серверу: остаток часов у задач в работе и направление связей.
"""

import hashlib
import math
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pandas as pd

from src.dataset import SHEET_NAME, dataset_from_raw
from src.normalizer import clean_role, load_role_aliases, normalize_role, role_key
from src.validator import validate_dataset


class InputError(ValueError):
    """Понятная пользователю ошибка входных данных."""


# Ошибки валидатора ядра, при которых план строить нельзя: без них
# задача или инженер читаются неоднозначно.
FATAL_CODES = {
    "DUPLICATE_TASK_ID",
    "INVALID_TASK_STATUS",
    "INVALID_CAPACITY_RATE",
    "ENGINEER_OVERALLOCATED",
}

DEPENDENCY_KINDS = ("has to be done before", "is required for", "depends on")


def number(value, label="число", default=None):
    if value is None or value == "":
        if default is not None:
            return float(default)
        raise InputError(f"Не заполнено: {label}")
    if isinstance(value, bool):
        raise InputError(f"{label}: ожидается число")
    try:
        result = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        raise InputError(f"{label}: неверное число {value!r}") from None
    if not math.isfinite(result) or result < 0:
        raise InputError(f"{label}: требуется конечное неотрицательное число")
    return result


@lru_cache(maxsize=1)
def _aliases():
    return load_role_aliases()


def role_name(value, known=()):
    """
    Имя роли так, как его пишет ядро.

    Сначала словарь config/aliases.yaml, затем уже известные роли:
    написание, совпадающее с известной ролью по ключу src.normalizer.role_key
    (без регистра, пробелов, «_», дефисов и точек), становится ею.
    «Дев_опс», «Дев опс» и «ДевОпс» — это «Девопс».
    """
    text = clean_role(value or "")
    named = normalize_role(text, _aliases())
    if named != text:
        return named
    key = role_key(text)
    return next((role for role in sorted(known) if role_key(role) == key), text)


def iso(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def issue(code, entity, message, level="warning", action="Проверьте исходные данные"):
    return dict(code=code, entity=entity, message=message, level=level, action=action)


def _text(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def _value(value, label, default=None):
    return number(None if _text(value) == "" else value, label, default)


def _read_sheet(source):
    """Лист датасета из файла или байтов, с защитой от огромных ZIP."""
    if not isinstance(source, bytes):
        source = Path(source).read_bytes()
    try:
        with ZipFile(BytesIO(source)) as archive:
            if sum(f.file_size for f in archive.infolist()) > 60_000_000:
                raise InputError("Распакованный Excel превышает 60 МБ")
    except BadZipFile:
        raise InputError("Файл не является XLSX") from None
    try:
        book = pd.ExcelFile(BytesIO(source), engine="openpyxl")
        sheet = SHEET_NAME if SHEET_NAME in book.sheet_names else book.sheet_names[0]
        raw = book.parse(sheet, header=None)
    except Exception as exc:
        raise InputError(f"Не удалось прочитать Excel: {exc}") from None
    if raw.shape[0] > 10000 or raw.shape[1] > 1000:
        raise InputError("Слишком большой лист: максимум 10 000 строк и 1 000 столбцов")
    return source, sheet, raw


def _core_issues(dataset):
    issues = []
    for found in validate_dataset(dataset.tasks, dataset.estimates, dataset.dependencies, dataset.engineers):
        if found.level == "ERROR" and found.code in FATAL_CODES:
            raise InputError(found.message)
        issues.append(issue(found.code.lower(), found.entity_id or "Датасет", found.message,
                            "error" if found.level == "ERROR" else "warning"))
    return issues


def import_xlsx(source, name="Официальный датасет"):
    """Официальный однолистовой датасет → состояние ОРБИТЫ."""
    content, sheet, raw = _read_sheet(source)
    try:
        dataset = dataset_from_raw(raw)
    except InputError:
        raise
    except Exception as exc:
        raise InputError(f"Не удалось разобрать датасет: {exc}") from None
    issues = _core_issues(dataset)

    estimates = {}
    for row in dataset.estimates.itertuples(index=False):
        roles = estimates.setdefault(str(row.task_id), {})
        roles[row.role] = roles.get(row.role, 0.0) + float(row.estimated_hours)
    spent = {}
    for row in dataset.spent_hours.itertuples(index=False):
        roles = spent.setdefault(str(row.task_id), {})
        roles[row.role] = roles.get(row.role, 0.0) + float(row.spent_hours)

    tasks = {}
    for record in dataset.tasks.to_dict("records"):
        tid = _text(record["task_id"])
        if not _text(record.get("team_id")) or not _text(record.get("Номер инициативы")):
            raise InputError(f"{tid}: не указаны команда или инициатива")
        tasks[tid] = {
            "id": tid, "initiative": _text(record["Номер инициативы"]),
            "title": _text(record.get("summary")) or tid, "team": _text(record["team_id"]),
            "parent": _text(record.get("parent_id")), "status": record["status"],
            "priority": _value(record.get("rung"), f"{tid}: rung", 0),
            "sp": _value(record.get("estimation_sp"), f"{tid}: estimation_sp"),
            "original_hh": _value(record.get("estimated_hh"), f"{tid}: estimated_hh", 0),
            "hours": {r: h for r, h in estimates.get(tid, {}).items() if h},
            "spent": {r: h for r, h in spent.get(tid, {}).items() if h},
            "required_skills": {},
            "planned_start": iso(record.get("planned_start")), "planned_end": iso(record.get("planned_end")),
            "actual_end": iso(record.get("actual_end")),
            "result": _text(record.get("итоговый результат")),
            "desired_result": _text(record.get("плановый результат")) or _text(record.get("заказчик")),
            "source_row": None,
        }

    for task in tasks.values():
        tid = task["id"]
        if task["sp"] <= 0 and task["status"] != "Done":
            issues.append(issue("zero_sp", tid, "У незавершённой задачи нет положительной оценки SP", "error"))
        for role, h in task["spent"].items():
            if h > task["hours"].get(role, 0):
                issues.append(issue("overrun", tid, f"{role}: потрачено {h:g} ч, оценка {task['hours'].get(role, 0):g} ч",
                                    action="Подтвердите оставшиеся часы"))
        if task["status"] == "InProgress" and task["hours"] and not any(
                h > task["spent"].get(r, 0) for r, h in task["hours"].items()):
            issues.append(issue("remaining_unknown", tid, "Оценка часов исчерпана, но задача не завершена", "error",
                                "Укажите остаток SP и часов в карточке"))

    dependencies = []
    for row in dataset.dependencies.itertuples(index=False):
        first, second, kind = _text(row.blocking_task_id), _text(row.blocked_task_id), _text(row.dependency_type)
        if kind not in DEPENDENCY_KINDS:
            raise InputError(f"Связь {first} / {second}: неизвестный тип зависимости {kind}")
        dependencies.append(dict(first=first, second=second, kind=kind, source_row=None))
    if any(d["kind"] == "depends on" for d in dependencies):
        issues.append(issue("dependency_direction", "Task_dependencies",
            "Связи читаются по столбцам: первая задача блокирует вторую при любом типе, включая depends on. "
            "На этом датасете такое чтение не противоречит ни одному факту Done, а разворот depends on противоречит.",
            action="При противоречии фактам проверьте направление исходной связи"))

    people = {}
    for record in dataset.engineers.to_dict("records"):
        eid, team = _text(record["engineer_id"]), _text(record.get("team_id"))
        if not team:
            raise InputError(f"{eid}: отсутствует команда")
        person = people.setdefault(eid, dict(id=eid, name=eid, roles=[], skills=[],
                                             grade=_text(record.get("grade")), allocations={}))
        role = _text(record.get("role"))
        if role and role not in person["roles"]:
            person["roles"].append(role)
        for skill in record.get("skills") or []:
            if skill not in person["skills"]:
                person["skills"].append(skill)
        if team in person["allocations"]:
            raise InputError(f"{eid}: повторное распределение в {team}")
        person["allocations"][team] = _value(record.get("capacity_rate"), f"{eid}: capacity_rate")

    history = [dict(team=_text(r["team_id"]), date=iso(r["snapshot_date"]),
                    achieved=_value(r.get("velocity_achieved"), "velocity_achieved"),
                    planned=_value(r.get("planned_sp"), "planned_sp", 0))
               for r in dataset.team_history.to_dict("records")]

    return dict(name=name, kind="official", tasks=list(tasks.values()), engineers=list(people.values()),
                dependencies=dependencies, history=history, issues=issues,
                references={k: list(v) for k, v in dataset.reference_lists.items()},
                source=dict(sha256=hashlib.sha256(content).hexdigest(), sheet=sheet, tasks=len(tasks),
                            estimate_tasks=len(estimates),
                            engineer_rows=sum(len(p["allocations"]) for p in people.values())))
