"""
Хранение baseline и планов.

Baseline — первоначальный план квартала, с которым сравниваются
пересчёты и факт. Он неизменяем: объект заморожен, план внутри
хранится строкой JSON и при каждом чтении разворачивается в новую
копию, а файл на диске сохраняется только для чтения. Пересчёт
плана не может изменить базу сравнения.
"""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from src.planner.result import PlanResult


class BaselineError(ValueError):
    """
    Baseline не подходит для сравнения с этим планом или состоянием.
    """


@dataclass(frozen=True)
class Baseline:
    """
    Первоначальный план квартала.

    planned_sp — плановые SP по спринтам: {спринт: {команда: SP}}.
    Считаются один раз и после пересчётов не меняются.
    planned_initiatives — инициативы, запланированные к завершению
    в квартале: знаменатель показателя квартала.
    """

    plan_json: str
    input_config: tuple[tuple[str, object], ...]
    planned_sp: tuple[tuple[int, tuple[tuple[str, float], ...]], ...]
    planned_initiatives: tuple[str, ...]

    @property
    def plan(self) -> PlanResult:
        """
        Новая копия плана: изменить сам baseline через неё нельзя.
        """

        return PlanResult.from_json(self.plan_json)

    @property
    def input(self) -> dict[str, object]:
        return dict(self.input_config)

    def planned_sp_by_sprint(self) -> dict[int, dict[str, float]]:
        return {
            sprint: dict(teams)
            for sprint, teams in self.planned_sp
        }

    def fingerprint(self) -> str:
        """
        Хеш содержимого — для проверки, что baseline не менялся.
        """

        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(
            {
                "plan": json.loads(self.plan_json),
                "input_config": self.input,
                "planned_sp": {
                    str(sprint): dict(teams)
                    for sprint, teams in self.planned_sp
                },
                "planned_initiatives": list(self.planned_initiatives),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "Baseline":
        data = json.loads(text)

        return make_baseline(PlanResult.from_dict(data["plan"]))


def make_baseline(plan: PlanResult) -> Baseline:
    """
    Фиксирует план как baseline.
    """

    if plan.meta.current_sprint != 0:
        raise BaselineError(
            "Baseline строится только по плану на начало квартала "
            f"(текущий спринт 0, а не {plan.meta.current_sprint})"
        )

    quarter = int(plan.meta.input_config["quarter_sprints"])
    planned: dict[int, dict[str, float]] = {}

    for task in plan.tasks.values():
        for sprint, sp in task.sp_by_sprint.items():
            if sprint <= quarter:
                teams = planned.setdefault(sprint, {})
                teams[task.team] = teams.get(task.team, 0.0) + sp

    return Baseline(
        plan_json=plan.to_json(),
        input_config=tuple(sorted(plan.meta.input_config.items())),
        planned_sp=tuple(
            (sprint, tuple(sorted(planned.get(sprint, {}).items())))
            for sprint in range(1, quarter + 1)
        ),
        planned_initiatives=tuple(
            sorted(
                initiative_id
                for initiative_id, initiative in plan.initiatives.items()
                if initiative.done_in_quarter
            )
        ),
    )


def save_baseline(baseline: Baseline, path: Path) -> None:
    """
    Сохраняет baseline и делает файл доступным только для чтения.
    Перезаписать существующий baseline нельзя.
    """

    if path.exists():
        raise BaselineError(f"Baseline уже сохранён: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.to_json(), encoding="utf-8")
    os.chmod(path, stat.S_IREAD)


def load_baseline(path: Path) -> Baseline:
    return Baseline.from_json(path.read_text(encoding="utf-8"))


def save_plan(plan: PlanResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.to_json(), encoding="utf-8")


def load_plan(path: Path) -> PlanResult:
    return PlanResult.from_json(path.read_text(encoding="utf-8"))
