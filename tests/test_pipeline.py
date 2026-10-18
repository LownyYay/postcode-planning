from pathlib import Path

import pytest

from src.assumptions import (
    Assumptions,
    load_assumptions,
)
from src.capacity import (
    calculate_remaining_role_hours,
    calculate_role_capacity,
    calculate_team_sp_capacity,
)
from src.star_map import analyze_bus_factor, role_bus_factor
from src.dataset import Dataset, load_dataset
from src.validator import validate_dataset


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


def test_raw_excel_size(
    dataset: Dataset,
) -> None:
    raw = dataset.raw

    assert raw.shape == (185, 46)


def test_tasks_import(
    dataset: Dataset,
) -> None:
    tasks = dataset.tasks

    assert len(tasks) == 45
    assert tasks["task_id"].nunique() == 45
    assert tasks["Номер инициативы"].nunique() == 15

    status_counts = (
        tasks["status"]
        .value_counts()
        .to_dict()
    )

    assert status_counts == {
        "ToDo": 31,
        "Done": 8,
        "InProgress": 6,
    }


def test_estimates_import(
    dataset: Dataset,
) -> None:
    tasks = dataset.tasks
    estimates = dataset.estimates

    assert len(estimates) == 258
    assert estimates["task_id"].nunique() == 45

    task_ids = set(tasks["task_id"])
    estimate_task_ids = set(estimates["task_id"])

    assert len(
        estimate_task_ids - task_ids
    ) == 0


def test_spent_hours_import(
    dataset: Dataset,
) -> None:
    spent_hours = dataset.spent_hours

    assert len(spent_hours) == 24
    assert spent_hours["task_id"].nunique() == 6

    assert (
        spent_hours["spent_hours"].sum()
        == pytest.approx(602)
    )


def test_engineers_import(
    dataset: Dataset,
) -> None:
    engineers = dataset.engineers

    assert len(engineers) == 34
    assert engineers["engineer_id"].nunique() == 30

    team_counts = (
        engineers
        .groupby("engineer_id")["team_id"]
        .nunique()
    )

    assert (team_counts > 1).sum() == 4


def test_history_and_sp_capacity(
    dataset: Dataset,
) -> None:
    team_history = dataset.team_history

    assert len(team_history) == 12
    assert team_history["team_id"].nunique() == 6

    capacity = calculate_team_sp_capacity(
        team_history
    )

    actual = dict(
        zip(
            capacity["team_id"],
            capacity["sprint_sp_capacity"],
        )
    )

    expected = {
        "Team-Alpha": 10.8,
        "Team-Beta": 8.4,
        "Team-Delta": 7.6,
        "Team-K": 12.8,
        "Team-L": 8.0,
        "Team-Platform": 7.2,
    }

    assert actual == pytest.approx(expected)


def test_dependencies_import(
    dataset: Dataset,
) -> None:
    tasks = dataset.tasks
    dependencies = dataset.dependencies

    assert len(dependencies) == 19

    task_ids = set(tasks["task_id"])

    dependency_ids = (
        set(dependencies["blocking_task_id"])
        | set(dependencies["blocked_task_id"])
    )

    assert len(
        dependency_ids - task_ids
    ) == 0

    pair_counts = (
        dependencies
        .groupby(
            [
                "blocking_task_id",
                "blocked_task_id",
            ]
        )
        .size()
    )

    assert (pair_counts > 1).sum() == 0


def test_validation_report(
    dataset: Dataset,
) -> None:
    issues = validate_dataset(
        tasks=dataset.tasks,
        estimates=dataset.estimates,
        dependencies=dataset.dependencies,
        engineers=dataset.engineers,
    )

    errors = [
        issue
        for issue in issues
        if issue.level == "ERROR"
    ]

    warnings = [
        issue
        for issue in issues
        if issue.level == "WARNING"
    ]

    assert len(issues) == 30
    assert len(errors) == 0
    assert len(warnings) == 30

    codes = [
        issue.code
        for issue in issues
    ]

    assert codes.count("HOURS_MISMATCH") == 24
    assert codes.count("ROLE_WITHOUT_ENGINEERS") == 6


