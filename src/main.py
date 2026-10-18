from src.assumptions import load_assumptions
from src.capacity import (
    calculate_remaining_role_hours,
    calculate_role_capacity,
    calculate_team_sp_capacity,
)
from src.star_map import analyze_bus_factor
from src.dataset import load_dataset
from src.importer import find_row_by_text
from src.validator import validate_dataset


def main() -> None:
    dataset = load_dataset()
    assumptions = load_assumptions()

    print("Загрузка датасета...")

    print(
        f"Размер листа: "
        f"{dataset.raw.shape[0]} строк, "
        f"{dataset.raw.shape[1]} столбцов"
    )

    blocks = {
        "Tasks": "Tasks (",
        "Spent time": "Spent_time_roles",
        "Estimates": "Estimates.",
        "Dependencies": "Task_dependencies",
        "Engineers": "Engineers_profiles",
        "History": "Team_history",
    }

    print("\nНайденные блоки:")

    for block_name, search_text in blocks.items():
        row_index = find_row_by_text(
            dataset.raw,
            search_text,
        )

        # В Excel номера строк начинаются с единицы,
        # а индексы pandas начинаются с нуля.
        excel_row = row_index + 1

        print(
            f"- {block_name}: "
            f"строка Excel {excel_row}"
        )

    validation_issues = validate_dataset(
        tasks=dataset.tasks,
        estimates=dataset.estimates,
        dependencies=dataset.dependencies,
        engineers=dataset.engineers,
    )

    team_sp_capacity = (
        calculate_team_sp_capacity(
            dataset.team_history,
            focus_factor=(
                assumptions.focus_factor
            ),
            sprint_count=(
                assumptions.sprint_count
            ),
        )
    )

    role_capacity = calculate_role_capacity(
        dataset.engineers,
        hours_per_sprint=(
            assumptions.hours_per_sprint
        ),
    )

    remaining_role_hours = (
        calculate_remaining_role_hours(
            estimates=dataset.estimates,
            spent_hours=dataset.spent_hours,
        )
    )

    active_tasks = dataset.tasks[
        dataset.tasks["status"].isin(
            ["ToDo", "InProgress"]
        )
    ]

    initiatives_count = (
        dataset.tasks["Номер инициативы"]
        .nunique()
    )

    print("\nРезультаты импорта:")

    print(
        f"- Всего задач: "
        f"{len(dataset.tasks)}"
    )

    print(
        f"- Активных задач: "
        f"{len(active_tasks)}"
    )

    print(
        f"- Инициатив: "
        f"{initiatives_count}"
    )

    print("\nСтатусы:")

    status_counts = (
        dataset.tasks["status"]
        .value_counts()
    )

    for status, count in status_counts.items():
        print(f"- {status}: {count}")

    print("\nКоманды:")

    team_counts = (
        dataset.tasks["team_id"]
        .value_counts()
        .sort_index()
    )

    for team_id, count in team_counts.items():
        print(
            f"- {team_id}: "
            f"{count} задач"
        )

    print("\nРезультаты импорта сметы:")

    print(
        f"- Требований к ролям: "
        f"{len(dataset.estimates)}"
    )

    print(
        f"- Задач в смете: "
        f"{len(dataset.estimate_task_ids)}"
    )

    print(
        f"- Задач в смете без Tasks: "
        f"{len(dataset.estimates_without_tasks)}"
    )

    print("\nЛишние задачи в смете:")

    for task_id in dataset.estimates_without_tasks:
        print(f"- {task_id}")

    print("\nРезультаты импорта затраченных часов:")

    print(
        f"- Записей по ролям: "
        f"{len(dataset.spent_hours)}"
    )

    print(
        f"- Задач с затраченными часами: "
        f"{dataset.spent_hours['task_id'].nunique()}"
    )

    print(
        f"- Всего затрачено часов: "
        f"{dataset.spent_hours['spent_hours'].sum():.0f}"
    )

    print("\nЗатраченные часы по задачам:")

    spent_by_task = (
        dataset.spent_hours
        .groupby("task_id")["spent_hours"]
        .sum()
        .sort_index()
    )

    for task_id, hours in spent_by_task.items():
        print(
            f"- {task_id}: "
            f"{hours:.0f} часов"
        )

    print("\nРезультаты импорта инженеров:")

    print(
        f"- Строк распределения: "
        f"{len(dataset.engineers)}"
    )

    print(
        f"- Уникальных инженеров: "
        f"{dataset.engineers['engineer_id'].nunique()}"
    )

    engineer_team_counts = (
        dataset.engineers
        .groupby("engineer_id")["team_id"]
        .nunique()
    )

    shared_engineer_ids = (
        engineer_team_counts[
            engineer_team_counts > 1
        ]
        .index
        .tolist()
    )

    print(
        f"- Инженеров в нескольких командах: "
        f"{len(shared_engineer_ids)}"
    )

    print("\nИнженеры в нескольких командах:")

    for engineer_id in shared_engineer_ids:
        engineer_rows = dataset.engineers[
            dataset.engineers["engineer_id"] == engineer_id
        ]

        allocations = []

        for _, engineer_row in engineer_rows.iterrows():
            team_id = engineer_row["team_id"]
            capacity_rate = engineer_row["capacity_rate"]

            allocations.append(
                f"{team_id}: {capacity_rate:.1f}"
            )

        allocation_text = ", ".join(allocations)

        print(
            f"- {engineer_id}: "
            f"{allocation_text}"
        )

    print("\nЗаявленные технологии:")

    print(
        f"- Уникальных технологий: "
        f"{len(dataset.unique_skills)}"
    )

    print("\nДаты задач:")

    created_at = dataset.tasks["created_at"]
    planned_end = dataset.tasks["planned_end"]

    print(
        f"- Первая задача создана: "
        f"{created_at.min():%Y-%m-%d}"
    )

    print(
        f"- Последняя плановая дата: "
        f"{planned_end.max():%Y-%m-%d}"
    )

    print("\nСправочники:")

    for name, values in dataset.reference_lists.items():
        print(
            f"- {name}: "
            f"{len(values)} значений"
        )

    print("\nРезультаты импорта истории команд:")

    print(
        f"- Исторических записей: "
        f"{len(dataset.team_history)}"
    )

    print(
        f"- Команд в истории: "
        f"{dataset.team_history['team_id'].nunique()}"
    )

    average_velocity = (
        dataset.team_history
        .groupby("team_id")["velocity_achieved"]
        .mean()
        .sort_index()
    )

    print("\nРасчётная SP-ёмкость на спринт:")

    for team_id, velocity in average_velocity.items():
        sprint_capacity = (
            velocity
            * assumptions.focus_factor
        )

        print(
            f"- {team_id}: "
            f"средняя velocity {velocity:.1f}, "
            f"ёмкость {sprint_capacity:.1f} SP"
        )

    print("\nРезультаты импорта зависимостей:")

    print(
        f"- Записей зависимостей: "
        f"{len(dataset.dependencies)}"
    )

    print(
        f"- Неизвестных task_id: "
        f"{len(dataset.missing_dependency_task_ids)}"
    )

    print("\nНеизвестные задачи в зависимостях:")

    for task_id in dataset.missing_dependency_task_ids:
        print(f"- {task_id}")

    print(
        f"\nПовторяющихся пар зависимостей: "
        f"{len(dataset.repeated_dependency_pairs)}"
    )

    for (
        blocking_task_id,
        blocked_task_id,
    ), count in (
        dataset.repeated_dependency_pairs.items()
    ):
        print(
            f"- {blocking_task_id} → "
            f"{blocked_task_id}: "
            f"{count} записи"
        )

    print("\nНормализация ролей:")

    print(
        f"- Загружено алиасов: "
        f"{len(dataset.role_aliases)}"
    )

    for source_role, target_role in (
        dataset.role_aliases.items()
    ):
        print(
            f"- {source_role} → "
            f"{target_role}"
        )

    print(
        f"\nРолей из сметы без инженеров: "
        f"{len(dataset.roles_without_engineers)}"
    )

    for role in dataset.roles_without_engineers:
        print(f"- {role}")

    error_count = sum(
        issue.level == "ERROR"
        for issue in validation_issues
    )

    warning_count = sum(
        issue.level == "WARNING"
        for issue in validation_issues
    )

    print("\nОтчёт валидации:")

    print(
        f"- Ошибок: {error_count}"
    )

    print(
        f"- Предупреждений: "
        f"{warning_count}"
    )

    print(
        f"- Всего проблем: "
        f"{len(validation_issues)}"
    )

    print("\nНайденные проблемы:")

    for issue in validation_issues:
        print(
            f"- [{issue.level}] "
            f"{issue.code}: "
            f"{issue.message}"
        )

    print("\nРесурсы команд по Story Points:")

    for _, row in team_sp_capacity.iterrows():
        print(
            f"- {row['team_id']}: "
            f"{row['sprint_sp_capacity']:.1f} SP "
            f"на спринт, "
            f"{row['quarter_sp_capacity']:.1f} SP "
            f"на квартал"
        )
    
    print("\nДоступные часы команд на спринт:")

    team_hours = (
        role_capacity
        .groupby("team_id")["available_hours"]
        .sum()
        .sort_index()
    )

    for team_id, hours in team_hours.items():
        print(
            f"- {team_id}: "
            f"{hours:.0f} часов"
        )

    in_progress_task_ids = set(
        dataset.tasks.loc[
            dataset.tasks["status"] == "InProgress",
            "task_id",
        ]
    )

    in_progress_remaining = (
        remaining_role_hours[
            remaining_role_hours["task_id"].isin(
                in_progress_task_ids
            )
        ]
        .groupby("task_id")[
            [
                "estimated_hours",
                "spent_hours",
                "remaining_hours",
                "overspent_hours",
            ]
        ]
        .sum()
        .sort_index()
    )

    print("\nОстаток задач InProgress:")

    for task_id, row in (
        in_progress_remaining.iterrows()
    ):
        print(
            f"- {task_id}: "
            f"смета {row['estimated_hours']:.0f}, "
            f"затрачено {row['spent_hours']:.0f}, "
            f"осталось {row['remaining_hours']:.0f} часов"
        )

    print("\nЗвёздная карта:")

    clustered_engineers, unique_engineers, bus_factor_report = analyze_bus_factor(
        dataset.engineers
    )

    print(
        f"\nУникальных специалистов: "
        f"{len(unique_engineers)}"
    )

    for _, eng in unique_engineers.iterrows():
        print(
            f"- Инженер {eng['engineer_id']} "
            f"(Команда: {eng['team_id']}, Роль: {eng['role']})\n"
            f"  Уникальный стек: {eng['skills_declared']}"
        )

    print("\nКритические технологии (Bus Factor = 1):")
    
    # Ищем технологии, которыми владеет ровно 1 человек
    critical_skills = {
        skill: count 
        for skill, count in bus_factor_report.items() 
        if count == 1
    }

    if not critical_skills:
        print("- Критических технологий не найдено (Bus Factor > 1 для всего стека).")
    else:
        for skill, count in critical_skills.items():
            print(f"- {skill}: знает только {count} сотрудник в компании")

    print("\nДефицитные технологии (Bus Factor = 2):")
    warning_skills = {
        skill: count 
        for skill, count in bus_factor_report.items() 
        if count == 2
    }
    
    if not warning_skills:
         print("- Дефицитных технологий не найдено.")
    else:
        for skill, count in warning_skills.items():
            print(f"- {skill}: знают {count} сотрудника")

if __name__ == "__main__":
    main()
