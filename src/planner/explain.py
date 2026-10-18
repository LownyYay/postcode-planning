"""
Причины решений и контрфакты.

Причины записываются в момент решения, из той же модели и тех же чисел,
что и план. Для каждой отложенной задачи и каждой незавершённой инициативы
запускается контрфакт: повторное решение с принудительным включением.
Контрфакты решаются только по уровням цели 1–2 и с отдельным бюджетом:
не больше counterfactual_limit решений за counterfactual_time_limit секунд.
"""

import time
from collections import defaultdict

from src.planner.config import SolveConfig
from src.planner.milp import (
    MilpModel,
    SolverError,
    build_model,
    decode_initiatives,
    level_initiatives,
    solve_level,
)
from src.planner.precheck import PrecheckResult
from src.planner.result import PlanResult
from src.planner.rules import blocker_ready_from, is_limited, window_pool_fund
from src.planner.state import EPS, PlanningState
from src.text import first_sentences, sprints as sprints_text


def _format_initiatives(state: PlanningState, initiative_ids) -> str:
    items = [
        f"{initiative_id} (rung {state.initiatives[initiative_id].rung:g})"
        for initiative_id in sorted(
            initiative_ids,
            key=lambda item: (-state.initiatives[item].rung, item),
        )
    ]

    return ", ".join(items)


def team_load(state: PlanningState, cancelled: set[str]) -> dict[str, tuple[float, float]]:
    """
    Спрос очков команды против ёмкости за квартал.
    """

    demand: dict[str, float] = defaultdict(float)

    for task_id, task in state.tasks.items():
        if task_id not in cancelled:
            demand[task.team] += task.sp_effective

    quarter = state.quarter - state.current_sprint

    return {
        team: (demand.get(team, 0.0), capacity * max(quarter, 0))
        for team, capacity in state.team_capacity.items()
    }


def role_load(
    state: PlanningState,
    solve: SolveConfig,
    cancelled: set[str],
) -> dict[tuple[str, str], tuple[float, float]]:
    """
    Спрос часов роли против фонда за квартал в выбранной области пула.
    Ключ — (команда, роль) при role_pool=team, иначе ("*", роль).
    """

    demand: dict[tuple[str, str], float] = defaultdict(float)

    for task_id, task in state.tasks.items():
        if task_id in cancelled:
            continue

        for role, hours in task.remaining_hours.items():
            if not is_limited(state, solve, role):
                continue

            scope = task.team if solve.role_pool == "team" else "*"
            demand[(scope, role)] += hours

    quarter_sprints = range(
        state.current_sprint + 1,
        state.quarter + 1,
    )

    def fund(scope: str, role: str) -> float:
        # Сумма по спринтам квартала: при персональной доступности
        # фонд в спринтах разный, и «фонд × число спринтов» неверно.
        if scope == "*":
            return state.window_global_fund(role, quarter_sprints)

        return window_pool_fund(state, solve, scope, role, quarter_sprints)

    return {
        (scope, role): (hours, fund(scope, role))
        for (scope, role), hours in demand.items()
    }


