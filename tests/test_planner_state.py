from pathlib import Path

import pytest

from src.dataset import Dataset, load_dataset
from src.planner import (
    InputConfig,
    PlannerConfig,
    PlanningState,
    SolveConfig,
    build_state,
    load_planner_config,
)


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


@pytest.fixture(scope="module")
def state(dataset: Dataset) -> PlanningState:
    # Конфиг задаётся явно, а не через значения по умолчанию.
    return build_state(
        dataset,
        InputConfig(
            inprogress_sp="by_hours",
            initiative_rung="max",
        ),
    )


def test_config_file_matches_defaults() -> None:
    assert load_planner_config() == PlannerConfig()


def test_config_rejects_unknown_values(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        SolveConfig(role_pool="everyone")

    with pytest.raises(ValueError):
        SolveConfig(min_progress=0.5)

    with pytest.raises(ValueError):
        InputConfig(inprogress_sp="half")

    bad_key = tmp_path / "bad_key.yaml"
    bad_key.write_text("solve:\n  unknown_key: 1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_planner_config(bad_key)

    # Параметр входа в разделе правил решения — ошибка.
    wrong_section = tmp_path / "wrong_section.yaml"
    wrong_section.write_text("solve:\n  capacity_scale: 0.8\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_planner_config(wrong_section)


def test_state_accepts_only_input_config(
    dataset: Dataset,
) -> None:
    with pytest.raises(TypeError):
        build_state(dataset, SolveConfig())

    with pytest.raises(TypeError):
        build_state(dataset, PlannerConfig())


def test_open_tasks_and_initiatives(
    state: PlanningState,
) -> None:
    assert len(state.tasks) == 37
    assert len(state.done_tasks) == 8
    assert len(state.initiatives) == 15
    assert state.quarter == 6
    assert state.horizon == 12

    # Порядок обхода не зависит от порядка строк в файле.
    assert list(state.tasks) == sorted(state.tasks)


def test_team_capacity(
    state: PlanningState,
    dataset: Dataset,
) -> None:
    assert state.team_capacity == pytest.approx(
        {
            "Team-Alpha": 10.8,
            "Team-Beta": 8.4,
            "Team-Delta": 7.6,
            "Team-K": 12.8,
            "Team-L": 8.0,
            "Team-Platform": 7.2,
        }
    )

    reduced = build_state(
        dataset,
        InputConfig(capacity_scale=0.8),
    )

    assert reduced.team_capacity["Team-Platform"] == pytest.approx(5.76)


def test_remaining_hours_by_role(
    state: PlanningState,
) -> None:
    assert state.tasks["DB-202"].remaining_hours == pytest.approx(
        {
            "Архитектор решений": 21,
            "Девопс": 30,
            "Разработчик .net": 90,
            "Разработчик Java": 106,
        }
    )

    assert state.team_fund("Team-Platform", "Разработчик .net") == 80
    assert state.global_fund("Разработчик Java") == 560


def test_inprogress_sp_modes(
    state: PlanningState,
    dataset: Dataset,
) -> None:
    # by_hours: доля оценки, пропорциональная остатку часов.
    assert state.tasks["DB-202"].sp_effective == pytest.approx(8 * 247 / 324)

    # Задача в работе без остатка ничего не стоит.
    assert state.tasks["MOB-7011"].zero_remaining
    assert state.tasks["MOB-7011"].sp_effective == 0

    # ToDo всегда стоит полную оценку.
    assert state.tasks["MOB-7012"].sp_effective == 11

    full = build_state(
        dataset,
        InputConfig(inprogress_sp="full"),
    )

    assert full.tasks["DB-202"].sp_effective == 8
    assert full.tasks["MOB-7011"].sp_effective == 5


def test_zero_remaining_warning(
    state: PlanningState,
) -> None:
    assert any(
        warning.startswith("MOB-7011")
        for warning in state.warnings
    )


def test_missing_roles(
    state: PlanningState,
) -> None:
    assert len(state.missing_roles) == 6
    assert "Руководитель проекта" in state.missing_roles

    needing = [
        task_id
        for task_id, task in state.tasks.items()
        if set(task.remaining_hours) & state.missing_roles
    ]

    assert len(needing) == 26


def test_dependencies_and_references(
    state: PlanningState,
) -> None:
    assert len(state.dependencies) == 19
    assert "Превышение плана" in state.reason_codes
    assert "не будет взято в квартал" in state.goal_options


def test_initiative_rung_modes(
    state: PlanningState,
    dataset: Dataset,
) -> None:
    assert state.initiatives["PRODF-7126"].rung == 92

    mean = build_state(
        dataset,
        InputConfig(initiative_rung="mean"),
    )

    assert mean.initiatives["PRODF-7126"].rung == pytest.approx(268 / 3)
