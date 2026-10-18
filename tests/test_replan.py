"""
Пересчёт по факту спринта, неизменяемость базы и показатели.

Основные проверки — на маленьком выдуманном примере, где правильный
ответ считается в уме. Один тест прогоняет весь цикл на реальном датасете.
"""

import dataclasses
from functools import lru_cache

import pandas as pd
import pytest

from src.dataset import load_dataset
from src.metrics import quarter_performance, sprint_performance
from src.planner import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    build_state,
    plan,
    validate_plan,
)
from src.planner.facts import (
    FactError,
    SprintFact,
    apply_facts,
    load_sprint_fact,
)
from src.planner.replan import replan
from src.planner.state import (
    EngineerInput,
    InitiativeInput,
    PlanningState,
    TaskInput,
)
from src.store import Baseline, BaselineError, load_baseline, make_baseline, save_baseline


GREEDY = SolveConfig(engine="greedy", role_pool="team", counterfactuals=False)


def make_task(task_id, team, initiative, sp, hours, status="ToDo"):
    return TaskInput(
        task_id=task_id,
        team=team,
        initiative=initiative,
        status=status,
        rung=80,
        sp=sp,
        sp_effective=sp,
        original_hours=dict(hours),
        remaining_hours=dict(hours),
    )


def make_state() -> PlanningState:
    """
    Команда A: разработчик на полную ставку, 10 SP за спринт.
    T1 (80 ч) и T2 (40 ч) в инициативе I1, связей нет.
    """

    tasks = {
        "T1": make_task("T1", "A", "I1", 6, {"Dev": 80}),
        "T2": make_task("T2", "A", "I1", 4, {"Dev": 40}),
    }

    return PlanningState(
        input_config=InputConfig(),
        current_sprint=0,
        tasks=tasks,
        done_tasks={},
        initiatives={"I1": InitiativeInput("I1", 80, ("T1", "T2"), ())},
        team_capacity={"A": 10.0},
        team_role_fund={("A", "Dev"): 80.0},
        engineers=(EngineerInput("E1", "A", "Dev", 1.0),),
        dependencies=(),
        missing_roles=frozenset(),
        reason_codes=("Превышение плана", "Отсутствие ресурсов"),
    )


def baseline_case():
    state = make_state()
    first = plan(state, GREEDY)

    return state, first, make_baseline(first)


def fact_as_planned(plan_result, state, sprint: int) -> SprintFact:
    """
    Факт, в точности повторяющий план спринта.
    """

    hours = {}
    closed = set()

    for task_id, task_plan in plan_result.tasks.items():
        share = task_plan.progress.get(sprint)

        if not share:
            continue

        for role, spent in task_plan.hours_by_sprint[sprint].items():
            hours[(task_id, role)] = spent

        if task_plan.forecast_sprint == sprint:
            closed.add(task_id)

    return SprintFact(sprint=sprint, hours=hours, closed=frozenset(closed))


def test_fact_format() -> None:
    table = pd.DataFrame(
        [
            {"sprint": 1, "task_id": "T1", "role": "ДевОпс", "spent_hours": 40, "status": "InProgress"},
            {"sprint": 1, "task_id": "T1", "role": "Девопс", "spent_hours": 10, "status": "InProgress"},
            {"sprint": 1, "task_id": "T2", "role": None, "spent_hours": None, "status": "Done"},
        ]
    )

    fact = load_sprint_fact(table, {"ДевОпс": "Девопс"})

    assert fact.sprint == 1
    # Роли нормализуются и складываются.
    assert fact.hours == {("T1", "Девопс"): 50}
    assert fact.closed == {"T2"}


def test_fact_format_errors() -> None:
    with pytest.raises(FactError):
        load_sprint_fact(pd.DataFrame([{"sprint": 1, "task_id": "T1"}]))

    two_sprints = pd.DataFrame(
        [
            {"sprint": 1, "task_id": "T1", "role": "Dev", "spent_hours": 1, "status": "ToDo"},
            {"sprint": 2, "task_id": "T1", "role": "Dev", "spent_hours": 1, "status": "ToDo"},
        ]
    )

    with pytest.raises(FactError):
        load_sprint_fact(two_sprints)


def test_apply_facts_closes_and_reduces() -> None:
    state = make_state()

    fact = SprintFact(
        sprint=1,
        hours={("T1", "Dev"): 40, ("T2", "Dev"): 40},
        closed=frozenset({"T2"}),
    )

    after = apply_facts(state, [fact])

    assert after.current_sprint == 1
    assert after.done_tasks == {"T2": 1}
    assert "T2" not in after.tasks
    assert after.tasks["T1"].remaining_hours == {"Dev": 40}

    # Половина часов сделана — половина очков осталась.
    assert after.tasks["T1"].sp_effective == pytest.approx(3)
    assert after.initiatives["I1"].open_task_ids == ("T1",)


def test_exhausted_hours_keep_remainder() -> None:
    state = make_state()

    fact = SprintFact(
        sprint=1,
        hours={("T1", "Dev"): 100},
        closed=frozenset(),
    )

    after = apply_facts(state, [fact])

    # Часы кончились, задача не закрыта: остаток — 10 % сметы.
    assert after.tasks["T1"].remaining_hours == {"Dev": 8}
    assert any("10 %" in warning for warning in after.warnings)


