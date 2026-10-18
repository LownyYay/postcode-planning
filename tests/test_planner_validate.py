"""
Проверки валидатора плана на маленьком выдуманном примере.

Две команды, три задачи, одна связь — правильный ответ
считается в уме, поэтому тесты проверяют правила,
а не контрольные числа конкретного датасета.
"""

from dataclasses import asdict

import pytest

from src.planner.assemble import assemble_plan
from src.planner.config import InputConfig, SolveConfig
from src.planner.result import (
    REASON_NO_RESOURCES,
    REASON_NOT_READY,
    Meta,
    PlanResult,
    Reason,
)
from src.planner.state import (
    EngineerInput,
    InitiativeInput,
    PlanningState,
    TaskInput,
)
from src.planner.validate import (
    PlanValidationError,
    find_violations,
    validate_plan,
)


REASON_CODES = (
    "Не оценивалась",
    "Отсутствие ресурсов",
    "Отсутствует готовность смежных команд",
    "Превышение плана",
    "Изменились требования",
)


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


def make_solve(role_pool: str = "team") -> SolveConfig:
    return SolveConfig(
        role_pool=role_pool,
        gap_planned=1,
        gap_done=0,
        min_progress=0.1,
    )


def make_state(
    t1_dev_hours: float = 80,
    t3_sp: float = 5,
    dev_in_team_b: bool = False,
) -> PlanningState:
    """
    Команда A: разработчик на полную ставку, 10 SP за спринт.
    Команда B: тестировщик на 0.5 ставки, 5 SP за спринт.
    T1 → T2 в инициативе I1, T3 в инициативе I2.
    """

    engineers = [
        EngineerInput("E1", "A", "Dev", 1.0),
        EngineerInput("E2", "B", "QA", 0.5),
    ]

    if dev_in_team_b:
        engineers.append(EngineerInput("E3", "B", "Dev", 0.5))

    fund: dict[tuple[str, str], float] = {}

    for engineer in engineers:
        key = (engineer.team, engineer.role)
        fund[key] = fund.get(key, 0.0) + engineer.capacity_rate * 80

    tasks = {
        "T1": make_task("T1", "A", "I1", 6, {"Dev": t1_dev_hours}),
        "T2": make_task("T2", "A", "I1", 4, {"Dev": 40}),
        "T3": make_task("T3", "B", "I2", t3_sp, {"QA": 40}),
    }

    return PlanningState(
        input_config=InputConfig(),
        current_sprint=0,
        tasks=tasks,
        done_tasks={"D0": 0},
        initiatives={
            "I1": InitiativeInput("I1", 80, ("T1", "T2"), ()),
            "I2": InitiativeInput("I2", 70, ("T3",), ("D0",)),
        },
        team_capacity={"A": 10.0, "B": 5.0},
        team_role_fund=fund,
        engineers=tuple(engineers),
        dependencies=(("D0", "T3"), ("T1", "T2")),
        missing_roles=frozenset(),
        reason_codes=REASON_CODES,
    )


def make_plan(
    state: PlanningState,
    progress: dict[str, dict[int, float]],
    cancelled: dict[str, Reason] | None = None,
    solve: SolveConfig | None = None,
) -> PlanResult:
    solve = solve or make_solve()

    meta = Meta(
        engine="test",
        solver=None,
        status="greedy",
        elapsed_seconds=0.0,
        current_sprint=state.current_sprint,
        input_config=asdict(state.input_config),
        solve_config=asdict(solve),
    )

    return assemble_plan(state, solve, progress, cancelled or {}, meta)


VALID_PROGRESS = {
    "T1": {1: 1.0},
    # T1 закрыта в спринте 1, зазор 1 → T2 не раньше спринта 3.
    "T2": {3: 1.0},
    "T3": {1: 1.0},
}


def violations_of(state, progress, cancelled=None) -> list[str]:
    return find_violations(make_plan(state, progress, cancelled), state)


def test_valid_plan_passes() -> None:
    state = make_state()
    plan = make_plan(state, VALID_PROGRESS)

    validate_plan(plan, state)

    assert plan.initiatives_done == 2
    assert plan.included_task_ids == ["T1", "T2", "T3"]


def test_team_sp_over_capacity() -> None:
    state = make_state(t3_sp=6)

    violations = violations_of(state, VALID_PROGRESS)

    assert any("B, спринт 1" in text for text in violations)


def test_role_hours_over_team_fund() -> None:
    state = make_state(t1_dev_hours=100)

    violations = violations_of(state, VALID_PROGRESS)

    assert any("A / Dev, спринт 1" in text for text in violations)


def test_included_task_must_reach_100_percent() -> None:
    state = make_state()
    plan = make_plan(state, {**VALID_PROGRESS, "T1": {1: 0.5}})

    plan.tasks["T1"].status = "included"

    with pytest.raises(PlanValidationError) as error:
        validate_plan(plan, state)

    assert any(
        "T1: включена, но не завершается" in text
        for text in error.value.violations
    )


def test_dependency_gap() -> None:
    state = make_state()

    violations = violations_of(state, {**VALID_PROGRESS, "T2": {2: 1.0}})

    assert any("нужен зазор 1" in text for text in violations)


def test_done_blocker_allows_next_sprint() -> None:
    # D0 закрыта до квартала, gap_done = 0 → T3 можно в спринте 1.
    state = make_state()

    assert violations_of(state, VALID_PROGRESS) == []


def test_blocked_included_while_blocker_postponed() -> None:
    state = make_state()

    violations = violations_of(
        state,
        {**VALID_PROGRESS, "T1": {7: 1.0}, "T2": {3: 1.0}},
    )

    assert any(
        "T2: включена, хотя блокирующая T1" in text
        for text in violations
    )


