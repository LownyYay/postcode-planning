"""
Параметры планировщика.

Правило разделения: если параметр меняет числа во входном состоянии
(очки задач, ёмкости, фонды, rung) — он в InputConfig. Если меняет
только правила выбора плана — в SolveConfig. Поэтому сценарии с разными
правилами решения запускаются на одном состоянии без пересборки,
а подать во второй аргумент plan параметр, формирующий вход, нельзя:
такого поля в SolveConfig просто нет.
"""

from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[2]
PLANNER_CONFIG_PATH = (
    PROJECT_DIR
    / "config"
    / "planner.yaml"
)


def _check_option(name: str, value: object, allowed: tuple) -> None:
    if value not in allowed:
        raise ValueError(
            f"Параметр {name}={value!r}: "
            f"допустимо {', '.join(map(str, allowed))}"
        )


@dataclass(frozen=True)
class InputConfig:
    """
    Параметры, которые формируют входное состояние.
    """

    # Очки задач в работе: by_hours — доля оценки по остатку часов, full — вся оценка.
    inprogress_sp: str = "by_hours"
    # Множитель ёмкости команд для сценариев просадки.
    capacity_scale: float = 1.0
    focus_factor: float = 0.8
    hours_per_sprint: float = 80.0
    # Rung инициативы по rung её задач.
    initiative_rung: str = "max"
    # Как считать остаток при пересчёте: по часам или по очкам.
    remaining_from: str = "hours"
    # Квартал — первые спринты горизонта прогноза.
    quarter_sprints: int = 6
    horizon_sprints: int = 12

    def __post_init__(self) -> None:
        _check_option("inprogress_sp", self.inprogress_sp, ("by_hours", "full"))
        _check_option("initiative_rung", self.initiative_rung, ("max", "mean"))
        _check_option("remaining_from", self.remaining_from, ("hours", "sp"))

        if self.capacity_scale <= 0:
            raise ValueError("capacity_scale должен быть больше нуля")

        if not 1 <= self.quarter_sprints <= self.horizon_sprints:
            raise ValueError(
                "quarter_sprints должен быть от 1 до horizon_sprints"
            )


@dataclass(frozen=True)
class SolveConfig:
    """
    Правила решения: не меняют входные числа,
    меняют только то, какой план считается допустимым и лучшим.
    """

    # Движок: milp — точная модель, greedy — жадный план.
    engine: str = "milp"
    solver: str = "highs"
    # Страховочный лимит на весь расчёт, секунды.
    safety_time_limit: float = 120.0
    random_seed: int = 42

    objective_mode: str = "count_first"

    # Откуда берутся часы роли и что делать с ролями без носителей.
    role_pool: str = "team_borrow"
    missing_roles: str = "external"
    shared_split: str = "fixed"

    # Пустых спринтов между блокирующей и блокируемой задачей.
    gap_planned: int = 1
    gap_done: int = 0

    # Минимальная доля работы в каждом активном спринте задачи.
    min_progress: float = 0.1

    # Завершать задачи квартала как можно раньше при равном их числе.
    # Разрыв ничьей между равноценными планами: на итог по инициативам
    # и задачам не влияет, но делает расписание читаемым. Стоит дорого —
    # на состоянии с фондом по спринтам счёт растёт с 2 до 20 секунд.
    early_finish: bool = False
    inprogress_must_finish: bool = False

    # Контрфакты: повторное решение с принудительным включением
    # отложенной задачи или незавершённой инициативы.
    counterfactuals: bool = True
    counterfactual_limit: int = 10
    counterfactual_time_limit: float = 20.0

    def __post_init__(self) -> None:
        _check_option("engine", self.engine, ("milp", "greedy"))
        _check_option("solver", self.solver, ("highs", "cbc"))
        _check_option("objective_mode", self.objective_mode, ("count_first", "rung_weighted"))
        _check_option("role_pool", self.role_pool, ("team", "team_borrow", "global"))
        _check_option("missing_roles", self.missing_roles, ("block", "external"))
        # Гибкое деление часов совместителей не поддерживается:
        # при основном team_borrow оно влияет только на учёт заимствований.
        _check_option("shared_split", self.shared_split, ("fixed",))
        _check_option("gap_planned", self.gap_planned, (0, 1))
        _check_option("gap_done", self.gap_done, (0, 1))

        if not 0 <= self.min_progress <= 0.2:
            raise ValueError("min_progress должен быть от 0 до 0.2")

        if self.safety_time_limit <= 0:
            raise ValueError("safety_time_limit должен быть больше нуля")


@dataclass(frozen=True)
class PlannerConfig:
    """
    Полный сценарий: как собрать вход и как решать.
    """

    input: InputConfig = field(default_factory=InputConfig)
    solve: SolveConfig = field(default_factory=SolveConfig)

    def with_input(self, **overrides: object) -> "PlannerConfig":
        return replace(self, input=replace(self.input, **overrides))

    def with_solve(self, **overrides: object) -> "PlannerConfig":
        return replace(self, solve=replace(self.solve, **overrides))

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {
            "input": asdict(self.input),
            "solve": asdict(self.solve),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannerConfig":
        return cls(
            input=InputConfig(**data.get("input", {})),
            solve=SolveConfig(**data.get("solve", {})),
        )


def load_planner_config(
    path: Path = PLANNER_CONFIG_PATH,
) -> PlannerConfig:
    """
    Загружает сценарий из YAML с разделами input и solve.

    Отсутствующие ключи берутся по умолчанию,
    незнакомые ключи и разделы считаются ошибкой.
    """

    data: dict = {}

    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}

    unknown_sections = sorted(set(data) - {"input", "solve"})

    if unknown_sections:
        raise ValueError(
            f"Неизвестные разделы в {path.name}: {', '.join(unknown_sections)}"
        )

    for section, config_type in (("input", InputConfig), ("solve", SolveConfig)):
        known = {item.name for item in fields(config_type)}
        unknown = sorted(set(data.get(section) or {}) - known)

        if unknown:
            raise ValueError(
                f"Неизвестные параметры в разделе {section}: {', '.join(unknown)}"
            )

    return PlannerConfig(
        input=InputConfig(**(data.get("input") or {})),
        solve=SolveConfig(**(data.get("solve") or {})),
    )
