"""
Квартальный планировщик.

build_state(dataset, InputConfig) собирает вход, plan(state, SolveConfig)
строит план, run_scenario(dataset, PlannerConfig) делает оба шага.
Любой план проверяется validate_plan.
"""

from src.planner.config import (
    InputConfig,
    PlannerConfig,
    SolveConfig,
    load_planner_config,
)
from src.planner.engine import plan, run_scenario
from src.planner.result import PlanResult
from src.planner.state import PlanningState, build_state
from src.planner.validate import PlanValidationError, validate_plan

__all__ = [
    "InputConfig",
    "PlanResult",
    "PlanValidationError",
    "PlannerConfig",
    "PlanningState",
    "SolveConfig",
    "build_state",
    "load_planner_config",
    "plan",
    "run_scenario",
    "validate_plan",
]
