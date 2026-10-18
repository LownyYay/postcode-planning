"""
Риски плана: красный, жёлтый и оранжевый алерты.

Жёлтый проверяется на маленьком выдуманном примере: задача отстаёт
и тянет за собой зависимую. Красный и оранжевый — на реальном датасете.
"""

from functools import lru_cache

import pytest

from src.dataset import load_dataset
from src.planner import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    build_state,
    plan,
)
from src.planner.facts import SprintFact, apply_facts
from src.planner.replan import replan
from src.planner.state import (
    EngineerInput,
    InitiativeInput,
    PlanningState,
    TaskInput,
)
from src.risks import find_risks, summarize
from src.store import make_baseline


GREEDY = SolveConfig(engine="greedy", role_pool="team", counterfactuals=False)


def make_task(task_id, hours, sp=4):
    return TaskInput(
        task_id=task_id,
        team="A",
        initiative="I1",
        status="ToDo",
        rung=80,
        sp=sp,
        sp_effective=sp,
        original_hours=dict(hours),
        remaining_hours=dict(hours),
    )


def chain_state() -> PlanningState:
    """
    Цепочка T1 → T2 → T3 по 80 ч разработчика,
    фонд — 80 ч за спринт, зазор между задачами один спринт.
    """

    tasks = {
        "T1": make_task("T1", {"Dev": 80}),
        "T2": make_task("T2", {"Dev": 80}),
        "T3": make_task("T3", {"Dev": 80}),
    }

    return PlanningState(
        input_config=InputConfig(),
        current_sprint=0,
        tasks=tasks,
        done_tasks={},
        initiatives={"I1": InitiativeInput("I1", 80, ("T1", "T2", "T3"), ())},
        team_capacity={"A": 10.0},
        team_role_fund={("A", "Dev"): 80.0},
        engineers=(EngineerInput("E1", "A", "Dev", 1.0),),
        dependencies=(("T1", "T2"), ("T2", "T3")),
        missing_roles=frozenset(),
        reason_codes=("Превышение плана", "Отсутствие ресурсов"),
    )


def test_cascade_shift_after_slip() -> None:
    state = chain_state()
    first = plan(state, GREEDY)
    baseline = make_baseline(first)

    # По плану T1 закрывается в первом спринте.
    assert first.tasks["T1"].forecast_sprint == 1

    # По факту сделали только половину и не закрыли.
    fact = SprintFact(sprint=1, hours={("T1", "Dev"): 40}, closed=frozenset())

    updated = replan(state, [fact], first, baseline, GREEDY)
    current = apply_facts(state, [fact])

    risks = find_risks(updated, current, GREEDY)
    cascade = [risk for risk in risks if risk.kind == "cascade_shift"]

    assert len(cascade) == 1
    assert cascade[0].level == "yellow"
    assert cascade[0].subject == "T1"

    # Сдвинулись обе зависимые задачи.
    assert set(cascade[0].details["affected"]) == {"T2", "T3"}
    assert "T2" in cascade[0].text


def test_no_cascade_without_replan() -> None:
    state = chain_state()

    # До пересчёта сравнивать не с чем — жёлтых алертов нет.
    risks = find_risks(plan(state, GREEDY), state, GREEDY)

    assert [risk for risk in risks if risk.level == "yellow"] == []


@lru_cache(maxsize=None)
def dataset():
    return load_dataset()


@lru_cache(maxsize=None)
def full_case():
    config = PlannerConfig(
        input=InputConfig(inprogress_sp="full"),
        solve=SolveConfig(counterfactuals=False),
    )

    state = build_state(dataset(), config.input)

    return state, config.solve, plan(state, config.solve)


def test_red_alert_for_overrun_initiative() -> None:
    state, solve, result = full_case()

    risks = find_risks(result, state, solve)
    red = [risk for risk in risks if risk.level == "red"]

    assert [risk.subject for risk in red] == ["PRODF-7120"]
    assert red[0].kind == "initiative_overrun"
    assert red[0].details["forecast_sprint"] > state.quarter
    assert "срывается" in red[0].text