class CounterfactualRunner:
    """
    Повторные решения с принудительным включением.

    Одна модель квартала без фиксаций: включение задаётся нижней
    границей бинарной переменной и снимается после решения.
    """

    def __init__(
        self,
        state: PlanningState,
        solve: SolveConfig,
        precheck: PrecheckResult,
        base_done: set[str],
    ) -> None:
        self.state = state
        self.solve = solve
        self.precheck = precheck
        self.base_done = base_done
        self.model: MilpModel | None = None
        self.used = 0
        self.deadline = time.perf_counter() + solve.counterfactual_time_limit

    def _ensure_model(self) -> MilpModel:
        if self.model is None:
            self.model = build_model(
                self.state,
                self.solve,
                self.precheck,
                last_sprint=self.state.quarter,
            )

        return self.model

    def budget_left(self) -> bool:
        return (
            self.used < self.solve.counterfactual_limit
            and time.perf_counter() < self.deadline
        )

    def force(self, kind: str, item_id: str, variable_of) -> dict[str, object]:
        """
        Решает уровни 1–2 с условием: бинарная переменная variable_of(model) = 1.
        """

        if not self.budget_left():
            return {
                "forced": kind,
                "id": item_id,
                "status": "not_computed",
                "text": "Контрфакт не рассчитан: исчерпан бюджет на контрфакты.",
            }

        self.used += 1
        model = self._ensure_model()
        forced = variable_of(model)
        forced.lowBound = 1

        try:
            value = solve_level(model, level_initiatives(model), self.deadline)
        except SolverError as error:
            text = str(error)

            if "недопустима" in text:
                return {
                    "forced": kind,
                    "id": item_id,
                    "status": "infeasible",
                    "text": "Невыполнимо при текущих ресурсах и зависимостях.",
                }

            return {
                "forced": kind,
                "id": item_id,
                "status": "not_computed",
                "text": f"Контрфакт не рассчитан: {text}.",
            }
        finally:
            forced.lowBound = 0

        decoded = decode_initiatives(model, value)

        done = {
            initiative_id
            for initiative_id, variable in model.init_done.items()
            if (variable.value() or 0) > 0.5
        }

        lost = sorted(self.base_done - done)
        gained = sorted(done - self.base_done)

        return {
            "forced": kind,
            "id": item_id,
            "status": "feasible",
            "initiatives_done": int(decoded.get("L12_initiatives", len(done))),
            "lost": [
                {"id": initiative_id, "rung": self.state.initiatives[initiative_id].rung}
                for initiative_id in lost
            ],
            "gained": gained,
        }


def _task_bounds_text(state: PlanningState, precheck: PrecheckResult, task_id: str) -> str | None:
    """
    Текст, если задача не успевает в квартал даже без конкуренции.
    """

    start = precheck.earliest_start.get(task_id)
    duration = precheck.min_duration.get(task_id)

    if start is None or duration is None:
        return None

    finish = start + duration - 1

    if finish <= state.quarter:
        return None

    return (
        f"Даже без конкуренции задача начинается не раньше спринта {start} "
        f"и занимает не меньше {duration} спринтов — завершение не раньше "
        f"спринта {finish}, за пределами квартала."
    )


def _load_text(
    state: PlanningState,
    solve: SolveConfig,
    task_id: str,
    teams: dict[str, tuple[float, float]],
    roles: dict[tuple[str, str], tuple[float, float]],
) -> list[str]:
    task = state.tasks[task_id]
    parts = []

    demand, capacity = teams[task.team]

    if demand > capacity + EPS:
        parts.append(
            f"{task.team} перегружена: {demand:.1f} SP спроса "
            f"при {capacity:.1f} за квартал."
        )

    worst = None

    for role in task.remaining_hours:
        scope = task.team if solve.role_pool == "team" else "*"
        load = roles.get((scope, role))

        if load is None or load[1] <= EPS:
            continue

        ratio = load[0] / load[1]

        if ratio > 1 + EPS and (worst is None or ratio > worst[0]):
            worst = (ratio, role, load)

    if worst is not None:
        _, role, (hours, fund) = worst
        where = "в команде" if solve.role_pool == "team" else "в организации"
        parts.append(
            f"Роль «{role}» перегружена {where}: нужно {hours:.0f} ч "
            f"при фонде {fund:.0f} ч за квартал."
        )

    return parts


def _counterfactual_text(state: PlanningState, result: dict[str, object], subject: str) -> str:
    status = result["status"]

    if status != "feasible":
        return result["text"]

    lost = [item["id"] for item in result["lost"]]

    if lost:
        return f"{subject} стоит {_format_initiatives(state, lost)}."

    return (
        f"{subject} не уменьшает число завершённых инициатив: "
        f"решение принято на следующих уровнях цели — меньше заимствований "
        f"или больше других задач в квартале."
    )


def _full(cell) -> bool:
    return cell is not None and cell.capacity > EPS and cell.planned >= cell.capacity - 1e-4


def _span(sprints: list[int]) -> str:
    if not sprints:
        return ""

    if len(sprints) == 1:
        return f"С{sprints[0]}"

    return f"С{sprints[0]}–С{sprints[-1]}"


