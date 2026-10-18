from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.importer import (
    extract_dependencies,
    extract_engineers,
    extract_estimates,
    extract_reference_lists,
    extract_spent_hours,
    extract_tasks,
    extract_team_history,
    load_raw_excel,
)
from src.normalizer import (
    load_role_aliases,
    normalize_role_column,
    unify_role_columns,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_DIR / "data" / "dataset.xlsx"
SHEET_NAME = "Датасет(обновленный)"


@dataclass
class Dataset:
    """
    Все таблицы и справочники загруженного датасета.
    """

    raw: pd.DataFrame
    tasks: pd.DataFrame
    estimates: pd.DataFrame
    spent_hours: pd.DataFrame
    engineers: pd.DataFrame
    team_history: pd.DataFrame
    dependencies: pd.DataFrame
    role_aliases: dict[str, str]
    reference_lists: dict[str, list[str]]

    @property
    def task_ids(self) -> set[str]:
        """
        Возвращает идентификаторы задач из Tasks.
        """

        return set(self.tasks["task_id"])

    @property
    def estimate_task_ids(self) -> set[str]:
        """
        Возвращает идентификаторы задач из сметы.
        """

        return set(self.estimates["task_id"])

    @property
    def estimates_without_tasks(self) -> list[str]:
        """
        Возвращает задачи из сметы, которых нет в Tasks.
        """

        return sorted(
            self.estimate_task_ids
            - self.task_ids
        )

    @property
    def dependency_task_ids(self) -> set[str]:
        """
        Возвращает все идентификаторы из зависимостей.
        """

        return (
            set(
                self.dependencies[
                    "blocking_task_id"
                ]
            )
            | set(
                self.dependencies[
                    "blocked_task_id"
                ]
            )
        )

    @property
    def missing_dependency_task_ids(
        self,
    ) -> list[str]:
        """
        Возвращает неизвестные задачи из зависимостей.
        """

        return sorted(
            self.dependency_task_ids
            - self.task_ids
        )

    @property
    def repeated_dependency_pairs(
        self,
    ) -> pd.Series:
        """
        Возвращает повторяющиеся пары зависимостей.
        """

        dependency_pair_counts = (
            self.dependencies
            .groupby(
                [
                    "blocking_task_id",
                    "blocked_task_id",
                ]
            )
            .size()
        )

        return dependency_pair_counts[
            dependency_pair_counts > 1
        ]

    @property
    def unique_skills(self) -> set[str]:
        """
        Возвращает все заявленные технологии.
        """

        return {
            skill
            for skills in self.engineers["skills"]
            for skill in skills
        }

    @property
    def roles_without_engineers(self) -> list[str]:
        """
        Возвращает роли из сметы без инженеров.
        """

        estimate_roles = set(
            self.estimates["role"]
        )

        engineer_roles = set(
            self.engineers["role"]
        )

        return sorted(
            estimate_roles
            - engineer_roles
        )


def load_dataset(
    path: Path = DATASET_PATH,
    sheet_name: str = SHEET_NAME,
) -> Dataset:
    """
    Загружает, извлекает и нормализует таблицы датасета.
    """

    raw = load_raw_excel(
        path=path,
        sheet_name=sheet_name,
    )

    return dataset_from_raw(raw)


def dataset_from_raw(raw: pd.DataFrame) -> Dataset:
    """
    Извлекает и нормализует таблицы из уже прочитанного листа.

    Нужна тем, кто получил лист не из файла на диске,
    например сервер, читающий Excel из памяти.
    """

    tasks = extract_tasks(raw)
    estimates = extract_estimates(raw)
    spent_hours = extract_spent_hours(raw)
    engineers = extract_engineers(raw)
    team_history = extract_team_history(raw)
    dependencies = extract_dependencies(raw)
    reference_lists = extract_reference_lists(raw)

    role_aliases = load_role_aliases()

    estimates = normalize_role_column(
        estimates,
        role_aliases,
    )

    spent_hours = normalize_role_column(
        spent_hours,
        role_aliases,
    )

    engineers = normalize_role_column(
        engineers,
        role_aliases,
    )

    # Сверх словаря: написания, различающиеся только регистром,
    # пробелами, «_», дефисами и точками, — одна роль.
    estimates, spent_hours, engineers = unify_role_columns(
        [estimates, spent_hours, engineers],
        role_aliases,
    )

    return Dataset(
        raw=raw,
        tasks=tasks,
        estimates=estimates,
        spent_hours=spent_hours,
        engineers=engineers,
        team_history=team_history,
        dependencies=dependencies,
        role_aliases=role_aliases,
        reference_lists=reference_lists,
    )
