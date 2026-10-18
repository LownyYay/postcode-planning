from dataclasses import dataclass

import pandas as pd


@dataclass
class ValidationIssue:
    """
    Одна найденная проблема в датасете.
    """

    level: str
    code: str
    message: str
    entity_id: str | None = None


def validate_dataset(
    tasks: pd.DataFrame,
    estimates: pd.DataFrame,
    dependencies: pd.DataFrame,
    engineers: pd.DataFrame,
) -> list[ValidationIssue]:
    """
    Проверяет связанные таблицы датасета.
    """

    issues: list[ValidationIssue] = []

    task_ids = set(tasks["task_id"])
    estimate_task_ids = set(estimates["task_id"])

    # 1. Проверяем дубли task_id.
    duplicate_task_ids = (
        tasks.loc[
            tasks["task_id"].duplicated(
                keep=False,
            ),
            "task_id",
        ]
        .unique()
        .tolist()
    )

    for task_id in sorted(duplicate_task_ids):
        issues.append(
            ValidationIssue(
                level="ERROR",
                code="DUPLICATE_TASK_ID",
                entity_id=task_id,
                message=(
                    f"Задача {task_id} встречается "
                    f"в Tasks несколько раз."
                ),
            )
        )

    # 2. Проверяем статусы задач.
    allowed_statuses = {
        "ToDo",
        "InProgress",
        "Done",
    }

    invalid_status_rows = tasks[
        ~tasks["status"].isin(
            allowed_statuses
        )
    ]

    for _, task in invalid_status_rows.iterrows():
        issues.append(
            ValidationIssue(
                level="ERROR",
                code="INVALID_TASK_STATUS",
                entity_id=task["task_id"],
                message=(
                    f"У задачи {task['task_id']} "
                    f"неизвестный статус: "
                    f"{task['status']}."
                ),
            )
        )

    # 3. В смете есть задача,
    # которой нет в Tasks.
    estimates_without_tasks = sorted(
        estimate_task_ids - task_ids
    )

    for task_id in estimates_without_tasks:
        issues.append(
            ValidationIssue(
                level="WARNING",
                code="ESTIMATE_WITHOUT_TASK",
                entity_id=task_id,
                message=(
                    f"Для {task_id} есть смета, "
                    f"но задача отсутствует в Tasks."
                ),
            )
        )

    # 4. В Tasks есть задача без сметы.
    tasks_without_estimates = sorted(
        task_ids - estimate_task_ids
    )

    for task_id in tasks_without_estimates:
        issues.append(
            ValidationIssue(
                level="ERROR",
                code="TASK_WITHOUT_ESTIMATE",
                entity_id=task_id,
                message=(
                    f"Для задачи {task_id} "
                    f"не найдена смета по ролям."
                ),
            )
        )

    # 5. Сравниваем estimated_hh
    # с суммой сметы по ролям.
    estimate_hours_by_task = (
        estimates
        .groupby("task_id")["estimated_hours"]
        .sum()
    )

    declared_hours_by_task = (
        tasks
        .set_index("task_id")["estimated_hh"]
    )

    common_task_ids = sorted(
        task_ids & estimate_task_ids
    )

    for task_id in common_task_ids:
        declared_hours = pd.to_numeric(
            declared_hours_by_task[task_id],
            errors="coerce",
        )

        calculated_hours = (
            estimate_hours_by_task[task_id]
        )

        if pd.isna(declared_hours):
            issues.append(
                ValidationIssue(
                    level="ERROR",
                    code="INVALID_ESTIMATED_HOURS",
                    entity_id=task_id,
                    message=(
                        f"У задачи {task_id} "
                        f"не заполнено estimated_hh."
                    ),
                )
            )

            continue

        if abs(
            float(declared_hours)
            - float(calculated_hours)
        ) > 0.001:
            issues.append(
                ValidationIssue(
                    level="WARNING",
                    code="HOURS_MISMATCH",
                    entity_id=task_id,
                    message=(
                        f"У задачи {task_id} "
                        f"estimated_hh = "
                        f"{declared_hours:.0f}, "
                        f"а сумма сметы по ролям = "
                        f"{calculated_hours:.0f}."
                    ),
                )
            )

    # 6. Проверяем задачи в зависимостях.
    dependency_task_ids = (
        set(dependencies["blocking_task_id"])
        | set(dependencies["blocked_task_id"])
    )

    missing_dependency_task_ids = sorted(
        dependency_task_ids - task_ids
    )

    for task_id in missing_dependency_task_ids:
        issues.append(
            ValidationIssue(
                level="ERROR",
                code="DEPENDENCY_TASK_NOT_FOUND",
                entity_id=task_id,
                message=(
                    f"Зависимость ссылается "
                    f"на отсутствующую задачу "
                    f"{task_id}."
                ),
            )
        )

    # 7. Проверяем повторяющиеся
    # пары зависимостей.
    dependency_pair_counts = (
        dependencies
        .groupby(
            [
                "blocking_task_id",
                "blocked_task_id",
            ]
        )
        .size()
    )

    repeated_pairs = dependency_pair_counts[
        dependency_pair_counts > 1
    ]

    for (
        blocking_task_id,
        blocked_task_id,
    ), count in repeated_pairs.items():
        issues.append(
            ValidationIssue(
                level="WARNING",
                code="DUPLICATE_DEPENDENCY",
                entity_id=(
                    f"{blocking_task_id}"
                    f"->{blocked_task_id}"
                ),
                message=(
                    f"Зависимость "
                    f"{blocking_task_id} → "
                    f"{blocked_task_id} "
                    f"встречается {count} раза."
                ),
            )
        )

    # 8. Проверяем ставки в каждой строке.
    for _, engineer in engineers.iterrows():
        capacity_rate = engineer["capacity_rate"]

        if (
            pd.isna(capacity_rate)
            or capacity_rate <= 0
            or capacity_rate > 1
        ):
            issues.append(
                ValidationIssue(
                    level="ERROR",
                    code="INVALID_CAPACITY_RATE",
                    entity_id=engineer["engineer_id"],
                    message=(
                        f"У инженера "
                        f"{engineer['engineer_id']} "
                        f"некорректная ставка: "
                        f"{capacity_rate}."
                    ),
                )
            )

    # 9. Проверяем суммарную ставку инженера.
    total_capacity_by_engineer = (
        engineers
        .groupby("engineer_id")["capacity_rate"]
        .sum()
    )

    overloaded_engineers = (
        total_capacity_by_engineer[
            total_capacity_by_engineer > 1.000001
        ]
    )

    for engineer_id, total_rate in (
        overloaded_engineers.items()
    ):
        issues.append(
            ValidationIssue(
                level="ERROR",
                code="ENGINEER_OVERALLOCATED",
                entity_id=engineer_id,
                message=(
                    f"Суммарная ставка инженера "
                    f"{engineer_id} равна "
                    f"{total_rate:.2f}, "
                    f"что превышает 1.0."
                ),
            )
        )

    # 10. Проверяем роли, для которых
    # нет ни одного инженера.
    estimate_roles = set(estimates["role"])
    engineer_roles = set(engineers["role"])

    roles_without_engineers = sorted(
        estimate_roles - engineer_roles
    )

    for role in roles_without_engineers:
        issues.append(
            ValidationIssue(
                level="WARNING",
                code="ROLE_WITHOUT_ENGINEERS",
                entity_id=role,
                message=(
                    f"В смете требуется роль "
                    f"«{role}», но инженеров "
                    f"с такой ролью нет."
                ),
            )
        )

    return issues