def test_team_available_hours(
    dataset: Dataset,
) -> None:
    role_capacity = calculate_role_capacity(
        dataset.engineers
    )

    actual = (
        role_capacity
        .groupby("team_id")["available_hours"]
        .sum()
        .to_dict()
    )

    expected = {
        "Team-Alpha": 240,
        "Team-Beta": 200,
        "Team-Delta": 200,
        "Team-K": 360,
        "Team-L": 960,
        "Team-Platform": 440,
    }

    assert actual == pytest.approx(expected)


def test_remaining_hours(
    dataset: Dataset,
) -> None:
    remaining = calculate_remaining_role_hours(
        estimates=dataset.estimates,
        spent_hours=dataset.spent_hours,
    )

    actual = (
        remaining
        .groupby("task_id")["remaining_hours"]
        .sum()
        .to_dict()
    )

    expected = {
        "DB-202": 247,
        "KP-3898": 162,
        "MOB-7011": 0,
        "MP-102": 86,
        "SRV-4011": 130,
        "SRV-4041": 64,
    }

    for task_id, expected_hours in expected.items():
        assert actual[task_id] == pytest.approx(
            expected_hours
        )


def test_dataset_properties(
    dataset: Dataset,
) -> None:
    assert len(dataset.task_ids) == 45
    assert len(dataset.estimate_task_ids) == 45
    assert len(dataset.estimates_without_tasks) == 0
    assert len(dataset.missing_dependency_task_ids) == 0
    assert len(dataset.repeated_dependency_pairs) == 0
    assert len(dataset.roles_without_engineers) == 6


def test_task_dates(
    dataset: Dataset,
) -> None:
    tasks = dataset.tasks

    created_at = tasks["created_at"]
    planned_end = tasks["planned_end"]
    actual_end = tasks["actual_end"]

    assert created_at.notna().sum() == 45
    assert planned_end.notna().sum() == 45

    # Фактическая дата завершения заполнена
    # только у закрытых задач.
    assert actual_end.notna().sum() == 8

    assert (
        created_at.min().date().isoformat()
        == "2025-09-01"
    )

    assert (
        planned_end.max().date().isoformat()
        == "2026-08-01"
    )


def test_engineer_skills(
    dataset: Dataset,
) -> None:
    engineers = dataset.engineers

    assert len(dataset.unique_skills) == 116

    # Запятая внутри скобок не разделяет навыки.
    assert (
        "CI/CD (GitLab, Jenkins)"
        in dataset.unique_skills
    )

    first_engineer = engineers[
        engineers["engineer_id"] == "ENG-401"
    ].iloc[0]

    assert first_engineer["skills"] == [
        "Java",
        "Spring Boot",
        "PostgreSQL",
        "Hibernate",
        "Apache Kafka",
    ]


def test_bus_factor_counts_people(
    dataset: Dataset,
) -> None:
    people, _, bus_factor = analyze_bus_factor(
        dataset.engineers
    )

    # Совместитель — один человек,
    # а не две строки распределения.
    assert len(people) == 30

    shared = people[
        people["engineer_id"] == "ENG-405"
    ].iloc[0]

    assert shared["team_id"] == "Team-Alpha, Team-L"

    # Навыки совместителей не удваиваются.
    assert bus_factor["Figma"] == 1
    assert bus_factor["Linux"] == 1

    critical = [
        skill
        for skill, count in bus_factor.items()
        if count == 1
    ]

    assert len(critical) == 86


def test_reference_lists(
    dataset: Dataset,
) -> None:
    references = dataset.reference_lists

    assert len(references["goal_options"]) == 8
    assert len(references["variance_reasons"]) == 5
    assert len(references["closure_results"]) == 3

    assert (
        "Отсутствие ресурсов"
        in references["variance_reasons"]
    )

    assert (
        "планируемый результат работ достигнут"
        in references["closure_results"]
    )


def test_load_assumptions() -> None:
    assumptions = load_assumptions()

    assert assumptions == Assumptions(
        focus_factor=0.8,
        hours_per_sprint=80,
        sprint_count=6,
    )


