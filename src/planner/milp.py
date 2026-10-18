"""
Точный планировщик квартала на смешанном целочисленном программировании.

Ключевая идея — общая доля прогресса: в каждом спринте решатель выбирает
долю задачи p от 0 до 1. Эта доля одновременно списывает очки задачи
из ёмкости команды и часы каждой роли из фонда роли. Очки и часы друг
в друга не конвертируются: это два независимых лимита.

Лестница целей решается проходами, значения пройденных уровней
фиксируются ограничениями:

L12 — максимум завершённых инициатив, при равенстве — максимум суммы rung.
      Оба уровня целочисленные, поэтому решаются одним точным проходом
      с весом инициативы W + rung, где W = сумма всех rung + 1: одна лишняя
      инициатива всегда перевешивает любую разницу в rung.
L3  — минимум отклонений от предыдущего плана (только при пересчёте):
      план не должен перетасовываться без необходимости.
L4  — минимум заимствованных часов (только при role_pool=team_borrow).
L5  — максимум задач, завершённых в квартале, при равенстве — завершить
      их как можно раньше. Тоже один проход со взвешенной суммой: вес
      задачи равен числу задач на число спринтов плюс один.
L6  — прогноз: задачи квартала зафиксированы, отложенные задачи
      завершаются как можно раньше на горизонте.

Проходы L12–L5 зависят только от решений в квартале: отложенная задача
в квартале не активна (правило 4), и любое решение квартала продолжается
на спринты после него. Поэтому они решаются на модели из спринтов квартала
с фиксацией значений уровней, а на полный горизонт переносится готовое
расписание квартала — только для прогноза L6. Это не меняет оптимум
ни одного уровня и в несколько раз ускоряет решение.
"""

import time
from dataclasses import asdict, dataclass, field

import pulp

from src.planner.assemble import assemble_plan, summarize_objective
from src.planner.config import SolveConfig
from src.planner.precheck import PrecheckResult
from src.planner.result import Meta, PlanResult
from src.planner.rules import is_limited, pool_scope
from src.planner.state import EPS, PlanningState


PASSES = ("L12", "L3", "L4", "L5", "L6")


class SolverError(RuntimeError):
    """
    Решатель не дал пригодного решения: ошибка, таймаут
    или недопустимая модель. Планировщик переходит на жадный план.
    """


@dataclass
class Level:
    """
    Один проход лестницы целей.
    """

    name: str
    expression: pulp.LpAffineExpression
    sense: int
    integral: bool


@dataclass
class MilpModel:
    state: PlanningState
    solve: SolveConfig
    precheck: PrecheckResult
    problem: pulp.LpProblem
    task_ids: list[str]
    sprints: list[int]
    quarter: int
    started: dict[tuple[str, int], pulp.LpVariable] = field(default_factory=dict)
    finished: dict[tuple[str, int], pulp.LpVariable] = field(default_factory=dict)
    progress: dict[tuple[str, int], pulp.LpVariable] = field(default_factory=dict)
    init_done: dict[str, pulp.LpVariable] = field(default_factory=dict)
    borrowed: dict[tuple[str, str, int], pulp.LpVariable] = field(default_factory=dict)

    def binary(self, name: str) -> pulp.LpVariable:
        return self.problem.add_variable(
            name,
            lowBound=0,
            upBound=1,
            cat="Binary",
        )

    def finished_by(self, task_id: str, sprint: int):
        """
        finished[j, t]; до первого спринта горизонта задача не завершена.
        """

        if sprint < self.sprints[0]:
            return 0

        return self.finished[(task_id, sprint)]

    def active(self, task_id: str, sprint: int):
        """
        active[j, t] = started[j, t] − finished[j, t − 1].
        """

        return (
            self.started[(task_id, sprint)]
            - self.finished_by(task_id, sprint - 1)
        )


