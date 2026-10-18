import pandas as pd


def calculate_team_sp_capacity(
    team_history: pd.DataFrame,
    focus_factor: float = 0.8,
    sprint_count: int = 6,
) -> pd.DataFrame:
    """
    Рассчитывает SP-ёмкость команд.

    Ёмкость спринта:
    средняя фактическая velocity × 0.8
    """

    result = (
        team_history
        .groupby("team_id", as_index=False)
        .agg(
            average_velocity=(
                "velocity_achieved",
                "mean",
            )
        )
    )

    result["sprint_sp_capacity"] = (
        result["average_velocity"]
        * focus_factor
    )

    result["quarter_sp_capacity"] = (
        result["sprint_sp_capacity"]
        * sprint_count
    )

    return result.sort_values(
        "team_id"
    ).reset_index(drop=True)


def calculate_role_capacity(
    engineers: pd.DataFrame,
    hours_per_sprint: float = 80,
) -> pd.DataFrame:
    """
    Рассчитывает доступные часы каждой роли
    в каждой команде за один спринт.

    1.0 ставки = 80 часов.
    0.5 ставки = 40 часов.
    """

    engineer_capacity = engineers.copy()

    engineer_capacity["available_hours"] = (
        engineer_capacity["capacity_rate"]
        * hours_per_sprint
    )

    result = (
        engineer_capacity
        .groupby(
            [
                "team_id",
                "role",
            ],
            as_index=False,
        )
        .agg(
            engineer_count=(
                "engineer_id",
                "nunique",
            ),
            available_hours=(
                "available_hours",
                "sum",
            ),
        )
    )

    return result.sort_values(
        [
            "team_id",
            "role",
        ]
    ).reset_index(drop=True)


def calculate_remaining_role_hours(
    estimates: pd.DataFrame,
    spent_hours: pd.DataFrame,
) -> pd.DataFrame:
    """
    Рассчитывает остаток работы отдельно
    по каждой роли каждой задачи.
    """

    estimated = (
        estimates
        .groupby(
            [
                "task_id",
                "role",
            ],
            as_index=False,
        )
        .agg(
            estimated_hours=(
                "estimated_hours",
                "sum",
            )
        )
    )

    spent = (
        spent_hours
        .groupby(
            [
                "task_id",
                "role",
            ],
            as_index=False,
        )
        .agg(
            spent_hours=(
                "spent_hours",
                "sum",
            )
        )
    )

    result = estimated.merge(
        spent,
        on=[
            "task_id",
            "role",
        ],
        how="outer",
    )

    result["estimated_hours"] = (
        result["estimated_hours"]
        .fillna(0)
    )

    result["spent_hours"] = (
        result["spent_hours"]
        .fillna(0)
    )

    result["remaining_hours"] = (
        result["estimated_hours"]
        - result["spent_hours"]
    ).clip(lower=0)

    result["overspent_hours"] = (
        result["spent_hours"]
        - result["estimated_hours"]
    ).clip(lower=0)

    return result.sort_values(
        [
            "task_id",
            "role",
        ]
    ).reset_index(drop=True)