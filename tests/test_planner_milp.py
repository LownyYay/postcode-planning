"""
Точный планировщик на реальном датасете и защита от сбоев решателя.

Каждый сценарий задаёт конфиг явно. Итог проверяется парой
«число инициатив + сумма rung»: равноценные наборы инициатив
в данных возможны, поэтому конкретный набор проверяется только там,
где он единственный.
"""

import pickle
import time
import warnings
from functools import lru_cache

import pulp
import pytest

from src.dataset import load_dataset
from src.planner import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    build_state,
    plan,
    run_scenario,
    validate_plan,
)
from src.planner.greedy import plan_greedy
from src.planner.milp import (
    Level,
    build_model,
    decode_initiatives,
    fix_level,
    integer_rungs,
    level_initiatives,
    plan_milp,
    solve_level,
)
from src.planner.precheck import run_precheck
from src.planner.state import (
    EngineerInput,
    InitiativeInput,
    PlanningState,
    TaskInput,
)


BASE = PlannerConfig(
    input=InputConfig(inprogress_sp="by_hours", initiative_rung="max"),
    solve=SolveConfig(
        engine="milp",
        solver="highs",
        role_pool="team_borrow",
        missing_roles="external",
        gap_planned=1,
        gap_done=0,
        min_progress=0.1,
        objective_mode="count_first",
        # Контрфакты проверяются в test_planner_explain.py,
        # здесь они только замедляли бы сценарии.
        counterfactuals=False,
    ),
)

# Сценарий: (параметры входа, параметры решения).
SCENARIOS = {
    "by_hours": ({}, {}),
    "full": ({"inprogress_sp": "full"}, {}),
    "full_global": ({"inprogress_sp": "full"}, {"role_pool": "global"}),
    "capacity_0_8": ({"capacity_scale": 0.8}, {}),
    "capacity_0_6": ({"capacity_scale": 0.6}, {}),
    "capacity_0_5": ({"capacity_scale": 0.5}, {}),
    "team": ({}, {"role_pool": "team"}),
    "capacity_0_5_team": ({"capacity_scale": 0.5}, {"role_pool": "team"}),
    "block": ({}, {"missing_roles": "block"}),
}

# Мягкий порог времени одного плана на этом датасете. Замеры HiGHS
# в один поток: 0.1–8 с в зависимости от сценария, узкое место — проход L5
# при просадке ёмкости. Превышение — предупреждение, а не ошибка.
SOFT_SECONDS = 10.0


@lru_cache(maxsize=None)
def dataset():
    return load_dataset()


def config_of(name: str) -> PlannerConfig:
    input_overrides, solve_overrides = SCENARIOS[name]
    return BASE.with_input(**input_overrides).with_solve(**solve_overrides)


@lru_cache(maxsize=None)
def run(name: str):
    config = config_of(name)
    result = run_scenario(dataset(), config)

    if result.meta.elapsed_seconds > SOFT_SECONDS:
        warnings.warn(
            f"Сценарий {name}: {result.meta.elapsed_seconds:.1f} с "
            f"при мягком пороге {SOFT_SECONDS} с",
            stacklevel=2,
        )

    return build_state(dataset(), config.input), config.solve, result


def done_initiatives(result) -> set[str]:
    return {
        initiative_id
        for initiative_id, initiative in result.initiatives.items()
        if initiative.done_in_quarter
    }