def build_model(
    state: PlanningState,
    solve: SolveConfig,
    precheck: PrecheckResult,
    last_sprint: int | None = None,
) -> MilpModel:
    """
    Переменные и ограничения. Порядок обхода задач, команд и ролей
    отсортирован, чтобы модель не зависела от порядка строк в файле.

    last_sprint позволяет обрезать горизонт — например, до квартала,
    чтобы проверить, что уровни 1–2 от спринтов после квартала не зависят.
    """

    last = last_sprint or state.horizon

    model = MilpModel(
        state=state,
        solve=solve,
        precheck=precheck,
        problem=pulp.LpProblem("quarter_plan", pulp.LpMaximize),
        task_ids=sorted(
            task_id
            for task_id in state.tasks
            if task_id not in precheck.cancelled
        ),
        sprints=list(range(state.current_sprint + 1, last + 1)),
        quarter=state.quarter,
    )

    for index, task_id in enumerate(model.task_ids):
        for sprint in model.sprints:
            key = (task_id, sprint)

            model.started[key] = model.binary(f"started_{index}_{sprint}")
            model.finished[key] = model.binary(f"finished_{index}_{sprint}")
            model.progress[key] = model.problem.add_variable(
                f"p_{index}_{sprint}",
                lowBound=0,
                upBound=1,
            )

    for index, initiative_id in enumerate(sorted(state.initiatives)):
        model.init_done[initiative_id] = model.binary(f"init_done_{index}")

    add_ladders(model)
    add_active_work(model)
    add_completion(model)
    add_quarter_rule(model)
    add_team_sp_limit(model)
    add_role_hours_limit(model)
    add_total_hours_limit(model)
    add_dependencies(model)
    add_in_progress(model)
    add_initiatives(model)

    return model


def add_ladders(model: MilpModel) -> None:
    """
    Правило 1. Лесенки started и finished не убывают;
    завершена может быть только начатая задача.
    """

    for task_id in model.task_ids:
        for sprint in model.sprints:
            key = (task_id, sprint)
            problem = model.problem

            problem += model.finished[key] <= model.started[key]

            if sprint > model.sprints[0]:
                previous = (task_id, sprint - 1)
                problem += model.started[key] >= model.started[previous]
                problem += model.finished[key] >= model.finished[previous]


def add_active_work(model: MilpModel) -> None:
    """
    Правила 2 и 10. Работа идёт только в активных спринтах,
    и в каждом активном спринте — не меньше min_progress.
    Убирает «висящие» задачи и доли по 4 %.

    Отложенная задача в квартале не активна по правилу 4,
    поэтому минимальная доля её там не касается.
    """

    min_progress = model.solve.min_progress

    for task_id in model.task_ids:
        for sprint in model.sprints:
            share = model.progress[(task_id, sprint)]
            active = model.active(task_id, sprint)

            model.problem += share <= active

            if min_progress > 0:
                model.problem += share >= min_progress * active


def add_completion(model: MilpModel) -> None:
    """
    Правило 3. К моменту завершения сделано 100 %,
    и всего не больше 100 %. Верхняя граница часов задачи
    следует отсюда же: часы роли = остаток роли × доля.
    """

    last = model.sprints[-1]

    for task_id in model.task_ids:
        cumulative = 0

        for sprint in model.sprints:
            cumulative += model.progress[(task_id, sprint)]
            model.problem += cumulative >= model.finished[(task_id, sprint)]

        model.problem += cumulative == model.finished[(task_id, last)]


def add_quarter_rule(model: MilpModel) -> None:
    """
    Правило 4. Не начинаем в квартале то, что в нём не закончим:
    started[j, Q] = finished[j, Q].
    """

    if model.quarter not in model.sprints:
        return

    for task_id in model.task_ids:
        model.problem += (
            model.started[(task_id, model.quarter)]
            == model.finished[(task_id, model.quarter)]
        )


def add_team_sp_limit(model: MilpModel) -> None:
    """
    Правило 5. Очки команды за спринт не больше её ёмкости.
    """

    state = model.state

    for team in state.teams:
        team_tasks = [
            task_id
            for task_id in model.task_ids
            if state.tasks[task_id].team == team
            and state.tasks[task_id].sp_effective > EPS
        ]

        if not team_tasks:
            continue

        for sprint in model.sprints:
            model.problem += (
                pulp.lpSum(
                    state.tasks[task_id].sp_effective
                    * model.progress[(task_id, sprint)]
                    for task_id in team_tasks
                )
                <= state.team_capacity[team]
            )


