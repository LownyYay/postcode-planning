import json
from dataclasses import asdict, dataclass, field


TASK_STATUSES = ("included", "postponed", "cancelled")

# Коды причин — значения справочника датасета
# «Причины расхождения планов заказчика и исполнителя».
REASON_NO_RESOURCES = "Отсутствие ресурсов"
REASON_NOT_READY = "Отсутствует готовность смежных команд"
REASON_OVER_PLAN = "Превышение плана"

# Значение справочника «Итог разработки» для перенесённых работ.
GOAL_POSTPONED = "не будет взято в квартал"

# optimal — доказанный оптимум MILP; greedy — жадный план по запросу;
# fallback_greedy — решатель не дал оптимума, показан жадный план.
RUN_STATUSES = (
    "optimal",
    "fallback_greedy",
    "greedy",
)


@dataclass(frozen=True)
class Reason:
    """
    Причина решения, записанная в момент решения.

    code — значение справочника датасета,
    text — простым языком для интерфейса,
    details — какое ограничение сработало и с какими числами.
    """

    code: str
    text: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class TaskPlan:
    """
    Решение по одной открытой задаче.

    included — завершается в квартале;
    postponed — выполнима, но проиграла приоритетам,
    прогноз завершения лежит за пределами квартала;
    cancelled — невозможна при любом плане.
    """

    task_id: str
    team: str
    initiative: str
    status: str
    progress: dict[int, float] = field(default_factory=dict)
    sprint_start: int | None = None
    sprint_end: int | None = None
    forecast_sprint: int | None = None
    sp_by_sprint: dict[int, float] = field(default_factory=dict)
    hours_by_sprint: dict[int, dict[str, float]] = field(default_factory=dict)
    reason_code: str | None = None
    reason_text: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    counterfactual: dict[str, object] | None = None


@dataclass
class InitiativePlan:
    """
    Итог по инициативе PRODF.

    partial — в квартале по инициативе идёт работа,
    но завершить её целиком не удаётся.
    """

    initiative_id: str
    rung: float
    done_in_quarter: bool
    forecast_sprint: int | None
    open_task_ids: list[str]
    blocking_task_ids: list[str]
    partial: bool = False
    reason_text: str | None = None
    counterfactual: dict[str, object] | None = None


@dataclass
class LoadCell:
    planned: float
    capacity: float

    @property
    def utilization(self) -> float | None:
        if self.capacity <= 0:
            return None

        return self.planned / self.capacity


@dataclass
class Bottleneck:
    """
    Ограничение, выбранное на 100 %.
    kind: team_sp — очки команды, role_hours — часы роли.
    """

    kind: str
    key: str
    sprint: int
    planned: float
    capacity: float


@dataclass
class Load:
    team_sp: dict[str, dict[int, LoadCell]] = field(default_factory=dict)
    role_hours: dict[str, dict[int, LoadCell]] = field(default_factory=dict)
    team_role_hours: dict[str, dict[str, dict[int, LoadCell]]] = field(
        default_factory=dict
    )
    bottlenecks: list[Bottleneck] = field(default_factory=list)


@dataclass
class BorrowRow:
    """
    Часы роли, которых команде не хватило своих
    и которые взяты из общего фонда организации.
    """

    team: str
    role: str
    sprint: int
    hours: float


@dataclass
class DemandRow:
    """
    Спрос задач, готовых к работе в следующем спринте,
    против фонда роли. Основа оранжевого алерта.
    """

    sprint: int
    team: str
    role: str
    ready_hours: float
    planned_hours: float
    fund_hours: float


@dataclass
class Change:
    """
    Сдвиг задачи или инициативы при пересчёте.

    baseline — спринт завершения в первоначальном плане,
    previous — в предыдущем пересчёте, current — сейчас.
    Пустое значение означает «не завершается в горизонте».
    """

    kind: str
    id: str
    baseline: int | None
    previous: int | None
    current: int | None
    status: str

    @property
    def shift_vs_baseline(self) -> int | None:
        if self.baseline is None or self.current is None:
            return None

        return self.current - self.baseline


@dataclass
class Meta:
    """
    Как получен план. input_config и solve_config записаны целиком:
    по ним план можно воспроизвести.
    """

    engine: str
    solver: str | None
    status: str
    elapsed_seconds: float
    current_sprint: int
    input_config: dict[str, object]
    solve_config: dict[str, object]
    objective: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fallback_reason: str | None = None


@dataclass
class PlanResult:
    tasks: dict[str, TaskPlan]
    initiatives: dict[str, InitiativePlan]
    load: Load
    borrowing: list[BorrowRow]
    next_sprint_demand: list[DemandRow]
    meta: Meta
    # Заполняется при пересчёте: сравнение с базой и предыдущим планом.
    changes: list[Change] = field(default_factory=list)

    @property
    def initiatives_done(self) -> int:
        return sum(
            plan.done_in_quarter
            for plan in self.initiatives.values()
        )

    @property
    def included_task_ids(self) -> list[str]:
        return sorted(
            task_id
            for task_id, plan in self.tasks.items()
            if plan.status == "included"
        )

    def tasks_with_status(self, status: str) -> list[str]:
        return sorted(
            task_id
            for task_id, plan in self.tasks.items()
            if plan.status == status
        )

    def to_dict(self) -> dict[str, object]:
        return _encode(asdict(self))

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "PlanResult":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: dict) -> "PlanResult":
        tasks = {
            task_id: TaskPlan(
                **{
                    **plan,
                    "progress": _int_keys(plan["progress"]),
                    "sp_by_sprint": _int_keys(plan["sp_by_sprint"]),
                    "hours_by_sprint": _int_keys(plan["hours_by_sprint"]),
                }
            )
            for task_id, plan in data["tasks"].items()
        }

        initiatives = {
            initiative_id: InitiativePlan(**plan)
            for initiative_id, plan in data["initiatives"].items()
        }

        load_data = data["load"]

        load = Load(
            team_sp={
                team: _cells(cells)
                for team, cells in load_data["team_sp"].items()
            },
            role_hours={
                role: _cells(cells)
                for role, cells in load_data["role_hours"].items()
            },
            team_role_hours={
                team: {
                    role: _cells(cells)
                    for role, cells in roles.items()
                }
                for team, roles in load_data["team_role_hours"].items()
            },
            bottlenecks=[
                Bottleneck(**row)
                for row in load_data["bottlenecks"]
            ],
        )

        return cls(
            tasks=tasks,
            initiatives=initiatives,
            load=load,
            borrowing=[BorrowRow(**row) for row in data["borrowing"]],
            next_sprint_demand=[
                DemandRow(**row)
                for row in data["next_sprint_demand"]
            ],
            meta=Meta(**data["meta"]),
            changes=[Change(**row) for row in data.get("changes", [])],
        )


def _encode(value: object) -> object:
    """
    Готовит структуру к JSON: номера спринтов становятся строками.
    """

    if isinstance(value, dict):
        return {
            str(key): _encode(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]

    return value


def _int_keys(value: dict) -> dict:
    """
    Возвращает номерам спринтов целый тип после JSON.
    """

    return {
        int(key): item
        for key, item in value.items()
    }


def _cells(cells: dict) -> dict[int, LoadCell]:
    return {
        int(sprint): LoadCell(**cell)
        for sprint, cell in cells.items()
    }