def summary(result) -> tuple[int, float]:
    objective = result.meta.objective
    return int(objective["initiatives_done"]), objective["initiatives_rung"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_is_optimal_and_valid(name: str) -> None:
    state, solve, result = run(name)

    assert result.meta.status == "optimal"
    assert result.meta.engine == "milp"

    validate_plan(result, state, solve)


@pytest.mark.parametrize(
    ("name", "initiatives", "rung"),
    [
        ("by_hours", 15, 1235),
        ("capacity_0_8", 13, 1075),
        ("capacity_0_6", 11, 924),
        ("capacity_0_5", 11, 901),
        ("full", 14, 1161),
    ],
)
def test_initiatives_and_rung(name: str, initiatives: int, rung: float) -> None:
    _, _, result = run(name)

    assert summary(result) == (initiatives, rung)

    # Значения слитого прохода L12 совпадают с итогом плана.
    assert result.meta.objective["L12_initiatives"] == initiatives
    assert result.meta.objective["L12_rung"] == rung


def test_full_sp_mode() -> None:
    _, _, result = run("full")

    assert done_initiatives(result) == {
        f"PRODF-{number}" for number in range(7120, 7135)
    } - {"PRODF-7120"}

    # Отложенная задача получает прогноз завершения после квартала.
    kp = result.tasks["KP-3898"]

    assert kp.status == "postponed"
    assert kp.forecast_sprint == 7


def test_team_borrow_matches_global() -> None:
    _, _, borrow = run("full")
    _, _, shared = run("full_global")

    assert summary(borrow) == summary(shared)

    # Без штрафа за заимствование завершается больше задач.
    assert len(shared.included_task_ids) == 36
    assert len(borrow.included_task_ids) <= len(shared.included_task_ids)


def test_strict_team_pool() -> None:
    _, _, result = run("team")

    assert result.initiatives_done == 1
    assert len(result.included_task_ids) == 6
    assert result.borrowing == []


def test_missing_roles_block() -> None:
    _, _, result = run("block")

    # При блокирующих ролях завершимы только эти две инициативы.
    assert done_initiatives(result) == {"PRODF-7123", "PRODF-7124"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_milp_not_worse_than_greedy(name: str) -> None:
    state, solve, result = run(name)
    greedy = plan_greedy(state, solve)

    validate_plan(greedy, state, solve)

    assert result.initiatives_done >= greedy.initiatives_done


def separate_initiative_levels(state, solve) -> dict[str, float]:
    """
    Уровни 1 и 2 двумя отдельными проходами — для сверки со слитым L12.
    """

    precheck = run_precheck(state, solve)
    model = build_model(state, solve, precheck, last_sprint=state.quarter)
    deadline = time.perf_counter() + 120

    count = Level(
        "count",
        pulp.lpSum(model.init_done.values()),
        pulp.LpMaximize,
        integral=True,
    )

    count_value = solve_level(model, count, deadline)
    fix_level(model, count, count_value)

    rungs, scale = integer_rungs(model)

    rung = Level(
        "rung",
        pulp.lpSum(
            rungs[initiative_id] * variable
            for initiative_id, variable in model.init_done.items()
        ),
        pulp.LpMaximize,
        integral=True,
    )

    rung_value = solve_level(model, rung, deadline)

    return {
        "L12_initiatives": round(count_value),
        "L12_rung": round(rung_value) / scale,
    }


def merged_initiative_level(state, solve, last_sprint) -> dict[str, float]:
    precheck = run_precheck(state, solve)
    model = build_model(state, solve, precheck, last_sprint=last_sprint)
    value = solve_level(
        model,
        level_initiatives(model),
        time.perf_counter() + 120,
    )

    return decode_initiatives(model, value)


@pytest.mark.parametrize("name", ["capacity_0_8", "full"])
def test_merged_level_equals_separate_passes(name: str) -> None:
    state, solve, _ = run(name)

    assert (
        merged_initiative_level(state, solve, state.quarter)
        == separate_initiative_levels(state, solve)
    )


def test_quarter_model_equals_full_horizon() -> None:
    # Уровни 1–2 не зависят от спринтов после квартала.
    state, solve, _ = run("capacity_0_8")

    assert (
        merged_initiative_level(state, solve, state.quarter)
        == merged_initiative_level(state, solve, state.horizon)
    )


def test_forecast_pass_keeps_quarter_plan() -> None:
    # Одна и та же модель: до прохода L6 и после него.
    state, solve, _ = run("full")
    precheck = run_precheck(state, solve)
    floor = plan_greedy(state, solve, precheck)

    before = plan_milp(state, solve, precheck, greedy_floor=floor, stop_after="L5")
    after = plan_milp(state, solve, precheck, greedy_floor=floor)

    assert after.included_task_ids == before.included_task_ids

    for task_id in after.included_task_ids:
        assert after.tasks[task_id].progress == before.tasks[task_id].progress

    # После L6 у отложенных задач появляется прогноз.
    assert after.tasks["KP-3898"].forecast_sprint == 7
    assert before.tasks["KP-3898"].forecast_sprint is None


def test_plan_does_not_change_state() -> None:
    config = config_of("full")
    state = build_state(dataset(), config.input)
    snapshot = pickle.dumps(state)

    plan(state, config.solve)

    assert pickle.dumps(state) == snapshot


def comparable(result) -> dict:
    data = result.to_dict()
    data["meta"].pop("elapsed_seconds")
    data["meta"]["objective"] = {
        key: value
        for key, value in data["meta"]["objective"].items()
        if not key.startswith("seconds_")
    }
    return data


def test_milp_is_deterministic() -> None:
    state, solve, first = run("by_hours")

    assert comparable(plan(state, solve)) == comparable(first)


def test_plan_reproduces_from_meta() -> None:
    _, _, original = run("capacity_0_8")

    config = PlannerConfig.from_dict(
        {
            "input": original.meta.input_config,
            "solve": original.meta.solve_config,
        }
    )

    assert config == config_of("capacity_0_8")
    assert comparable(run_scenario(dataset(), config)) == comparable(original)


def test_plan_accepts_only_solve_config() -> None:
    state = build_state(dataset(), BASE.input)

    with pytest.raises(TypeError):
        plan(state, InputConfig())


def impossible_in_quarter_state() -> PlanningState:
    """
    Задача в работе требует 600 ч разработчика при фонде 80 ч:
    восемь спринтов, в квартал из шести не помещается.
    """

    task = TaskInput(
        task_id="X",
        team="A",
        initiative="I",
        status="InProgress",
        rung=80,
        sp=5,
        sp_effective=5,
        original_hours={"Dev": 600},
        remaining_hours={"Dev": 600},
    )

    return PlanningState(
        input_config=InputConfig(),
        current_sprint=0,
        tasks={"X": task},
        done_tasks={},
        initiatives={"I": InitiativeInput("I", 80, ("X",), ())},
        team_capacity={"A": 10.0},
        team_role_fund={("A", "Dev"): 80.0},
        engineers=(EngineerInput("E1", "A", "Dev", 1.0),),
        dependencies=(),
        missing_roles=frozenset(),
        reason_codes=("Превышение плана", "Отсутствие ресурсов"),
    )


def test_infeasible_model_falls_back_to_greedy() -> None:
    state = impossible_in_quarter_state()
    solve = SolveConfig(
        role_pool="team",
        inprogress_must_finish=True,
    )

    result = plan(state, solve)

    assert result.meta.status == "fallback_greedy"
    assert result.meta.engine == "greedy"
    assert "недопустима" in result.meta.fallback_reason
    assert any("Точный план не построен" in text for text in result.meta.warnings)

    validate_plan(result, state, solve)