def test_load_assumptions_defaults(
    tmp_path: Path,
) -> None:
    missing_path = (
        tmp_path
        / "missing_assumptions.yaml"
    )

    assumptions = load_assumptions(
        path=missing_path,
    )

    assert assumptions == Assumptions()

    partial_path = (
        tmp_path
        / "partial_assumptions.yaml"
    )

    partial_path.write_text(
        "input:\n  focus_factor: 0.7\n",
        encoding="utf-8",
    )

    partial_assumptions = load_assumptions(
        path=partial_path,
    )

    assert partial_assumptions == Assumptions(
        focus_factor=0.7,
        hours_per_sprint=80,
        sprint_count=6,
    )


def test_role_spellings_are_unified() -> None:
    """
    Одна роль, записанная разными способами, сводится к одному названию.
    """

    from src.normalizer import role_key, unify_roles

    variants = ["Дев_опс", "Дев опс", "Дев-опс", "ДевОпс", "девопс", " Девопс "]

    assert len({role_key(v) for v in variants}) == 1
    assert role_key("Тёстировщик") == role_key("Тестировщик")
    assert role_key("Аналитик") != role_key("Аналитик 1С")

    mapping = unify_roles(variants + ["Аналитик", "аналитик", "Аналитик"], {"ДевОпс": "Девопс"})

    assert {mapping[v.strip()] for v in variants} == {"Девопс"}
    assert mapping["аналитик"] == "Аналитик"


def test_similar_profiles_group_by_stack(
    dataset: Dataset,
) -> None:
    people, unique, _ = analyze_bus_factor(dataset.engineers)

    cluster = dict(zip(people["engineer_id"], people["cluster_id"]))

    # Похожие стеки — в одной группе: два аналитика, два фронтенд-разработчика.
    assert cluster["ENG-404"] == cluster["ENG-425"] != -1
    assert cluster["ENG-406"] == cluster["ENG-407"] != -1

    # У единственных девопса и архитектора похожих коллег нет.
    assert {"ENG-418", "ENG-419"} <= set(unique["engineer_id"])
    assert len(unique) == 17


def test_role_bus_factor_names_work_at_risk(
    dataset: Dataset,
) -> None:
    people_frame, _, _ = analyze_bus_factor(dataset.engineers)
    engineers = dataset.engineers
    people = [
        {
            "id": engineer_id,
            "roles": sorted(set(rows["role"])),
            "allocations": dict(zip(rows["team_id"], rows["capacity_rate"])),
        }
        for engineer_id, rows in engineers.groupby("engineer_id")
    ]
    remaining = calculate_remaining_role_hours(dataset.estimates, dataset.spent_hours)
    active = dataset.tasks[dataset.tasks["status"].isin(["ToDo", "InProgress"])]
    tasks = [
        {
            "id": row["task_id"],
            "team": row["team_id"],
            "initiative": row["Номер инициативы"],
            "remaining_hours": {
                role: hours
                for role, hours in remaining[remaining["task_id"] == row["task_id"]]
                .set_index("role")["remaining_hours"].items()
            },
        }
        for _, row in active.iterrows()
    ]

    rows = {row["role"]: row for row in role_bus_factor(people, tasks)}

    # Совместитель — один человек: у девопса ENG-419 две команды и полная ставка.
    devops = rows["Девопс"]
    assert devops["holders"] == ["ENG-419"]
    assert devops["bf"] == 1
    assert devops["fund_hours"] == pytest.approx(80)
    assert devops["holder_teams"] == ["Team-K", "Team-Platform"]

    # Без единственного носителя встают все работы роли.
    assert len(devops["tasks"]) >= 15
    assert len(devops["initiatives"]) >= 10

    # Роль без носителей — BF = 0, работа вне штата.
    assert rows["Руководитель проекта"]["bf"] == 0
    assert rows["Руководитель проекта"]["demand_hours"] > 400

    # Сортировка: сначала роли с одним носителем.
    first = role_bus_factor(people, tasks)[0]
    assert first["bf"] == 1