def _allowed_start(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
    task_id: str,
) -> tuple[int, list[str]]:
    """
    Самый ранний спринт, который разрешают зависимости и решение главы
    в этом плане, и что именно его задаёт.
    """

    first = state.current_sprint + 1
    allowed, limits = first, []

    for blocking in state.predecessors(task_id):
        finish = (
            None
            if blocking in state.done_tasks or blocking not in plan.tasks
            else plan.tasks[blocking].forecast_sprint
        )
        ready = blocker_ready_from(state, solve, blocking, finish)

        if ready is None or ready <= first:
            continue

        if blocking in state.done_tasks:
            text = f"{blocking} закрыта в спринте {state.done_tasks[blocking]}"
        elif solve.gap_planned:
            text = (
                f"{blocking} завершается в спринте {finish}, зазор между "
                f"связанными задачами — {sprints_text(solve.gap_planned)}"
            )
        else:
            text = f"{blocking} завершается в спринте {finish}"

        if ready > allowed:
            allowed, limits = ready, [text]
        elif ready == allowed:
            limits.append(text)

    not_before = state.not_before.get(task_id)

    if not_before and not_before > allowed:
        allowed, limits = not_before, [
            f"глава команды запретила начинать раньше спринта {not_before}"
        ]

    return allowed, limits


def _busy_resources(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
    task_id: str,
    sprints,
) -> list[str]:
    """
    Что в этих спринтах занято целиком: SP команды задачи или часы её ролей.
    """

    task = state.tasks[task_id]
    busy = []

    team_sprints = [
        sprint
        for sprint in sprints
        if _full(plan.load.team_sp.get(task.team, {}).get(sprint))
    ]

    if team_sprints:
        busy.append(f"SP команды {task.team} в {_span(team_sprints)}")

    for role in sorted(task.remaining_hours):
        if not is_limited(state, solve, role):
            continue

        if solve.role_pool == "team":
            cells = plan.load.team_role_hours.get(task.team, {}).get(role, {})
        else:
            cells = plan.load.role_hours.get(role, {})

        role_sprints = [sprint for sprint in sprints if _full(cells.get(sprint))]

        if role_sprints:
            busy.append(f"часы роли «{role}» в {_span(role_sprints)}")

    return busy


def _included_text(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
    task_id: str,
) -> str:
    """
    Почему задача в квартале и почему именно в этих спринтах.
    """

    task = state.tasks[task_id]
    task_plan = plan.tasks[task_id]
    initiative = plan.initiatives[task.initiative]
    parts = []

    if initiative.done_in_quarter:
        parts.append(
            f"Работа квартальной цели {task.initiative} (rung {initiative.rung:g}): "
            f"инициатива завершается в спринте {initiative.forecast_sprint}."
        )
    else:
        parts.append(
            f"Инициатива {task.initiative} (rung {initiative.rung:g}) в квартале "
            f"не завершается, но эта задача помещается без ущерба для других "
            f"целей — задел на следующий квартал."
        )

    start = task_plan.sprint_start
    sprints = sorted(task_plan.progress)
    allowed, limits = _allowed_start(plan, state, solve, task_id)

    if start is not None:
        if start > allowed:
            busy = _busy_resources(plan, state, solve, task_id, range(allowed, start))

            if busy:
                parts.append(
                    f"Начало в С{start}: раньше заняты {'; '.join(busy)} — "
                    f"их получают работы целей с большим приоритетом или более ранним сроком."
                )
            else:
                parts.append(
                    f"Начало в С{start}: раньше ресурс отдан работам с более ранним "
                    f"сроком, задача при этом успевает к сроку своей цели."
                )
        elif limits:
            parts.append(
                f"Начало в С{start} — сразу, как позволяет зависимость: {'; '.join(limits)}."
            )
        else:
            parts.append(f"Начало в первом доступном спринте С{start}.")

    if len(sprints) > 1:
        causes = []
        capacity = state.team_capacity.get(task.team, 0.0)

        if task.sp_effective > capacity + EPS:
            causes.append(
                f"{task.sp_effective:.1f} SP — больше ёмкости {task.team} "
                f"за спринт ({capacity:.1f} SP)"
            )

        for role, hours in sorted(task.remaining_hours.items()):
            if not is_limited(state, solve, role):
                continue

            fund = (
                state.team_fund(task.team, role, sprints[0])
                if solve.role_pool == "team"
                else state.global_fund(role, sprints[0])
            )

            if hours > fund + EPS:
                causes.append(f"«{role}» {hours:.0f} ч при фонде роли {fund:.0f} ч за спринт")

        if not causes:
            busy = _busy_resources(plan, state, solve, task_id, sprints)
            causes.append(
                "в эти спринты ресурс делится с другими задачами: " + "; ".join(busy)
                if busy
                else "работа идёт параллельно с другими задачами команды"
            )

        parts.append(
            f"Растянута на {sprints_text(len(sprints))} ({_span(sprints)}): {'; '.join(causes)}."
        )

    external = sorted(
        role for role in task.remaining_hours
        if not is_limited(state, solve, role)
    )

    if external:
        hours = sum(task.remaining_hours[role] for role in external)
        parts.append(
            f"Роли вне штата ({', '.join(external)}) — {hours:.0f} ч "
            f"выполняют внешние исполнители."
        )

    return " ".join(parts)


