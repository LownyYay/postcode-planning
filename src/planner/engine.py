import logging

from src.dataset import Dataset
from src.planner.config import InputConfig, PlannerConfig, SolveConfig
from src.planner.explain import explain_plan
from src.planner.greedy import plan_greedy
from src.planner.milp import SolverError, plan_milp
from src.planner.precheck import run_precheck
from src.planner.result import PlanResult
from src.planner.state import PlanningState, build_state
from src.planner.validate import validate_plan


logger = logging.getLogger(__name__)


def plan(
    state: PlanningState,
    solve: SolveConfig | None = None,
    previous_progress: dict[str, dict[int, float]] | None = None,
) -> PlanResult:
    """
    Строит план по правилам solve на готовом состоянии.

    Состояние не меняется, поэтому на одном состоянии можно запускать
    сценарии с разными правилами решения без пересборки.

    Жадный план строится всегда: это запасной вариант и нижняя граница
    для MILP по числу завершённых инициатив. Если решатель не дал
    доказанного оптимума, возвращается жадный план со статусом
    fallback_greedy и причиной в meta.fallback_reason.

    previous_progress — доли предыдущего плана: при пересчёте включает
    уровень стабильности, чтобы план не перетасовывался без нужды.

    Любой возвращаемый план проходит validate_plan;
    нарушение правил — исключение, а не переход на жадный план.
    """

    if solve is None:
        solve = SolveConfig()

    if not isinstance(solve, SolveConfig):
        raise TypeError(
            f"plan ожидает SolveConfig, получен {type(solve).__name__}"
        )

    precheck = run_precheck(state, solve)

    greedy = plan_greedy(state, solve, precheck)
    validate_plan(greedy, state, solve)

    if solve.engine == "greedy":
        explain_plan(greedy, state, solve, precheck)
        validate_plan(greedy, state, solve)
        return greedy

    try:
        result = plan_milp(
            state,
            solve,
            precheck,
            greedy_floor=greedy,
            previous_progress=previous_progress,
        )
    except SolverError as error:
        logger.warning(
            "Решатель не дал решения, используется жадный план: %s",
            error,
        )

        fallback = plan_greedy(
            state,
            solve,
            precheck,
            status="fallback_greedy",
            fallback_reason=str(error),
        )

        fallback.meta.warnings.append(
            f"Точный план не построен ({error}). Показан жадный план; "
            f"повторяемость результата не гарантируется."
        )

        explain_plan(fallback, state, solve, precheck)
        validate_plan(fallback, state, solve)

        return fallback

    explain_plan(result, state, solve, precheck)
    validate_plan(result, state, solve)

    return result


# Ключ — id датасета и InputConfig. Вместе с состоянием хранится сам
# датасет: пока он в кэше, его id не может достаться другому объекту.
_STATE_CACHE: dict[tuple[int, InputConfig], tuple[Dataset, PlanningState]] = {}


def scenario_state(
    dataset: Dataset,
    config: InputConfig,
) -> PlanningState:
    """
    Состояние для сценария; повторная сборка с тем же InputConfig не нужна.
    """

    key = (id(dataset), config)
    cached = _STATE_CACHE.get(key)

    if cached is None or cached[0] is not dataset:
        cached = (dataset, build_state(dataset, config))
        _STATE_CACHE[key] = cached

    return cached[1]


def run_scenario(
    dataset: Dataset,
    config: PlannerConfig,
) -> PlanResult:
    """
    Запускает сценарий целиком. Состояние кэшируется по InputConfig:
    сценарии, различающиеся только правилами решения, его не пересобирают.
    """

    return plan(scenario_state(dataset, config.input), config.solve)
