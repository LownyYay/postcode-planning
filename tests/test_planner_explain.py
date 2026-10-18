"""
Причины переноса и контрфакты на реальном датасете.
"""

from functools import lru_cache

from src.dataset import load_dataset
from src.planner import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    build_state,
    run_scenario,
    validate_plan,
)
from src.planner.result import REASON_NO_RESOURCES, REASON_OVER_PLAN


FULL = PlannerConfig(
    input=InputConfig(inprogress_sp="full"),
    solve=SolveConfig(
        role_pool="team_borrow",
        missing_roles="external",
        counterfactuals=True,
        counterfactual_limit=10,
        counterfactual_time_limit=60,
    ),
)


@lru_cache(maxsize=None)
def dataset():
    return load_dataset()


@lru_cache(maxsize=None)
def full_plan():
    return run_scenario(dataset(), FULL)


def test_initiative_counterfactual() -> None:
    result = full_plan()
    initiative = result.initiatives["PRODF-7120"]

    assert not initiative.done_in_quarter

    # Спасение PRODF-7120 стоит PRODF-7121: число инициатив не растёт.
    counterfactual = initiative.counterfactual

    assert counterfactual["status"] == "feasible"
    assert [item["id"] for item in counterfactual["lost"]] == ["PRODF-7121"]
    assert counterfactual["initiatives_done"] == result.initiatives_done

    assert "PRODF-7121 (rung 80)" in initiative.reason_text


def test_postponed_task_reason() -> None:
    task = full_plan().tasks["KP-3898"]

    assert task.status == "postponed"
    assert task.reason_code == REASON_OVER_PLAN
    assert "Team-Platform перегружена: 45.0 SP спроса при 43.2 за квартал" in task.reason_text
    assert "Прогноз завершения — спринт 7" in task.reason_text

    # Её инициатива не завершается и так, поэтому включение ничего не стоит.
    assert task.counterfactual["status"] == "feasible"
    assert task.counterfactual["lost"] == []
    assert "всё равно не завершается" in task.reason_text


def test_every_decision_has_reason() -> None:
    result = full_plan()

    for task in result.tasks.values():
        if task.status != "included":
            assert task.reason_code and task.reason_text

    for initiative in result.initiatives.values():
        if not initiative.done_in_quarter:
            assert initiative.reason_text


def test_counterfactual_budget() -> None:
    config = PlannerConfig(
        input=InputConfig(capacity_scale=0.5),
        solve=SolveConfig(counterfactual_limit=2),
    )

    result = run_scenario(dataset(), config)

    counterfactuals = [
        item.counterfactual
        for item in list(result.initiatives.values()) + list(result.tasks.values())
        if item.counterfactual is not None
    ]

    computed = [item for item in counterfactuals if item["status"] != "not_computed"]
    skipped = [item for item in counterfactuals if item["status"] == "not_computed"]

    assert len(computed) == 2
    assert skipped
    assert all("исчерпан бюджет" in item["text"] for item in skipped)
    assert result.meta.objective["counterfactuals_run"] == 2


def test_cancelled_task_reason_in_team_pool() -> None:
    config = PlannerConfig(
        solve=SolveConfig(role_pool="team", counterfactuals=False),
    )

    result = run_scenario(dataset(), config)

    cancelled = [
        result.tasks[task_id]
        for task_id in result.tasks_with_status("cancelled")
    ]

    assert cancelled
    assert all(task.reason_code and task.reason_text for task in cancelled)
    assert any(
        task.reason_code == REASON_NO_RESOURCES and "нет роли" in task.reason_text
        for task in cancelled
    )

    # У незавершённых инициатив с отменёнными задачами контрфакт — «невыполнимо».
    blocked = [
        initiative
        for initiative in result.initiatives.values()
        if not initiative.done_in_quarter
    ]

    assert all(
        initiative.counterfactual["status"] == "infeasible"
        for initiative in blocked
        if any(
            result.tasks[task_id].status == "cancelled"
            for task_id in initiative.blocking_task_ids
        )
    )

    validate_plan(result, build_state(dataset(), config.input), config.solve)


def test_included_tasks_explain_goal_and_sprints() -> None:
    result = full_plan()

    for task in result.tasks.values():
        if task.status == "included":
            assert task.reason_text, task.task_id
            assert task.details["decision"] in ("include", "groundwork")

    # Задача растянута, потому что часов роли больше, чем фонд за спринт.
    db202 = result.tasks["DB-202"]
    assert "квартальной цели PRODF-7124" in db202.reason_text
    assert "Растянута на" in db202.reason_text

    # Зависимая задача начинается сразу, как позволяет зазор.
    mp103 = result.tasks["MP-103"]
    assert "MP-102 завершается в спринте" in mp103.reason_text


def test_postponed_work_gets_a_recommendation() -> None:
    result = full_plan()

    postponed = [task for task in result.tasks.values() if task.status == "postponed"]

    assert postponed
    assert all(task.details["decision"] in ("postpone", "cancel") for task in postponed)
    assert all("Рекомендация" in task.reason_text for task in postponed)

    # Начатая задача, приостановленная на квартал, объяснена отдельно.
    assert "уже в работе" in result.tasks["KP-3898"].reason_text

    # Сорванная инициатива: первопричина и что делать с её работами в квартале.
    initiative = result.initiatives["PRODF-7120"]
    assert "Причина: Team-Platform перегружена" in initiative.reason_text
    assert "Рекомендация: перенести инициативу" in initiative.reason_text