def add_role_hours_limit(model: MilpModel) -> None:
    """
    Правила 6 и 11. Часы роли за спринт не больше фонда
    в области пула role_pool:
    team — только своя команда;
    global — вся организация;
    team_borrow — лимит организации плюс учёт часов,
    взятых сверх фонда своей команды.
    """

    state = model.state
    pool = model.solve.role_pool

    usage: dict[tuple[str, str], list[str]] = {}

    for task_id in model.task_ids:
        task = state.tasks[task_id]

        for role in task.remaining_hours:
            if is_limited(state, model.solve, role):
                usage.setdefault((task.team, role), []).append(task_id)

    def hours(task_ids, role, sprint):
        return pulp.lpSum(
            state.tasks[task_id].remaining_hours[role]
            * model.progress[(task_id, sprint)]
            for task_id in task_ids
        )

    for sprint in model.sprints:
        if pool == "team":
            for (team, role), task_ids in sorted(usage.items()):
                model.problem += (
                    hours(task_ids, role, sprint)
                    <= state.team_fund(team, role, sprint)
                )
            continue

        for role in sorted({role for (_, role) in usage}):
            role_tasks = [
                task_id
                for (_, usage_role), task_ids in sorted(usage.items())
                if usage_role == role
                for task_id in task_ids
            ]

            model.problem += (
                hours(role_tasks, role, sprint)
                <= state.global_fund(role, sprint)
            )

        if pool != "team_borrow":
            continue

        for (team, role), task_ids in sorted(usage.items()):
            variable = model.problem.add_variable(
                f"borrowed_{len(model.borrowed)}",
                lowBound=0,
            )

            model.borrowed[(team, role, sprint)] = variable
            model.problem += (
                variable
                >= hours(task_ids, role, sprint)
                - state.team_fund(team, role, sprint)
            )


def add_total_hours_limit(model: MilpModel) -> None:
    """
    Правило 12. Часы по всем ролям сразу за спринт не больше потолка,
    заданного в state.hours_limit.

    Фонд считается по паре «команда, роль», поэтому человек с двумя
    ролями отдаёт свои часы в оба фонда. Этот потолок не даёт потратить
    его дважды: он равен сумме часов различных инженеров области.
    Без записей в hours_limit ограничение не строится.
    """

    state = model.state

    if not state.hours_limit:
        return

    usage: dict[str, list[tuple[str, float]]] = {}

    for task_id in model.task_ids:
        task = state.tasks[task_id]
        scope = pool_scope(model.solve, task.team)

        for role, hours in task.remaining_hours.items():
            if is_limited(state, model.solve, role):
                usage.setdefault(scope, []).append((task_id, hours))

    for scope, rows in sorted(usage.items()):
        for sprint in model.sprints:
            limit = state.sprint_hours_limit(scope, sprint)

            if limit is None:
                continue

            model.problem += (
                pulp.lpSum(
                    hours * model.progress[(task_id, sprint)]
                    for task_id, hours in rows
                )
                <= limit
            )


def add_dependencies(model: MilpModel) -> None:
    """
    Правило 7. started[b, t] ≤ finished[a, t − 1 − gap].

    gap = gap_planned, если блокирующая только запланирована,
    gap = gap_done, если она уже Done. Все команды в одной модели,
    поэтому межкомандные связи учитываются сами собой.
    Дополнительно задача не стартует раньше самого раннего спринта
    из предпроверки — это следствие тех же правил.
    """

    state = model.state
    solve = model.solve
    in_model = set(model.task_ids)

    for task_id in model.task_ids:
        earliest = model.precheck.earliest_start.get(task_id, model.sprints[0])

        for sprint in model.sprints:
            if sprint < earliest:
                model.problem += model.started[(task_id, sprint)] == 0

    for blocking, blocked in state.dependencies:
        if blocked not in in_model:
            continue

        if blocking in state.done_tasks:
            ready_from = state.done_tasks[blocking] + 1 + solve.gap_done

            for sprint in model.sprints:
                if sprint < ready_from:
                    model.problem += model.started[(blocked, sprint)] == 0
            continue

        if blocking not in in_model:
            # Отменённая блокирующая: предпроверка уже отменила блокируемую.
            continue

        for sprint in model.sprints:
            model.problem += (
                model.started[(blocked, sprint)]
                <= model.finished_by(blocking, sprint - 1 - solve.gap_planned)
            )