def test_orange_alerts_for_roles() -> None:
    state, solve, result = full_case()

    risks = find_risks(result, state, solve)
    orange = [risk for risk in risks if risk.level == "orange"]

    deficits = [risk for risk in orange if risk.kind == "role_deficit"]
    missing = [risk for risk in orange if risk.kind == "role_missing"]
    gaps = [risk for risk in orange if risk.kind == "team_gap"]

    # Роли без единого носителя — один риск с часами каждой роли.
    assert len(missing) == 1
    assert set(missing[0].details["roles"]) == state.missing_roles
    assert missing[0].details["roles"]["Руководитель проекта"]["quarter_hours"] > 400

    # Часы, которые план берёт у других команд, — один риск на все команды.
    assert len(gaps) == 1
    assert gaps[0].details["gap_hours"] > 0
    assert "Team-Platform" in gaps[0].details["teams"]

    # В первом спринте план сдерживают очки команд, а не часы ролей:
    # роль, у которой есть свободные часы, дефицитом не считается.
    for risk in deficits:
        assert risk.details["fund_hours"] - risk.details["planned_hours"] <= max(1.0, 0.02 * risk.details["fund_hours"])


def test_summary_counts() -> None:
    state, solve, result = full_case()

    counts = summarize(find_risks(result, state, solve))

    assert counts["red"] == 1
    assert counts["yellow"] == 0
    assert counts["orange"] == 2


def deficit_state(fund: float) -> PlanningState:
    """
    Две независимые задачи по 80 ч разработчика, очков хватает на обе.
    Фонд роли задаёт, успевают ли обе в первом спринте.
    """

    tasks = {
        "T1": make_task("T1", {"Dev": 80}),
        "T2": make_task("T2", {"Dev": 80}),
    }

    return PlanningState(
        input_config=InputConfig(),
        current_sprint=0,
        tasks=tasks,
        done_tasks={},
        initiatives={"I1": InitiativeInput("I1", 80, ("T1", "T2"), ())},
        team_capacity={"A": 20.0},
        team_role_fund={("A", "Dev"): fund},
        engineers=(EngineerInput("E1", "A", "Dev", fund / 80),),
        dependencies=(),
        missing_roles=frozenset(),
        reason_codes=("Превышение плана", "Отсутствие ресурсов"),
    )


def test_role_deficit_needs_full_role_and_waiting_work() -> None:
    # Фонд 80 ч: роль занята полностью, вторая задача ждёт — дефицит.
    state = deficit_state(80.0)
    risks = find_risks(plan(state, GREEDY), state, GREEDY)
    deficits = [risk for risk in risks if risk.kind == "role_deficit"]

    assert len(deficits) == 1
    assert deficits[0].details["planned_hours"] == pytest.approx(80.0)
    assert deficits[0].details["gap_hours"] == pytest.approx(80.0)
    assert "занята полностью" in deficits[0].text

    # Фонд 160 ч: обе задачи идут в первом спринте, дефицита нет.
    state = deficit_state(160.0)
    risks = find_risks(plan(state, GREEDY), state, GREEDY)

    assert [risk for risk in risks if risk.kind == "role_deficit"] == []


def test_goal_slip_after_replan() -> None:
    state = chain_state()
    first = plan(state, GREEDY)
    baseline = make_baseline(first)

    fact = SprintFact(sprint=1, hours={("T1", "Dev"): 40}, closed=frozenset())
    updated = replan(state, [fact], first, baseline, GREEDY)
    risks = find_risks(updated, apply_facts(state, [fact]), GREEDY)

    slips = [risk for risk in risks if risk.kind == "initiative_slip"]

    # Цель ещё в квартале, но завершается позже базы и теряет запас.
    assert updated.initiatives["I1"].done_in_quarter
    assert [risk.subject for risk in slips] == ["I1"]
    assert slips[0].details["shift"] > 0
    assert slips[0].details["slack_sprints"] == state.quarter - slips[0].details["forecast_sprint"]