def test_baseline_is_immutable(tmp_path) -> None:
    _, first, baseline = baseline_case()
    fingerprint = baseline.fingerprint()

    with pytest.raises(dataclasses.FrozenInstanceError):
        baseline.plan_json = "{}"

    # Копия плана из базы изменяема, сама база — нет.
    copy = baseline.plan
    copy.tasks["T1"].status = "cancelled"

    assert baseline.plan.tasks["T1"].status != "cancelled"
    assert baseline.fingerprint() == fingerprint

    path = tmp_path / "baseline.json"
    save_baseline(baseline, path)

    assert load_baseline(path).fingerprint() == fingerprint

    # Повторное сохранение запрещено, файл только для чтения.
    with pytest.raises(BaselineError):
        save_baseline(baseline, path)


def test_replan_on_plan_keeps_schedule() -> None:
    state, first, baseline = baseline_case()
    fingerprint = baseline.fingerprint()

    fact = fact_as_planned(first, state, 1)
    result = replan(state, [fact], first, baseline, GREEDY)

    # База не изменилась.
    assert baseline.fingerprint() == fingerprint

    # Работа шла по плану — сдвигов нет.
    assert all(
        change.shift_vs_baseline in (0, None)
        for change in result.changes
    )

    performance = sprint_performance(baseline, state, [fact])

    assert performance[0].ratio == pytest.approx(1.0)


def test_replan_rejects_other_input_config() -> None:
    state, first, baseline = baseline_case()

    other = dataclasses.replace(
        state,
        input_config=InputConfig(capacity_scale=0.5),
    )

    with pytest.raises(BaselineError):
        replan(other, [], first, baseline, GREEDY)


def test_quarter_performance_counts_planned_initiatives() -> None:
    state, first, baseline = baseline_case()

    assert baseline.planned_initiatives == ("I1",)

    fact = SprintFact(
        sprint=1,
        hours={("T1", "Dev"): 80, ("T2", "Dev"): 40},
        closed=frozenset({"T1", "T2"}),
    )

    after = apply_facts(state, [fact])
    result = plan(after, GREEDY)
    performance = quarter_performance(baseline, result, after)

    assert performance.done == ("I1",)
    assert performance.actual_ratio == 1.0


@lru_cache(maxsize=None)
def dataset():
    return load_dataset()


def test_full_cycle_on_dataset() -> None:
    config = PlannerConfig(
        input=InputConfig(inprogress_sp="by_hours"),
        solve=SolveConfig(counterfactuals=False),
    )

    state = build_state(dataset(), config.input)
    first = plan(state, config.solve)
    baseline = make_baseline(first)
    fingerprint = baseline.fingerprint()

    fact = fact_as_planned(first, state, 1)
    result = replan(state, [fact], first, baseline, config.solve)

    validate_plan(result, apply_facts(state, [fact]), config.solve)

    assert result.meta.current_sprint == 1
    assert baseline.fingerprint() == fingerprint

    # При пересчёте включается уровень стабильности.
    assert "L3" in result.meta.objective

    # Спринт отработан по плану: выполнение 100 %, инициативы не сдвинулись.
    performance = sprint_performance(baseline, state, [fact])

    assert performance[0].ratio == pytest.approx(1.0, abs=1e-6)

    shifts = [
        change
        for change in result.changes
        if change.kind == "initiative" and change.shift_vs_baseline
    ]

    assert shifts == []


def test_external_roles_do_not_slow_progress() -> None:
    """
    Роль без носителей в факте не отчитывается: прогресс считается
    по ролям штата, а внештатная часть идёт в той же доле.
    """

    from src.planner.facts import spend_hours, work_share

    remaining = {"Dev": 80.0, "PM": 20.0}

    assert work_share(remaining, {"Dev": 40.0}, {"PM"}) == pytest.approx(0.5)
    assert spend_hours(remaining, {"Dev": 40.0}, {"PM"}) == pytest.approx({"Dev": 40.0, "PM": 10.0})

    # Если внештатные часы всё же пришли в факте, они вычитаются как есть.
    assert spend_hours(remaining, {"Dev": 40.0, "PM": 5.0}, {"PM"}) == pytest.approx({"Dev": 40.0, "PM": 15.0})

    # Без внештатных ролей правило прежнее: часы по роли минус факт.
    assert work_share({"Dev": 80.0}, {"Dev": 20.0}, set()) == pytest.approx(0.25)


def test_sprint_on_plan_is_full_without_external_hours() -> None:
    config = PlannerConfig(
        input=InputConfig(inprogress_sp="by_hours"),
        solve=SolveConfig(counterfactuals=False),
    )

    state = build_state(dataset(), config.input)
    first = plan(state, config.solve)
    baseline = make_baseline(first)

    # Факт по плану, но без часов ролей вне штата: их выполняют внешние исполнители.
    fact = fact_as_planned(first, state, 1)
    fact = SprintFact(
        sprint=1,
        hours={key: hours for key, hours in fact.hours.items() if key[1] not in state.missing_roles},
        closed=fact.closed,
    )

    performance = sprint_performance(baseline, state, [fact])

    assert performance[0].ratio == pytest.approx(1.0, abs=1e-6)