def add_in_progress(model: MilpModel) -> None:
    """
    Правило 8. Задача в работе, если попадает в квартал, активна
    с первого доступного спринта. Обязательность выполнения задаёт
    inprogress_must_finish. Задача в работе без остатка часов
    закрывается в первом доступном спринте.
    """

    state = model.state
    quarter = model.quarter

    for task_id in model.task_ids:
        task = state.tasks[task_id]

        if task.status != "InProgress":
            continue

        open_blockers = [
            blocking
            for blocking in state.predecessors(task_id)
            if blocking not in state.done_tasks
        ]

        if open_blockers:
            # Первый доступный спринт зависит от плана блокирующих задач.
            continue

        first = model.precheck.earliest_start.get(task_id, model.sprints[0])

        if first not in model.sprints:
            continue

        if task.zero_remaining:
            model.problem += model.progress[(task_id, first)] == 1
            continue

        if quarter in model.sprints and first <= quarter:
            model.problem += (
                model.started[(task_id, first)]
                >= model.finished[(task_id, quarter)]
            )

            if model.solve.inprogress_must_finish:
                model.problem += model.finished[(task_id, quarter)] == 1


def add_initiatives(model: MilpModel) -> None:
    """
    Правило 9. Инициатива завершена, только если завершены в квартале
    все её открытые задачи. Инициатива с отменённой задачей
    завершённой быть не может.
    """

    state = model.state
    in_model = set(model.task_ids)

    for initiative_id, initiative in state.initiatives.items():
        done = model.init_done[initiative_id]

        if any(task_id not in in_model for task_id in initiative.open_task_ids):
            model.problem += done == 0
            continue

        if model.quarter not in model.sprints:
            model.problem += done == 0
            continue

        for task_id in initiative.open_task_ids:
            model.problem += done <= model.finished[(task_id, model.quarter)]


def integer_rungs(model: MilpModel) -> tuple[dict[str, int], int]:
    """
    Rung инициатив как целые числа и масштаб перевода.

    При initiative_rung=mean rung дробный: он умножается на 100
    и округляется, иначе точный слитый проход невозможен.
    """

    config = model.state.input_config
    scale = 1 if config.initiative_rung == "max" else 100

    rungs = {
        initiative_id: round(initiative.rung * scale)
        for initiative_id, initiative in model.state.initiatives.items()
    }

    return rungs, scale


def level_initiatives(model: MilpModel) -> Level:
    """
    L12: число завершённых инициатив, при равенстве — сумма rung.
    """

    rungs, _ = integer_rungs(model)

    if model.solve.objective_mode == "rung_weighted":
        weights = rungs
    else:
        big = sum(rungs.values()) + 1
        weights = {
            initiative_id: big + rung
            for initiative_id, rung in rungs.items()
        }

    return Level(
        name="L12",
        expression=pulp.lpSum(
            weights[initiative_id] * variable
            for initiative_id, variable in model.init_done.items()
        ),
        sense=pulp.LpMaximize,
        integral=True,
    )


def decode_initiatives(model: MilpModel, value: float) -> dict[str, float]:
    """
    Разбирает значение L12 на число инициатив и сумму rung.
    """

    rungs, scale = integer_rungs(model)
    total = round(value)

    if model.solve.objective_mode == "rung_weighted":
        return {"L12_rung": total / scale}

    big = sum(rungs.values()) + 1
    count = total // big

    return {
        "L12_initiatives": float(count),
        "L12_rung": (total - count * big) / scale,
    }


