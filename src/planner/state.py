from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from src.dataset import Dataset
from src.planner.config import InputConfig


OPEN_STATUSES = ("ToDo", "InProgress")

# Погрешность сравнения часов и долей.
EPS = 1e-6


@dataclass(frozen=True)
class TaskInput:
    """
    Открытая задача на входе планировщика.

    Смета — сумма часов по ролям из калькулятора,
    а не «Итого» и не estimated_hh.
    """

    task_id: str
    team: str
    initiative: str
    status: str
    rung: float
    sp: float
    sp_effective: float
    original_hours: dict[str, float]
    remaining_hours: dict[str, float]

    @property
    def total_remaining(self) -> float:
        return sum(self.remaining_hours.values())

    @property
    def zero_remaining(self) -> bool:
        """
        Задача в работе, у которой не осталось часов.
        Закрывается в первом спринте без затрат.
        """

        return (
            self.status == "InProgress"
            and self.total_remaining <= EPS
        )


@dataclass(frozen=True)
class InitiativeInput:
    """
    Инициатива PRODF: все её задачи, включая закрытые.
    """

    initiative_id: str
    rung: float
    open_task_ids: tuple[str, ...]
    done_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class EngineerInput:
    """
    Одна строка распределения инженера в команду.
    """

    engineer_id: str
    team: str
    role: str
    capacity_rate: float


@dataclass(frozen=True)
class PlanningState:
    """
    Всё, что нужно планировщику, в готовом виде.

    Задачи, команды и роли хранятся отсортированными,
    чтобы порядок обхода не зависел от порядка строк в файле.
    """

    input_config: InputConfig
    current_sprint: int
    tasks: dict[str, TaskInput]
    done_tasks: dict[str, int]
    initiatives: dict[str, InitiativeInput]
    team_capacity: dict[str, float]
    team_role_fund: dict[tuple[str, str], float]
    engineers: tuple[EngineerInput, ...]
    dependencies: tuple[tuple[str, str], ...]
    missing_roles: frozenset[str]
    warnings: tuple[str, ...] = field(default=())
    reason_codes: tuple[str, ...] = field(default=())
    goal_options: tuple[str, ...] = field(default=())

    # Фонд роли в конкретном спринте. Пустой словарь означает, что фонд
    # одинаков во всех спринтах и берётся из team_role_fund. Заполняется,
    # когда у инженеров есть персональная доступность по спринтам или
    # часы передаются между командами на часть квартала.
    sprint_role_fund: dict[tuple[str, str, int], float] = field(
        default_factory=dict
    )

    # Потолок часов по всем ролям сразу за один спринт: ключ — пара
    # «область, спринт», где область — команда или "*" для организации.
    # Нужен, когда один человек числится в нескольких ролях: его часы
    # попадают в фонд каждой роли, и без этого потолка план может
    # потратить одного человека дважды. Пустой словарь — ограничения нет.
    hours_limit: dict[tuple[str, int], float] = field(default_factory=dict)

    # Спринт, раньше которого задачу нельзя начинать: решение главы
    # команды, а не следствие зависимостей. Задача без записи начинается
    # с первого спринта горизонта.
    not_before: dict[str, int] = field(default_factory=dict)

    @property
    def quarter(self) -> int:
        return self.input_config.quarter_sprints

    @property
    def horizon(self) -> int:
        return self.input_config.horizon_sprints

    @property
    def sprints(self) -> range:
        """
        Номера спринтов горизонта, ещё не прошедших.
        """

        return range(self.current_sprint + 1, self.horizon + 1)

    @property
    def teams(self) -> list[str]:
        return sorted(self.team_capacity)

    @property
    def roles(self) -> list[str]:
        roles = {
            role
            for task in self.tasks.values()
            for role in task.remaining_hours
        }

        roles |= {
            role
            for (_, role) in self.team_role_fund
        }

        roles |= {
            role
            for (_, role, _) in self.sprint_role_fund
        }

        return sorted(roles)

    def predecessors(self, task_id: str) -> list[str]:
        return sorted(
            blocking
            for blocking, blocked in self.dependencies
            if blocked == task_id
        )

    def successors(self, task_id: str) -> list[str]:
        return sorted(
            blocked
            for blocking, blocked in self.dependencies
            if blocking == task_id
        )

    def team_fund(
        self,
        team: str,
        role: str,
        sprint: int | None = None,
    ) -> float:
        """
        Часы роли в команде за один спринт.

        Без указания спринта возвращается базовый фонд. С указанием —
        значение из sprint_role_fund, если оно там есть, иначе базовый:
        спринты без персональной доступности работают по общему правилу.
        """

        if sprint is not None:
            override = self.sprint_role_fund.get((team, role, sprint))

            if override is not None:
                return override

        return self.team_role_fund.get((team, role), 0.0)

    def global_fund(self, role: str, sprint: int | None = None) -> float:
        """
        Часы роли во всей организации за один спринт.
        """

        teams = {team for (team, _) in self.team_role_fund}

        if sprint is not None:
            teams |= {
                team
                for (team, fund_role, _) in self.sprint_role_fund
                if fund_role == role
            }

        return sum(
            self.team_fund(team, role, sprint)
            for team in teams
        )

    def peak_team_fund(self, team: str, role: str) -> float:
        """
        Наибольший фонд роли по спринтам горизонта.

        Отвечает на вопрос «выполнима ли работа в принципе»:
        если роли нет ни в одном спринте, задача невозможна при любом плане.
        """

        return max(
            (self.team_fund(team, role, sprint) for sprint in self.sprints),
            default=self.team_fund(team, role),
        )

    def peak_global_fund(self, role: str) -> float:
        return max(
            (self.global_fund(role, sprint) for sprint in self.sprints),
            default=self.global_fund(role),
        )

    def window_team_fund(self, team: str, role: str, sprints) -> float:
        """
        Сумма фонда роли по набору спринтов.

        Для агрегатов за квартал: при переменном фонде произведение
        «фонд × число спринтов» неверно.
        """

        return sum(self.team_fund(team, role, sprint) for sprint in sprints)

    def window_global_fund(self, role: str, sprints) -> float:
        return sum(self.global_fund(role, sprint) for sprint in sprints)

    def sprint_hours_limit(self, scope: str, sprint: int) -> float | None:
        """
        Потолок часов по всем ролям области за спринт; None — не задан.
        """

        return self.hours_limit.get((scope, sprint))



