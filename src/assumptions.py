"""
Расчётные допущения для отчёта о ресурсах.

Числа берутся из раздела input файла config/planner.yaml — того же,
по которому строится план. Отдельного файла допущений нет, чтобы
отчёт и планировщик не могли разойтись.
"""

from dataclasses import dataclass
from pathlib import Path

from src.planner.config import PLANNER_CONFIG_PATH, load_planner_config


@dataclass(frozen=True)
class Assumptions:
    """
    Настройки расчёта ресурсов команд.
    """

    focus_factor: float = 0.8
    hours_per_sprint: float = 80
    sprint_count: int = 6


def load_assumptions(
    path: Path = PLANNER_CONFIG_PATH,
) -> Assumptions:
    """
    Загружает допущения из конфигурации планировщика.

    При отсутствии файла или отдельных ключей
    использует значения по умолчанию.
    """

    config = load_planner_config(path).input

    return Assumptions(
        focus_factor=float(config.focus_factor),
        hours_per_sprint=float(config.hours_per_sprint),
        sprint_count=int(config.quarter_sprints),
    )