def level_stability(
    model: MilpModel,
    previous: dict[str, dict[int, float]],
) -> Level:
    """
    L3: минимум отклонений от предыдущего плана.

    Доли предыдущего плана пересчитаны в доли нового остатка,
    поэтому задача, которая идёт по плану, отклонений не даёт.
    """

    deviations = []

    for task_id in model.task_ids:
        planned = previous.get(task_id, {})

        for sprint in model.sprints:
            share = model.progress[(task_id, sprint)]
            was = planned.get(sprint, 0.0)

            deviation = model.problem.add_variable(
                f"deviation_{len(deviations)}",
                lowBound=0,
            )

            model.problem += deviation >= share - was
            model.problem += deviation >= was - share
            deviations.append(deviation)

    return Level(
        name="L3",
        expression=pulp.lpSum(deviations),
        sense=pulp.LpMinimize,
        integral=False,
    )


def level_borrowing(model: MilpModel) -> Level:
    """
    L4: минимум часов, взятых сверх фонда своей команды.
    """

    # Заимствования считаются только в квартале: после него план —
    # прогноз, и штраф за займ вытеснял бы отложенные задачи из прогноза.
    return Level(
        name="L4",
        expression=pulp.lpSum(
            variable
            for (_, _, sprint), variable in model.borrowed.items()
            if sprint <= model.quarter
        ),
        sense=pulp.LpMinimize,
        integral=False,
    )


def early_weight(model: MilpModel) -> int:
    """
    Вес задачи квартала против срока её завершения.

    Сумма finished[j, t] по всем задачам и спринтам не может превысить
    число задач, умноженное на число спринтов. Вес на единицу больше,
    поэтому одна лишняя задача в квартале всегда перевешивает любой
    выигрыш в сроках.
    """

    return len(model.task_ids) * len(model.sprints) + 1


def level_tasks(model: MilpModel) -> Level:
    """
    L5: максимум задач, завершённых в квартале, при равенстве —
    завершить их как можно раньше.

    Оба уровня целочисленные, поэтому решаются одним точным проходом
    со взвешенной суммой — тем же приёмом, что L12. Отдельным проходом
    разрыв ничьей стоил бы ещё семь секунд на официальном датасете.

    Без второго слагаемого решатель волен растянуть работу до конца
    квартала: план выглядит так, будто всё делается в последний момент,
    а у зависимых работ пропадает запас.
    """

    quarter_tasks = pulp.lpSum(
        model.finished[(task_id, model.quarter)]
        for task_id in model.task_ids
    )

    if not model.solve.early_finish:
        return Level(
            name="L5",
            expression=quarter_tasks,
            sense=pulp.LpMaximize,
            integral=True,
        )

    weight = early_weight(model)

    return Level(
        name="L5",
        expression=(
            weight * quarter_tasks
            + pulp.lpSum(
                model.finished[(task_id, sprint)]
                for task_id in model.task_ids
                for sprint in model.sprints
            )
        ),
        sense=pulp.LpMaximize,
        integral=True,
    )


def level_forecast(model: MilpModel, postponed: list[str]) -> Level:
    """
    L6: отложенные задачи завершаются как можно раньше на горизонте.
    Нужен только для прогнозных сроков красного алерта.
    """

    return Level(
        name="L6",
        expression=pulp.lpSum(
            model.finished[(task_id, sprint)]
            for task_id in postponed
            for sprint in model.sprints
        ),
        sense=pulp.LpMaximize,
        integral=True,
    )


def make_solver(solve: SolveConfig, time_limit: float, exact: bool):
    """
    Решатель с фиксированным зерном и одним потоком:
    одинаковый вход даёт одинаковый результат.
    exact — нулевой допуск по разрыву для целочисленных проходов.
    """

    time_limit = max(time_limit, 1.0)
    gap = 0.0 if exact else None

    if solve.solver == "highs":
        return pulp.HiGHS(
            msg=False,
            timeLimit=time_limit,
            threads=1,
            gapRel=gap,
            random_seed=solve.random_seed,
        )

    return pulp.PULP_CBC_CMD(
        msg=False,
        timeLimit=time_limit,
        threads=1,
        gapRel=gap,
        options=[f"randomSeed {solve.random_seed}", f"randomCbcSeed {solve.random_seed}"],
    )