def build_state(
    dataset: Dataset,
    config: InputConfig,
    current_sprint: int = 0,
) -> PlanningState:
    """
    Готовит вход планировщика из загруженного датасета.

    Принимает только InputConfig: правила решения
    на входные числа не влияют и передаются в plan.
    """

    if not isinstance(config, InputConfig):
        raise TypeError(
            f"build_state ожидает InputConfig, получен {type(config).__name__}"
        )

    tasks_table = dataset.tasks.sort_values("task_id")

    original = _hours_by_task_role(
        dataset.estimates,
        "estimated_hours",
    )

    spent = _hours_by_task_role(
        dataset.spent_hours,
        "spent_hours",
    )

    warnings: list[str] = []
    tasks: dict[str, TaskInput] = {}
    done_tasks: dict[str, int] = {}
    members: dict[str, list[tuple[str, str, float]]] = defaultdict(list)

    for _, row in tasks_table.iterrows():
        task_id = row["task_id"]
        initiative = str(row["Номер инициативы"]).strip()
        rung = _number(row["rung"])
        members[initiative].append((task_id, row["status"], rung))

        if row["status"] == "Done":
            # Закрытые задачи в модель не входят,
            # но открывают зависимости и засчитываются в инициативу.
            done_tasks[task_id] = 0
            continue

        if row["status"] not in OPEN_STATUSES:
            continue

        task_original = original.get(task_id, {})
        task_spent = spent.get(task_id, {})

        remaining = {
            role: hours - task_spent.get(role, 0.0)
            for role, hours in task_original.items()
            if hours - task_spent.get(role, 0.0) > EPS
        }

        sp = _number(row["estimation_sp"])

        sp_effective = _effective_sp(
            sp=sp,
            status=row["status"],
            original=task_original,
            remaining=remaining,
            config=config,
        )

        task = TaskInput(
            task_id=task_id,
            team=row["team_id"],
            initiative=initiative,
            status=row["status"],
            rung=rung,
            sp=sp,
            sp_effective=sp_effective,
            original_hours=dict(sorted(task_original.items())),
            remaining_hours=dict(sorted(remaining.items())),
        )

        if task.zero_remaining:
            warnings.append(
                f"{task_id}: задача в статусе InProgress без остатка часов, "
                f"вероятно, не переведена в Done"
            )

        tasks[task_id] = task

    initiatives = _build_initiatives(members, config)

    history = dataset.team_history

    team_capacity = {
        team: float(velocity) * config.focus_factor * config.capacity_scale
        for team, velocity in (
            history.groupby("team_id")["velocity_achieved"]
            .mean()
            .sort_index()
            .items()
        )
    }

    engineers = tuple(
        EngineerInput(
            engineer_id=row["engineer_id"],
            team=row["team_id"],
            role=row["role"],
            capacity_rate=float(row["capacity_rate"]),
        )
        for _, row in (
            dataset.engineers
            .sort_values(["engineer_id", "team_id"])
            .iterrows()
        )
    )

    team_role_fund: dict[tuple[str, str], float] = defaultdict(float)

    for engineer in engineers:
        team_role_fund[(engineer.team, engineer.role)] += (
            engineer.capacity_rate * config.hours_per_sprint
        )

    engineer_roles = {engineer.role for engineer in engineers}

    missing_roles = frozenset(
        role
        for task in tasks.values()
        for role in task.remaining_hours
        if role not in engineer_roles
    )

    dependencies = tuple(
        sorted(
            set(
                zip(
                    dataset.dependencies["blocking_task_id"],
                    dataset.dependencies["blocked_task_id"],
                )
            )
        )
    )

    known = set(tasks) | set(done_tasks)

    for blocking, blocked in dependencies:
        if blocking not in known or blocked not in known:
            warnings.append(
                f"Связь {blocking} → {blocked} ссылается на неизвестную задачу "
                f"и не учитывается"
            )

    dependencies = tuple(
        (blocking, blocked)
        for blocking, blocked in dependencies
        if blocking in known and blocked in known
    )

    return PlanningState(
        input_config=config,
        current_sprint=current_sprint,
        tasks=tasks,
        done_tasks=done_tasks,
        initiatives=initiatives,
        team_capacity=team_capacity,
        team_role_fund=dict(sorted(team_role_fund.items())),
        engineers=engineers,
        dependencies=dependencies,
        missing_roles=missing_roles,
        warnings=tuple(warnings),
        reason_codes=tuple(
            dataset.reference_lists.get("variance_reasons", [])
        ),
        goal_options=tuple(
            dataset.reference_lists.get("goal_options", [])
        ),
    )


