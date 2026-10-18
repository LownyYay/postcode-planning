import pytest

from src.dataset import Dataset, load_dataset
from src.planner import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    build_state,
    validate_plan,
)
from src.planner.greedy import plan_greedy
from src.planner.precheck import run_precheck
from src.planner.result import REASON_NO_RESOURCES, REASON_NOT_READY


# Конфиг каждого сценария задаётся явно.
BASE = PlannerConfig(
    input=InputConfig(inprogress_sp="by_hours"),
    solve=SolveConfig(
        engine="greedy",
        role_pool="team_borrow",
        missing_roles="external",
        gap_planned=1,
        gap_done=0,
        min_progress=0.1,
    ),
)

# Сценарий: (параметры входа, параметры решения).
SCENARIOS = {
    "baseline": ({}, {}),
    "full": ({"inprogress_sp": "full"}, {}),
    "global": ({}, {"role_pool": "global"}),
    "team": ({}, {"role_pool": "team"}),
    "block": ({}, {"missing_roles": "block"}),
    "capacity_0_8": ({"capacity_scale": 0.8}, {}),
    "capacity_0_6": ({"capacity_scale": 0.6}, {}),
    "capacity_0_5": ({"capacity_scale": 0.5}, {}),
    "gap_0": ({}, {"gap_planned": 0}),
    "min_progress_0": ({}, {"min_progress": 0.0}),
}


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


def run_greedy(dataset: Dataset, name: str):
    input_overrides, solve_overrides = SCENARIOS[name]
    config = BASE.with_input(**input_overrides).with_solve(**solve_overrides)
    state = build_state(dataset, config.input)

    return state, config.solve, plan_greedy(state, config.solve)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_greedy_plan_is_valid(dataset: Dataset, name: str) -> None:
    state, solve, plan = run_greedy(dataset, name)

    validate_plan(plan, state, solve)

    assert plan.meta.engine == "greedy"
    assert plan.meta.status == "greedy"


def test_strict_team_pool(dataset: Dataset) -> None:
    _, _, plan = run_greedy(dataset, "team")

    assert plan.initiatives_done == 1
    assert len(plan.included_task_ids) == 6
    assert plan.borrowing == []

    cancelled = [
        plan.tasks[task_id]
        for task_id in plan.tasks_with_status("cancelled")
    ]

    # Причины «нет роли в команде» и отмены по цепочке.
    assert any(
        task.reason_code == REASON_NO_RESOURCES
        and "нет роли" in task.reason_text
        for task in cancelled
    )

    assert any(
        task.reason_code == REASON_NOT_READY
        for task in cancelled
    )


def test_missing_roles_block(dataset: Dataset) -> None:
    _, _, plan = run_greedy(dataset, "block")

    done = {
        initiative_id
        for initiative_id, initiative in plan.initiatives.items()
        if initiative.done_in_quarter
    }

    # Больше двух инициатив при блокирующих ролях не завершить.
    assert done <= {"PRODF-7123", "PRODF-7124"}


def test_zero_remaining_task_closes_first(dataset: Dataset) -> None:
    _, _, plan = run_greedy(dataset, "baseline")

    closed = plan.tasks["MOB-7011"]

    assert closed.status == "included"
    assert closed.progress == {1: 1.0}
    assert closed.sp_by_sprint == {1: 0.0}
    assert closed.hours_by_sprint == {1: {}}
    assert closed.reason_text == "Остаток 0 ч — закрытие без затрат."

    # Зависимая задача открывается по обычному правилу зазора.
    assert plan.tasks["MOB-7012"].sprint_start >= 3


def test_precheck_min_duration(dataset: Dataset) -> None:
    state = build_state(dataset, BASE.input)
    precheck = run_precheck(state, BASE.solve)

    # 90 ч .NET при фонде 80 и 11 SP при ёмкости 7.2 — не меньше двух спринтов.
    assert precheck.min_duration["DB-202"] == 2
    assert precheck.min_duration["MOB-7012"] == 2
    assert precheck.cancelled == {}


def test_greedy_is_deterministic(dataset: Dataset) -> None:
    state = build_state(dataset, BASE.input)

    first = plan_greedy(state, BASE.solve).to_dict()
    second = plan_greedy(state, BASE.solve).to_dict()

    first["meta"].pop("elapsed_seconds")
    second["meta"].pop("elapsed_seconds")

    assert first == second