def solve_level(model: MilpModel, level: Level, deadline: float) -> float:
    """
    Один проход. Любой исход, кроме доказанного оптимума, — ошибка:
    при таймауте повторяемость плана не гарантируется.
    """

    remaining = deadline - time.perf_counter()

    if remaining <= 0:
        raise SolverError("исчерпан страховочный лимит времени решателя")

    problem = model.problem
    problem.sense = level.sense
    problem.setObjective(level.expression)

    try:
        problem.solve(make_solver(model.solve, remaining, level.integral))
    except Exception as error:
        raise SolverError(f"ошибка решателя: {error}") from error

    status = pulp.LpStatus.get(problem.status, str(problem.status))

    if status == "Infeasible":
        raise SolverError(f"модель недопустима на проходе {level.name}")

    if problem.sol_status != pulp.LpSolutionOptimal:
        raise SolverError(
            f"проход {level.name}: нет доказанного оптимума "
            f"(статус {status}), вероятно, сработал лимит времени"
        )

    return float(pulp.value(level.expression) or 0.0)


def fix_level(model: MilpModel, level: Level, value: float) -> None:
    """
    Фиксирует достигнутое значение уровня.

    Целый уровень — с допуском 0.5, непрерывный — с относительным
    допуском 1e-6: строгое равенство на числовой погрешности
    даёт ложную недопустимость.
    """

    if level.integral:
        if level.sense == pulp.LpMaximize:
            model.problem += level.expression >= value - 0.5, f"fix_{level.name}"
        else:
            model.problem += level.expression <= value + 0.5, f"fix_{level.name}"
        return

    tolerance = 1e-6 * max(1.0, abs(value))

    if level.sense == pulp.LpMaximize:
        model.problem += level.expression >= value - tolerance, f"fix_{level.name}"
    else:
        model.problem += level.expression <= value + tolerance, f"fix_{level.name}"


def quarter_tasks(model: MilpModel) -> list[str]:
    """
    Задачи, завершённые в квартале по текущему решению.
    """

    return [
        task_id
        for task_id in model.task_ids
        if (model.finished[(task_id, model.quarter)].value() or 0) > 0.5
    ]


def extract_progress(model: MilpModel) -> dict[str, dict[int, float]]:
    progress: dict[str, dict[int, float]] = {}

    for task_id in model.task_ids:
        shares = {}

        for sprint in model.sprints:
            value = model.progress[(task_id, sprint)].value() or 0.0

            if value > 1e-7:
                shares[sprint] = min(1.0, round(value, 9))

        if shares:
            progress[task_id] = shares

    return progress


def transfer_quarter(
    source: MilpModel,
    target: MilpModel,
    in_quarter: list[str],
) -> None:
    """
    Переносит решение квартала в модель полного горизонта.

    Задачи квартала закрепляются целиком: после квартала они
    завершены и работы не получают. Отложенные задачи в квартале
    не начаты. Итоги по инициативам переносятся как есть.
    """

    quarter_set = set(in_quarter)

    for task_id in target.task_ids:
        for sprint in target.sprints:
            key = (task_id, sprint)

            if sprint in source.sprints:
                values = (
                    round(source.started[key].value() or 0),
                    round(source.finished[key].value() or 0),
                    source.progress[key].value() or 0.0,
                )
            elif task_id in quarter_set:
                values = (1, 1, 0.0)
            else:
                continue

            started, finished, share = values

            for variable, value in (
                (target.started[key], started),
                (target.finished[key], finished),
                (target.progress[key], 0.0 if share < 1e-9 else min(1.0, share)),
            ):
                variable.setInitialValue(value)
                variable.fixValue()

    for initiative_id, variable in target.init_done.items():
        value = source.init_done[initiative_id].value() or 0
        variable.setInitialValue(round(value))
        variable.fixValue()