def _initiative_advice(plan: PlanResult, state: PlanningState, initiative_id: str) -> str:
    """
    Что делать с инициативой, которая не завершается в квартале.
    """

    initiative = plan.initiatives[initiative_id]
    groundwork = [
        task_id
        for task_id in initiative.open_task_ids
        if plan.tasks[task_id].status == "included"
    ]
    cancelled = [
        task_id
        for task_id in initiative.open_task_ids
        if plan.tasks[task_id].status == "cancelled"
    ]

    if cancelled:
        return (
            f"Рекомендация: отменить или пересмотреть инициативу — задачи "
            f"{', '.join(cancelled)} невыполнимы при текущем составе."
        )

    if initiative.forecast_sprint is None:
        return (
            f"Рекомендация: пересмотреть объём или отменить — в горизонте "
            f"{state.horizon} спринтов инициатива не завершается."
        )

    text = (
        f"Рекомендация: перенести инициативу на следующий квартал "
        f"(прогноз — спринт {initiative.forecast_sprint})."
    )

    if groundwork:
        sp = sum(sum(plan.tasks[task_id].sp_by_sprint.values()) for task_id in groundwork)
        text += (
            f" Работы в квартале ({', '.join(groundwork)}, {sp:.1f} SP) — задел: "
            f"их можно отложить, это освободит ресурс команд без ущерба для других целей."
        )

    return text