def _number(value: object) -> float:
    """
    Число из ячейки; пустое или нечисловое значение — ноль.
    """

    number = pd.to_numeric(value, errors="coerce")

    if pd.isna(number):
        return 0.0

    return float(number)


def _hours_by_task_role(
    table: pd.DataFrame,
    column: str,
) -> dict[str, dict[str, float]]:
    """
    Сумма часов по паре «задача — роль».
    """

    result: dict[str, dict[str, float]] = defaultdict(dict)

    grouped = (
        table.groupby(["task_id", "role"])[column]
        .sum()
    )

    for (task_id, role), hours in grouped.items():
        if hours > 0:
            result[task_id][role] = float(hours)

    return result


def _effective_sp(
    sp: float,
    status: str,
    original: dict[str, float],
    remaining: dict[str, float],
    config: InputConfig,
) -> float:
    """
    Очки, которые задача спишет из ёмкости при 100 % прогресса.

    При inprogress_sp=by_hours задача в работе стоит долю оценки,
    пропорциональную остатку часов.
    """

    if status != "InProgress" or config.inprogress_sp == "full":
        return sp

    total = sum(original.values())

    if total <= EPS:
        return sp

    return sp * sum(remaining.values()) / total


def _build_initiatives(
    members: dict[str, list[tuple[str, str, float]]],
    config: InputConfig,
) -> dict[str, InitiativeInput]:
    """
    Собирает инициативы и их rung по правилу initiative_rung.
    """

    initiatives = {}

    for initiative in sorted(members):
        rows = members[initiative]
        rungs = [rung for _, _, rung in rows]

        if config.initiative_rung == "max":
            rung = max(rungs)
        else:
            rung = sum(rungs) / len(rungs)

        initiatives[initiative] = InitiativeInput(
            initiative_id=initiative,
            rung=rung,
            open_task_ids=tuple(
                task_id
                for task_id, status, _ in rows
                if status in OPEN_STATUSES
            ),
            done_task_ids=tuple(
                task_id
                for task_id, status, _ in rows
                if status == "Done"
            ),
        )

    return initiatives
