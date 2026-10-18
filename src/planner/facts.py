"""
Факт спринта и его применение к входному состоянию.

Формат файла факта — таблица с колонками:
sprint, task_id, role, spent_hours, status.
Одна строка — часы роли по задаче за спринт; status — состояние задачи
на конец спринта: ToDo, InProgress или Done. Строка без роли и часов
допустима, если нужно только сообщить статус.
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace

import pandas as pd

from src.normalizer import normalize_role
from src.planner.state import EPS, PlanningState, TaskInput


FACT_COLUMNS = ("sprint", "task_id", "role", "spent_hours", "status")
FACT_STATUSES = ("ToDo", "InProgress", "Done")

# Пока задача не закрыта, накопленный прогресс не больше 0.9.
MAX_OPEN_PROGRESS = 0.9

# Если задача не закрыта, а часы кончились, остаток по каждой роли —
# не меньше этой доли сметы.
MIN_REMAINING_SHARE = 0.1


@dataclass(frozen=True)
class SprintFact:
    """
    Факт одного спринта.

    hours — часы по паре «задача, роль» за этот спринт;
    closed — задачи, закрытые (Done) в этом спринте.
    """

    sprint: int
    hours: dict[tuple[str, str], float] = field(default_factory=dict)
    closed: frozenset[str] = field(default_factory=frozenset)

    def task_hours(self, task_id: str) -> dict[str, float]:
        return {
            role: hours
            for (fact_task, role), hours in self.hours.items()
            if fact_task == task_id
        }


class FactError(ValueError):
    """
    Файл факта не соответствует формату или состоянию плана.
    """


def load_sprint_fact(
    table: pd.DataFrame,
    role_aliases: dict[str, str] | None = None,
) -> SprintFact:
    """
    Читает факт одного спринта из таблицы.
    """

    missing = [column for column in FACT_COLUMNS if column not in table.columns]

    if missing:
        raise FactError(f"В факте спринта нет колонок: {', '.join(missing)}")

    sprints = sorted(set(pd.to_numeric(table["sprint"], errors="coerce").dropna().astype(int)))

    if len(sprints) != 1:
        raise FactError(f"Файл факта должен описывать ровно один спринт, найдено: {sprints}")

    hours: dict[tuple[str, str], float] = defaultdict(float)
    statuses: dict[str, str] = {}

    for _, row in table.iterrows():
        task_id = str(row["task_id"]).strip()
        status = str(row["status"]).strip()

        if status not in FACT_STATUSES:
            raise FactError(f"{task_id}: неизвестный статус «{status}»")

        if statuses.setdefault(task_id, status) != status:
            raise FactError(f"{task_id}: разные статусы в строках одного спринта")

        if pd.isna(row["role"]) or str(row["role"]).strip() == "":
            continue

        spent = pd.to_numeric(row["spent_hours"], errors="coerce")

        if pd.isna(spent) or spent < 0:
            raise FactError(f"{task_id}: некорректные часы «{row['spent_hours']}»")

        role = normalize_role(row["role"], role_aliases or {})
        hours[(task_id, role)] += float(spent)

    return SprintFact(
        sprint=sprints[0],
        hours=dict(sorted(hours.items())),
        closed=frozenset(
            task_id
            for task_id, status in statuses.items()
            if status == "Done"
        ),
    )


def work_share(
    remaining: dict[str, float],
    spent: dict[str, float],
    external: frozenset[str] | set[str],
) -> float:
    """
    Доля работы задачи, сделанная за спринт.

    Считается по ролям штата: потраченные часы этих ролей к их остатку
    на начало спринта. Роли без носителей (external) выполняются вне
    штата, их часов в факте обычно нет — если бы они входили в долю,
    задача, сделанная ровно по плану, выглядела бы отстающей. Часы ролей,
    которых нет в остатке, — перерасход, а не прогресс, в долю не входят.
    У задачи только с внештатными ролями доля считается по всем ролям.
    """

    staffed = {role: hours for role, hours in remaining.items() if role not in external}
    base = staffed or remaining
    total = sum(base.values())
    done = sum(hours for role, hours in spent.items() if role in base)

    if total <= EPS:
        return 1.0 if done > EPS else 0.0

    return min(done / total, 1.0)


def spend_hours(
    remaining: dict[str, float],
    spent: dict[str, float],
    external: frozenset[str] | set[str],
) -> dict[str, float]:
    """
    Остаток по ролям после часов спринта.

    Роль штата уменьшается на свои потраченные часы. Внештатная роль,
    по которой часов нет, уменьшается в той же доле, в какой продвинулась
    работа штата: внешние исполнители идут вместе с командой.
    """

    share = work_share(remaining, spent, external)
    result = {}

    for role, hours in remaining.items():
        if role in spent:
            left = hours - spent[role]
        elif role in external:
            left = hours * (1 - share)
        else:
            left = hours

        if left > EPS:
            result[role] = left

    return result


@dataclass(frozen=True)
class TaskProgress:
    """
    Как факт изменил задачу: остаток по ролям, накопленный прогресс
    в долях базового объёма и спринт закрытия.
    """

    remaining_hours: dict[str, float]
    progress: float
    closed_in: int | None
    credited_sp: dict[int, float]
    exhausted: bool


def track_progress(
    baseline_state: PlanningState,
    facts: list[SprintFact],
) -> dict[str, TaskProgress]:
    """
    Проходит факты по порядку и считает для каждой задачи
    остаток, прогресс и зачтённые SP по спринтам.

    Прогресс за спринт = фактические часы ролей штата / их остаток
    на начало спринта (work_share). Накопленный прогресс открытой задачи
    не больше 0.9; при закрытии засчитывается весь остаток базовых SP.
    """

    _check_sequence(baseline_state, facts)

    external = baseline_state.missing_roles
    result = {}

    for task_id, task in baseline_state.tasks.items():
        remaining = dict(task.remaining_hours)
        progress = 0.0
        closed_in = None
        credited: dict[int, float] = {}
        exhausted = False

        for fact in facts:
            if closed_in is not None:
                break

            if task_id in fact.closed:
                closed_in = fact.sprint
                credited[fact.sprint] = task.sp_effective * (1 - progress)
                progress = 1.0
                break

            spent = fact.task_hours(task_id)

            if sum(spent.values()) <= EPS:
                continue

            share = work_share(remaining, spent, external)
            new_progress = min(
                progress + (1 - progress) * share,
                MAX_OPEN_PROGRESS,
            )

            credited[fact.sprint] = task.sp_effective * (new_progress - progress)
            progress = new_progress
            remaining = spend_hours(remaining, spent, external)

            if not remaining:
                # Часы кончились, а задача не закрыта: остаток по каждой
                # роли — 10 % сметы. Это допущение, оно описано в README.
                exhausted = True
                remaining = {
                    role: hours * MIN_REMAINING_SHARE
                    for role, hours in task.original_hours.items()
                }

        result[task_id] = TaskProgress(
            remaining_hours=dict(sorted(remaining.items())),
            progress=progress,
            closed_in=closed_in,
            credited_sp=credited,
            exhausted=exhausted,
        )

    return result


def apply_facts(
    baseline_state: PlanningState,
    facts: list[SprintFact],
) -> PlanningState:
    """
    Состояние после фактов: закрытые задачи уходят из модели,
    у открытых — новый остаток по ролям и новые SP, текущий спринт — k.
    """

    if not facts:
        return baseline_state

    tracked = track_progress(baseline_state, facts)
    remaining_from = baseline_state.input_config.remaining_from

    tasks: dict[str, TaskInput] = {}
    done_tasks = dict(baseline_state.done_tasks)
    warnings = list(baseline_state.warnings)

    for task_id, task in baseline_state.tasks.items():
        progress = tracked[task_id]

        if progress.closed_in is not None:
            done_tasks[task_id] = progress.closed_in
            continue

        base_total = task.total_remaining

        if remaining_from == "hours" and base_total > EPS:
            sp_effective = task.sp_effective * sum(progress.remaining_hours.values()) / base_total
        else:
            sp_effective = task.sp_effective * (1 - progress.progress)

        if progress.exhausted:
            warnings.append(
                f"{task_id}: часы по смете израсходованы, а задача не закрыта; "
                f"остаток принят равным 10 % сметы по каждой роли"
            )

        tasks[task_id] = replace(
            task,
            status="InProgress" if progress.progress > 0 else task.status,
            sp_effective=sp_effective,
            remaining_hours=progress.remaining_hours,
        )

    initiatives = {
        initiative_id: replace(
            initiative,
            open_task_ids=tuple(
                task_id for task_id in initiative.open_task_ids
                if task_id in tasks
            ),
            done_task_ids=tuple(
                sorted(
                    set(initiative.done_task_ids)
                    | {
                        task_id for task_id in initiative.open_task_ids
                        if task_id not in tasks
                    }
                )
            ),
        )
        for initiative_id, initiative in baseline_state.initiatives.items()
    }

    return replace(
        baseline_state,
        current_sprint=max(fact.sprint for fact in facts),
        tasks=tasks,
        done_tasks=done_tasks,
        initiatives=initiatives,
        warnings=tuple(warnings),
    )


def _check_sequence(state: PlanningState, facts: list[SprintFact]) -> None:
    sprints = [fact.sprint for fact in facts]
    expected = list(range(state.current_sprint + 1, state.current_sprint + 1 + len(facts)))

    if sprints != expected:
        raise FactError(
            f"Факты должны идти подряд со спринта {state.current_sprint + 1}: "
            f"ожидались {expected}, получены {sprints}"
        )

    known = set(state.tasks)

    for fact in facts:
        unknown = sorted(
            {task_id for task_id, _ in fact.hours} | set(fact.closed)
        )
        unknown = [task_id for task_id in unknown if task_id not in known]

        if unknown:
            raise FactError(
                f"Спринт {fact.sprint}: задачи {', '.join(unknown)} "
                f"не открыты в базовом плане"
            )