def explain_plan(
    plan: PlanResult,
    state: PlanningState,
    solve: SolveConfig,
    precheck: PrecheckResult,
) -> None:
    """
    Дописывает в план причины переноса и контрфакты.

    Вызывается сразу после решения, до валидации и возврата плана.
    """

    cancelled = set(precheck.cancelled)
    teams = team_load(state, cancelled)
    roles = role_load(state, solve, cancelled)

    base_done = {
        initiative_id
        for initiative_id, initiative in plan.initiatives.items()
        if initiative.done_in_quarter
    }

    runner = None

    if solve.counterfactuals and plan.meta.engine == "milp":
        runner = CounterfactualRunner(state, solve, precheck, base_done)

    # Сначала инициативы — они определяют главный показатель.
    for initiative_id, initiative in sorted(
        plan.initiatives.items(),
        key=lambda item: (-item[1].rung, item[0]),
    ):
        if initiative.done_in_quarter:
            continue

        cancelled_tasks = [
            task_id
            for task_id in initiative.blocking_task_ids
            if plan.tasks[task_id].status == "cancelled"
        ]

        text = (
            f"Инициатива не завершается в квартале: мешают задачи "
            f"{', '.join(initiative.blocking_task_ids)}."
        )

        if cancelled_tasks:
            result = {
                "forced": "initiative",
                "id": initiative_id,
                "status": "infeasible",
                "text": (
                    f"Невыполнимо: отменены задачи {', '.join(cancelled_tasks)} — "
                    f"{plan.tasks[cancelled_tasks[0]].reason_text}"
                ),
            }
        elif runner is not None:
            result = runner.force(
                "initiative",
                initiative_id,
                lambda model, key=initiative_id: model.init_done[key],
            )
        else:
            result = None

        if result is not None:
            initiative.counterfactual = result
            text += " " + _counterfactual_text(state, result, "Её спасение")

        text += " " + _initiative_advice(plan, state, initiative_id)
        initiative.reason_text = text

    for task_id in plan.tasks_with_status("postponed"):
        task_plan = plan.tasks[task_id]
        initiative = state.initiatives[task_plan.initiative]
        initiative_done = plan.initiatives[initiative.initiative_id].done_in_quarter

        parts = _load_text(state, solve, task_id, teams, roles)
        bounds = _task_bounds_text(state, precheck, task_id)

        if bounds:
            parts.append(bounds)

        result = None

        if bounds is None and runner is not None:
            result = runner.force(
                "task",
                task_id,
                lambda model, key=task_id: model.finished[(key, model.quarter)],
            )
            task_plan.counterfactual = result

        lost = (
            [item["id"] for item in result["lost"]]
            if result is not None and result["status"] == "feasible"
            else None
        )

        if lost:
            parts.append(
                f"Задача проиграла приоритетам: её включение стоит "
                f"{_format_initiatives(state, lost)}."
            )
        elif lost == []:
            reason = (
                f"Её инициатива {initiative.initiative_id} (rung {initiative.rung:g}) "
                f"в квартале всё равно не завершается, поэтому "
                if not initiative_done
                else ""
            )
            parts.append(
                f"{reason}включение задачи не добавляет завершённых инициатив; "
                f"она отложена ради меньших заимствований у других команд "
                f"или большего числа других задач в квартале."
                if reason
                else "Включение задачи не уменьшает число завершённых инициатив; "
                f"она отложена ради меньших заимствований у других команд "
                f"или большего числа других задач в квартале."
            )
        elif result is not None:
            parts.append(result["text"])
        elif not bounds:
            parts.append(
                f"Задача выполнима, но проиграла приоритетам; её инициатива "
                f"{initiative.initiative_id} (rung {initiative.rung:g})."
            )

        if task_plan.forecast_sprint is None:
            parts.append(f"В горизонте {state.horizon} спринтов задача не завершается.")
            parts.append("Рекомендация: пересмотреть объём или отменить.")
            decision = "cancel"
        else:
            parts.append(f"Прогноз завершения — спринт {task_plan.forecast_sprint}.")
            parts.append("Рекомендация: перенести на следующий квартал.")
            decision = "postpone"

        if state.tasks[task_id].status == "InProgress" and not any(
            sprint <= state.quarter for sprint in task_plan.progress
        ):
            parts.append(
                "Задача уже в работе, но в квартале приостанавливается: закончить её "
                "в квартале не получается, а план не тратит квартальный ресурс на "
                "работу, которая в нём не завершится. Если её важно продолжать, "
                "поднимите приоритет её инициативы или освободите ресурс команды."
            )

        task_plan.reason_text = " ".join(parts)
        task_plan.details = {
            **task_plan.details,
            "decision": decision,
            "team_load": dict(zip(("demand_sp", "capacity_sp"), teams[task_plan.team])),
        }

    # Первопричина срыва инициативы — из причин её отложенных задач,
    # они известны только после разбора задач.
    for initiative in plan.initiatives.values():
        if initiative.done_in_quarter or not initiative.reason_text:
            continue

        causes = first_sentences(
            plan.tasks[task_id].reason_text
            for task_id in initiative.blocking_task_ids
            if plan.tasks[task_id].status == "postponed"
        )

        if causes:
            head, _, tail = initiative.reason_text.partition(". ")
            initiative.reason_text = f"{head}. Причина: {' '.join(causes)} {tail}".strip()

    for task_id, task_plan in plan.tasks.items():
        if task_plan.status == "cancelled":
            task_plan.details = {**task_plan.details, "decision": "cancel"}
            continue

        if task_plan.status != "included":
            continue

        initiative_done = plan.initiatives[task_plan.initiative].done_in_quarter
        task_plan.details = {
            **task_plan.details,
            "decision": "include" if initiative_done else "groundwork",
        }

        # Задача в работе без остатка часов уже объяснена сборкой плана.
        if task_plan.reason_text is None:
            task_plan.reason_text = _included_text(plan, state, solve, task_id)

    if runner is not None:
        plan.meta.objective["counterfactuals_run"] = float(runner.used)