def plan_milp(
    state: PlanningState,
    solve: SolveConfig,
    precheck: PrecheckResult,
    greedy_floor: PlanResult | None = None,
    stop_after: str | None = None,
    previous_progress: dict[str, dict[int, float]] | None = None,
) -> PlanResult:
    """
    Решает модель по лестнице целей и собирает PlanResult.

    greedy_floor — жадный план тех же параметров: число завершённых
    инициатив не может быть меньше, чем у него.
    previous_progress — доли предыдущего плана для уровня L3: включается
    только при пересчёте.
    stop_after — остановиться после указанного прохода (для проверок).
    """

    started = time.perf_counter()
    deadline = started + solve.safety_time_limit

    values: dict[str, float] = {}
    pass_seconds: dict[str, float] = {}

    def run(model: MilpModel, level: Level, fix: bool = True) -> None:
        begin = time.perf_counter()
        values[level.name] = solve_level(model, level, deadline)
        pass_seconds[level.name] = time.perf_counter() - begin

        if fix:
            fix_level(model, level, values[level.name])

    if state.current_sprint >= state.quarter:
        # Квартал закончился: решать в нём нечего, остаётся только прогноз.
        return _forecast_only(state, solve, precheck, started, deadline)

    # Фаза 1: уровни L12–L5 на спринтах квартала.
    quarter_model = build_model(state, solve, precheck, last_sprint=state.quarter)

    if greedy_floor is not None:
        quarter_model.problem += (
            pulp.lpSum(quarter_model.init_done.values())
            >= greedy_floor.initiatives_done
        ), "floor_greedy"

    result_model = quarter_model

    for name in PASSES[:-1]:
        if name == "L12":
            run(quarter_model, level_initiatives(quarter_model))
        elif name == "L3" and previous_progress:
            run(quarter_model, level_stability(quarter_model, previous_progress))
        elif name == "L4" and solve.role_pool == "team_borrow":
            run(quarter_model, level_borrowing(quarter_model))
        elif name == "L5":
            run(quarter_model, level_tasks(quarter_model))

        if name == stop_after:
            break
    else:
        # Фаза 2: прогноз отложенных задач на полном горизонте.
        in_quarter = quarter_tasks(quarter_model)

        full_model = build_model(state, solve, precheck)
        transfer_quarter(quarter_model, full_model, in_quarter)

        postponed = sorted(set(full_model.task_ids) - set(in_quarter))

        if postponed:
            run(full_model, level_forecast(full_model, postponed), fix=False)
        else:
            values["L6"] = 0.0

        result_model = full_model

    meta = Meta(
        engine="milp",
        solver=solve.solver,
        status="optimal",
        elapsed_seconds=0.0,
        current_sprint=state.current_sprint,
        input_config=asdict(state.input_config),
        solve_config=asdict(solve),
        warnings=list(state.warnings),
    )

    plan = assemble_plan(
        state,
        solve,
        extract_progress(result_model),
        precheck.cancelled,
        meta,
    )

    objective = summarize_objective(state, plan)

    if "L12" in values:
        objective.update(decode_initiatives(quarter_model, values["L12"]))

    for name, value in values.items():
        if name == "L12":
            continue

        if name == "L5" and solve.early_finish:
            # Слитый уровень: старшая часть — задачи, младшая — сроки.
            weight = early_weight(quarter_model)
            objective["L5_tasks"] = int(round(value)) // weight
            objective["L5_early"] = int(round(value)) % weight
            continue

        objective[name] = value

    objective.update({f"seconds_{name}": seconds for name, seconds in pass_seconds.items()})

    plan.meta.objective = objective
    plan.meta.elapsed_seconds = time.perf_counter() - started

    return plan


def _forecast_only(
    state: PlanningState,
    solve: SolveConfig,
    precheck: PrecheckResult,
    started: float,
    deadline: float,
) -> PlanResult:
    """
    Квартал уже закончился: все открытые задачи отложены,
    считается только прогноз их завершения на горизонте.
    """

    model = build_model(state, solve, precheck)

    values = {}

    if model.task_ids:
        values["L6"] = solve_level(
            model,
            level_forecast(model, model.task_ids),
            deadline,
        )

    meta = Meta(
        engine="milp",
        solver=solve.solver,
        status="optimal",
        elapsed_seconds=0.0,
        current_sprint=state.current_sprint,
        input_config=asdict(state.input_config),
        solve_config=asdict(solve),
        warnings=list(state.warnings),
    )

    plan = assemble_plan(
        state,
        solve,
        extract_progress(model),
        precheck.cancelled,
        meta,
    )

    plan.meta.objective = {**summarize_objective(state, plan), **values}
    plan.meta.elapsed_seconds = time.perf_counter() - started

    return plan