def test_initiative_flag_must_match_tasks() -> None:
    state = make_state()
    plan = make_plan(state, VALID_PROGRESS)

    plan.initiatives["I1"].done_in_quarter = False

    violations = find_violations(plan, state)

    assert any(text.startswith("I1:") for text in violations)


def test_postponed_task_needs_reason() -> None:
    state = make_state()
    plan = make_plan(state, {**VALID_PROGRESS, "T2": {7: 1.0}})

    assert plan.tasks["T2"].status == "postponed"
    assert plan.tasks["T2"].reason_code == "Превышение плана"
    assert find_violations(plan, state) == []

    plan.tasks["T2"].reason_text = None

    assert any(
        "T2: статус postponed без причины" in text
        for text in find_violations(plan, state)
    )


def test_reason_code_from_reference_list() -> None:
    state = make_state()
    plan = make_plan(state, {**VALID_PROGRESS, "T2": {7: 1.0}})

    plan.tasks["T2"].reason_code = "Просто так"

    assert any(
        "не из справочника" in text
        for text in find_violations(plan, state)
    )


def test_min_progress_in_active_sprints() -> None:
    state = make_state()

    violations = violations_of(
        state,
        {**VALID_PROGRESS, "T1": {1: 0.95, 2: 0.05}, "T2": {4: 1.0}},
    )

    assert any("меньше минимальной" in text for text in violations)


def test_gap_inside_work_window() -> None:
    state = make_state()

    violations = violations_of(
        state,
        {**VALID_PROGRESS, "T1": {1: 0.5, 3: 0.5}, "T2": {5: 1.0}},
    )

    assert any("T1: в спринте 2 доля 0.000" in text for text in violations)


def test_started_in_quarter_must_finish_in_quarter() -> None:
    state = make_state()

    violations = violations_of(
        state,
        {**VALID_PROGRESS, "T1": {6: 0.5, 7: 0.5}, "T2": {9: 1.0}},
    )

    assert any("начата в квартале" in text for text in violations)


def test_cancelled_blocker_cancels_blocked() -> None:
    state = make_state()
    cancelled = {
        "T1": Reason(REASON_NO_RESOURCES, "Нет разработчика"),
    }

    violations = violations_of(state, {"T2": {3: 1.0}, "T3": {1: 1.0}}, cancelled)

    assert any("T2: блокирующая T1 отменена" in text for text in violations)

    fixed = {
        **cancelled,
        "T2": Reason(REASON_NOT_READY, "Отменена блокирующая T1"),
    }

    plan = make_plan(state, {"T3": {1: 1.0}}, fixed)

    validate_plan(plan, state)

    assert plan.initiatives["I1"].done_in_quarter is False
    assert plan.initiatives["I1"].forecast_sprint is None
    assert plan.initiatives_done == 1


def test_borrowing_is_recorded() -> None:
    # Разработчику A нужно 100 ч при фонде 80:
    # 20 ч берутся у команды B.
    state = make_state(
        t1_dev_hours=100,
        dev_in_team_b=True,
    )

    plan = make_plan(state, VALID_PROGRESS, solve=make_solve("team_borrow"))

    validate_plan(plan, state)

    assert [
        (row.team, row.role, row.sprint, round(row.hours, 6))
        for row in plan.borrowing
    ] == [("A", "Dev", 1, 20.0)]

    plan.borrowing = []

    assert any(
        "заимствовано 20.00" in text
        for text in find_violations(plan, state)
    )


def test_global_fund_limit_in_borrow_mode() -> None:
    # Во всей организации разработчиков на 120 ч, нужно 140.
    state = make_state(
        t1_dev_hours=140,
        dev_in_team_b=True,
    )

    plan = make_plan(state, VALID_PROGRESS, solve=make_solve("team_borrow"))
    violations = find_violations(plan, state)

    assert any("фонде организации" in text for text in violations)


def test_bottlenecks_and_next_sprint_demand() -> None:
    state = make_state()
    plan = make_plan(state, VALID_PROGRESS)

    # B расходует всю ёмкость в спринте 1, A — все часы Dev.
    keys = {
        (row.kind, row.key, row.sprint)
        for row in plan.load.bottlenecks
    }

    assert ("team_sp", "B", 1) in keys
    assert ("role_hours", "A / Dev", 1) in keys

    # Готовы к спринту 1: T1 и T3; T2 ждёт T1.
    demand = {
        (row.team, row.role): row.ready_hours
        for row in plan.next_sprint_demand
    }

    assert demand == {("A", "Dev"): 80, ("B", "QA"): 40}


def test_json_roundtrip() -> None:
    state = make_state(
        t1_dev_hours=100,
        dev_in_team_b=True,
    )

    plan = make_plan(
        state,
        {**VALID_PROGRESS, "T2": {7: 1.0}},
        solve=make_solve("team_borrow"),
    )
    restored = PlanResult.from_json(plan.to_json())

    assert restored == plan
    assert restored.tasks["T2"].progress == {7: 1.0}

    validate_plan(restored, state)


def test_rules_come_from_plan_meta() -> None:
    # Без явных правил валидатор берёт их из меты плана.
    state = make_state(
        t1_dev_hours=100,
        dev_in_team_b=True,
    )

    plan = make_plan(state, VALID_PROGRESS, solve=make_solve("team_borrow"))

    assert find_violations(plan, state) == []

    # Те же доли при строгом пуле своей команды нарушают фонд A.
    assert any(
        "A / Dev" in text
        for text in find_violations(plan, state, make_solve("team"))
    )
