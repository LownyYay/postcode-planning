"""Сквозная проверка стыка: состояние ОРБИТЫ → адаптер → ядро → валидатор.

Пока этот тест не проходит, подменять расчёт на сервере нельзя.
Кроме самой проходимости он закрепляет числа из постановки задачи:
ёмкость команды, часы ставки, зазор зависимостей и растяжение работы,
которая не помещается в фонд одного спринта.
"""

import sys
from copy import deepcopy
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]

for path in (PROJECT_DIR, PROJECT_DIR / "orbit"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from planner.bridge import core_state, role_key  # noqa: E402
from planner.present import orbit_plan  # noqa: E402
from planner.engine import default_config  # noqa: E402
from planner.importer import import_xlsx  # noqa: E402
from src.planner import SolveConfig, plan, validate_plan  # noqa: E402
from src.planner.rules import is_limited  # noqa: E402
from src.risks import find_risks  # noqa: E402


from src.dataset import DATASET_PATH as DATASET  # noqa: E402

# Специалисты организации общие: команда может взять часы роли сверх
# своего фонда, и такие часы попадают в таблицу заимствований — именно
# из неё вырастает заявка на помощь между командами. Замыкание каждой
# команды на себя отменяет 31 задачу из 37: в датасете шесть ролей
# есть не в каждой команде.
SOLVE = SolveConfig(role_pool="team_borrow", counterfactuals=False)


@pytest.fixture(scope="module")
def data():
    if not DATASET.exists():
        pytest.skip(f"нет датасета {DATASET}")

    return import_xlsx(DATASET, "Обновлённый датасет хакатона")


@pytest.fixture(scope="module")
def config():
    return default_config()


@pytest.fixture(scope="module")
def state(data, config):
    return core_state(data, config)


@pytest.fixture(scope="module")
def result(state):
    return plan(state, SOLVE)


def test_plan_passes_validator(state, result):
    """
    Главное: план из состояния ОРБИТЫ проходит независимый валидатор.
    """

    validate_plan(result, state, SOLVE)

    assert result.meta.status in ("optimal", "greedy", "fallback_greedy")
    assert set(result.tasks) == set(state.tasks)


def test_every_open_task_has_a_decision(state, result):
    """
    Решение принято по каждой открытой задаче, и у переноса
    и отмены есть причина простым языком.
    """

    for task_id, task_plan in result.tasks.items():
        assert task_plan.status in ("included", "postponed", "cancelled")

        if task_plan.status != "included":
            assert task_plan.reason_text, f"{task_id}: нет причины"
            assert task_plan.reason_code, f"{task_id}: нет кода причины"


def test_initiatives_are_decided(state, result):
    """
    По каждой инициативе есть вердикт: завершается в квартале или нет.
    """

    assert result.initiatives

    for initiative in result.initiatives.values():
        if not initiative.done_in_quarter:
            assert (
                initiative.forecast_sprint is None
                or initiative.forecast_sprint > state.quarter
            )


def test_team_capacity_is_mean_velocity_with_focus_factor(data, state):
    """
    Постановка: ресурс команды на спринт — средняя фактическая
    производительность с коэффициентом 0,8.
    """

    achieved = {}

    for row in data["history"]:
        achieved.setdefault(row["team"], []).append(row["achieved"])

    for team, values in achieved.items():
        expected = sum(values) / len(values) * 0.8

        assert state.team_capacity[team] == pytest.approx(expected)


def test_full_rate_gives_eighty_hours(data, config, state):
    """
    Постановка: до 80 часов за спринт при полной занятости
    и до 40 часов при занятости 0,5 ставки.
    """

    sprint = state.current_sprint + 1

    for person in data["engineers"]:
        for team, rate in person["allocations"].items():
            own = [
                role
                for role in person["roles"]
                if state.team_fund(team, role, sprint) > 0
            ]

            if not own:
                continue

            # Фонд роли складывается из часов её носителей, поэтому
            # проверяем вклад человека, а не итог по команде.
            assert rate in (0.5, 1.0)

    hours = {0.5: 40.0, 1.0: 80.0}

    for person in data["engineers"]:
        for team, rate in sorted(person["allocations"].items()):
            solo = [
                role
                for role in person["roles"]
                if _only_holder(data, config, team, role, person["id"])
            ]

            for role in solo:
                assert state.team_fund(team, role, sprint) == pytest.approx(
                    hours[rate]
                )


def _only_holder(data, config, team, role, engineer_id):
    """
    Единственный ли это носитель роли в команде.
    """

    holders = [
        person["id"]
        for person in data["engineers"]
        if role in person["roles"] and person["allocations"].get(team, 0) > 0
    ]

    return holders == [engineer_id]


def test_dependency_gap_is_respected(state, result):
    """
    Постановка: между блокирующей и блокируемой задачей зазор
    минимум в один спринт; после фактического Done — следующий спринт.
    """

    for blocking, blocked in state.dependencies:
        blocked_plan = result.tasks.get(blocked)

        if blocked_plan is None or not blocked_plan.progress:
            continue

        start = min(blocked_plan.progress)

        if blocking in state.done_tasks:
            assert start >= state.done_tasks[blocking] + 1
            continue

        blocking_plan = result.tasks.get(blocking)

        if blocking_plan is None or not blocking_plan.progress:
            continue

        finish = max(blocking_plan.progress)

        assert start >= finish + 2, (
            f"{blocking} → {blocked}: нет пустого спринта между работами"
        )


def test_work_beyond_one_sprint_fund_is_stretched(state, result):
    """
    Пример из онбординга: если роли нужно больше часов, чем даёт фонд
    одного спринта, работа растягивается, а не исчезает.

    DB-202 требует 90 ч роли «Разработчик .net» при фонде 80 ч.
    """

    task = state.tasks["DB-202"]
    role = role_key("Разработчик .net")
    needed = task.remaining_hours[role]
    sprint = state.current_sprint + 1
    fund = state.team_fund(task.team, role, sprint)

    assert needed > fund, "пример потерял смысл: работа помещается в спринт"

    task_plan = result.tasks["DB-202"]

    if task_plan.status == "cancelled":
        pytest.skip("задача отменена предпроверкой, растягивать нечего")

    assert len(task_plan.progress) >= 2
    assert max(task_plan.hours_by_sprint[s].get(role, 0.0)
               for s in task_plan.progress) <= fund + 1e-6


def test_personal_availability_zeroes_the_fund(data, config):
    """
    Персональная доступность по спринтам: инженер, недоступный
    в спринте, не отдаёт часы в фонд своей роли.
    """

    person = next(
        p for p in data["engineers"]
        if p["roles"] and len(p["allocations"]) == 1
    )
    team = next(iter(person["allocations"]))
    role = role_key(person["roles"][0])

    changed = deepcopy(config)
    changed["availability"] = {person["id"]: {"1": 0, "2": 0}}

    before = core_state(data, config)
    after = core_state(data, changed)

    assert after.team_fund(team, role, 1) < before.team_fund(team, role, 1)
    assert after.team_fund(team, role, 3) == before.team_fund(team, role, 3)

    result = plan(after, SOLVE)
    validate_plan(result, after, SOLVE)


def test_transfer_moves_hours_between_teams(data, config):
    """
    Согласованная передача часов сдвигает фонд ровно в тех спринтах,
    на которые она согласована.
    """

    person = next(
        p for p in data["engineers"]
        if p["roles"] and len(p["allocations"]) == 1
    )
    donor = next(iter(person["allocations"]))
    target = next(
        t for t in {x for p in data["engineers"] for x in p["allocations"]}
        if t != donor
    )
    role = role_key(person["roles"][0])

    changed = deepcopy(config)
    changed["transfers"] = [
        {
            "engineer": person["id"],
            "from_team": donor,
            "to_team": target,
            "hours": 20,
            "start": 2,
            "end": 3,
        }
    ]

    before = core_state(data, config)
    after = core_state(data, changed)

    assert after.team_fund(target, role, 2) == pytest.approx(
        before.team_fund(target, role, 2) + 20
    )
    assert after.team_fund(donor, role, 2) == pytest.approx(
        before.team_fund(donor, role, 2) - 20
    )
    assert after.team_fund(target, role, 4) == before.team_fund(target, role, 4)

    result = plan(after, SOLVE)
    validate_plan(result, after, SOLVE)


def test_required_skill_narrows_the_fund(data, config):
    """
    Правило технологического соответствия: требование стека сужает
    круг носителей роли, а значит и фонд часов этой работы.
    """

    task = next(
        t for t in data["tasks"]
        if t["status"] != "Done" and t["hours"]
    )
    role = next(iter(task["hours"]))

    changed = deepcopy(config)
    changed["task_overrides"] = {
        task["id"]: {"required_skills": {role: ["Заведомо отсутствующий стек"]}}
    }

    state = core_state(data, changed)
    narrowed = role_key(role, ["Заведомо отсутствующий стек"])

    assert narrowed in state.tasks[task["id"]].remaining_hours
    assert state.team_fund(task["team"], narrowed, 1) == 0.0

    # Роль, которую сузили требованием, не уходит «во внешние»:
    # иначе требование стека перестало бы действовать.
    assert is_limited(state, SOLVE, narrowed)

    result = plan(state, SOLVE)
    validate_plan(result, state, SOLVE)

    assert result.tasks[task["id"]].status == "cancelled"
    assert result.tasks[task["id"]].reason_text


def test_manual_start_is_respected(data, config):
    """
    Глава команды может запретить начинать раньше выбранного спринта.
    """

    task = next(t for t in data["tasks"] if t["status"] != "Done")

    changed = deepcopy(config)
    changed["not_before"] = {task["id"]: 4}

    state = core_state(data, changed)
    result = plan(state, SOLVE)
    validate_plan(result, state, SOLVE)

    progress = result.tasks[task["id"]].progress

    if progress:
        assert min(progress) >= 4


def test_hours_limit_prevents_double_spending_a_person(data, config):
    """
    Человек с двумя ролями отдаёт часы в каждый фонд. Потолок часов
    не даёт потратить его дважды: сумма по всем ролям за спринт
    не больше часов различных инженеров организации.
    """

    state = core_state(data, config)
    result = plan(state, SOLVE)
    used = {}

    for task_plan in result.tasks.values():
        for sprint, roles in task_plan.hours_by_sprint.items():
            for role, hours in roles.items():
                if is_limited(state, SOLVE, role):
                    used[sprint] = used.get(sprint, 0.0) + hours

    assert used, "план не потратил ни одного часа"

    for sprint, hours in used.items():
        limit = state.sprint_hours_limit("*", sprint)

        assert limit is not None, f"спринт {sprint}: не задан потолок"
        assert hours <= limit + 1e-4, f"спринт {sprint}: {hours} при {limit}"


def test_team_excess_is_recorded_as_borrowing(data, config):
    """
    Часы, взятые командой сверх своего фонда, не теряются: каждый такой
    час попадает в таблицу заимствований. Из неё и вырастает заявка
    на помощь между командами.
    """

    state = core_state(data, config)
    result = plan(state, SOLVE)

    recorded = {
        (row.team, row.role, row.sprint): row.hours
        for row in result.borrowing
    }

    used = {}

    for task_plan in result.tasks.values():
        for sprint, roles in task_plan.hours_by_sprint.items():
            for role, hours in roles.items():
                if is_limited(state, SOLVE, role):
                    key = (task_plan.team, role, sprint)
                    used[key] = used.get(key, 0.0) + hours

    for (team, role, sprint), hours in used.items():
        excess = hours - state.team_fund(team, role, sprint)

        if excess > 1e-3:
            assert recorded.get((team, role, sprint)) == pytest.approx(
                excess, abs=1e-3
            ), f"{team} / {role}, спринт {sprint}: заимствование не записано"


def test_risks_are_produced(state, result):
    """
    Риски считаются на том же состоянии и разложены по уровням
    из постановки: красный, оранжевый, жёлтый.
    """

    risks = find_risks(result, state, SOLVE)

    assert all(risk.level in ("red", "orange", "yellow") for risk in risks)
    assert all(risk.text for risk in risks)


# --- обратный адаптер: план ядра в формате интерфейса ---


@pytest.fixture(scope="module")
def presented(state, result, data, config):
    return orbit_plan(state, result, data, config, solve=SOLVE)


def test_presented_plan_matches_interface_contract(presented, data):
    """
    Интерфейс читает entries, resources, edges и initiatives.
    Формат должен совпадать с тем, что он уже умеет показывать.
    """

    assert set(presented["entries"]) == {t["id"] for t in data["tasks"]}

    for task_id, entry in presented["entries"].items():
        assert set(entry) >= {
            "id", "start", "end", "status", "fragments", "reasons", "waits",
        }
        assert entry["status"] in (
            "done", "planned", "beyond", "blocked", "partial",
        )
        assert entry["reasons"], f"{task_id}: пустые причины"

        for fragment in entry["fragments"]:
            assert set(fragment) == {"sprint", "fraction", "sp", "assignments"}
            assert 0 < fragment["fraction"] <= 1 + 1e-9

    for initiative in presented["initiatives"]:
        assert initiative["risk"] in ("ok", "beyond", "unknown")

    assert all(len(edge) == 2 for edge in presented["edges"])
    assert all("capacity" in row for row in presented["resources"].values())


def test_validation_is_returned_not_raised(presented):
    """
    Независимая проверка возвращается полем, а не исключением:
    сервер должен показать план с пометкой, а не упасть.
    """

    assert isinstance(presented["validation"], list)


def test_fragments_repeat_the_core_plan(state, result, presented):
    """
    Доли и очки фрагментов совпадают с планом ядра один в один,
    а не пересчитываются заново.
    """

    for task_id, task_plan in result.tasks.items():
        fragments = {
            fragment["sprint"]: fragment
            for fragment in presented["entries"][task_id]["fragments"]
        }

        assert set(fragments) == set(task_plan.progress)

        for sprint, share in task_plan.progress.items():
            assert fragments[sprint]["fraction"] == pytest.approx(share)
            assert fragments[sprint]["sp"] == pytest.approx(
                task_plan.sp_by_sprint[sprint]
            )


def test_people_are_assigned_within_their_hours(presented, data, config):
    """
    Часы ролей разложены на конкретных инженеров, и никто
    не занят сверх своей доступности в спринте.
    """

    from planner.engine import capacities

    assigned = {}

    for entry in presented["entries"].values():
        for fragment in entry["fragments"]:
            for row in fragment["assignments"]:
                key = (row["engineer"], fragment["sprint"])
                assigned[key] = assigned.get(key, 0.0) + row["hours"]

    assert assigned, "ни одна работа не разложена на людей"

    for (engineer_id, sprint), hours in assigned.items():
        available, _ = capacities(data, config, sprint)

        assert hours <= available[engineer_id] + 1e-4, (
            f"{engineer_id}, спринт {sprint}: {hours} ч при "
            f"{available[engineer_id]} ч доступных"
        )


def test_assignments_respect_roles_and_skills(presented, data, config):
    """
    Инженер получает только ту роль, которая у него подтверждена.
    """

    people = {p["id"]: p for p in data["engineers"]}

    for entry in presented["entries"].values():
        for fragment in entry["fragments"]:
            for row in fragment["assignments"]:
                person = people[row["engineer"]]

                assert row["role"] in person["roles"], (
                    f"{row['engineer']}: роль {row['role']} не подтверждена"
                )
                assert row["hours"] > 0


def test_assignments_cover_the_planned_hours(state, result, presented):
    """
    Каждый час роли, у которой есть носители, разложен на людей.

    Роли без единого носителя выполняются вне штата: ядро их не
    ограничивает, раскладывать нечего, и они показаны диагностикой.
    """

    unassigned = {
        line.split(",")[0]
        for line in presented["validation"]
        if "разложено" in line
    }

    checked = 0

    for task_id, task_plan in result.tasks.items():
        if task_id in unassigned:
            continue

        fragments = {
            fragment["sprint"]: fragment
            for fragment in presented["entries"][task_id]["fragments"]
        }

        for sprint, roles in task_plan.hours_by_sprint.items():
            needed = sum(
                hours
                for role, hours in roles.items()
                if is_limited(state, SOLVE, role)
            )
            placed = sum(
                row["hours"] for row in fragments[sprint]["assignments"]
            )

            assert placed == pytest.approx(needed, abs=1e-3)
            checked += 1

    assert checked, "проверка не выполнила ни одного сравнения"


def test_external_roles_are_reported_not_hidden(presented):
    """
    Часы ролей без носителей не молчат: по каждой такой роли
    есть диагностика с числом часов.
    """

    external = [
        row for row in presented["diagnostics"]
        if row["code"] == "external_role"
    ]

    assert external, "роли без носителей не показаны"

    for row in external:
        assert row["entity"] and "ч" in row["message"]


def test_validation_is_empty_on_a_correct_plan(presented):
    """
    На исправном плане независимая проверка молчит: иначе интерфейс
    покажет тревогу там, где всё в порядке.
    """

    assert presented["validation"] == []


# --- звёздная карта и Bus Factor на плане ядра ---


def test_star_map_reads_the_core_plan(presented, data, config):
    """
    Звёздная карта строится на новом плане без правок: состав команд,
    компетенции, Bus Factor и загрузка людей по спринту.
    """

    from planner.engine import skills_map

    sprint = presented["cutoff"] + 1
    star = skills_map(data, config, presented, sprint)

    assert star["cells"], "звёздная карта пуста"
    assert len(star["people"]) == len(data["engineers"])

    for cell in star["cells"]:
        assert cell["bf"] == len(cell["holders"])
        assert cell["free_hours"] <= cell["available_hours"] + 1e-6

    # Постановка требует выделить компетенции с единственным носителем
    # и показать, где это создаёт риск для работ.
    single = [cell for cell in star["cells"] if cell["bf"] == 1]

    assert single, "не найдено ни одной компетенции с одним носителем"
    assert any(cell["related_tasks"] for cell in single), (
        "компетенции с одним носителем не связаны ни с одной работой"
    )


def test_star_map_load_comes_from_assignments(presented, data, config):
    """
    Загрузка человека в звёздной карте — это часы из назначений
    плана, а не отдельный расчёт.
    """

    from planner.engine import skills_map

    sprint = presented["cutoff"] + 1
    star = skills_map(data, config, presented, sprint)

    expected = {}

    for entry in presented["entries"].values():
        for fragment in entry["fragments"]:
            if fragment["sprint"] == sprint:
                for row in fragment["assignments"]:
                    expected[row["engineer"]] = (
                        expected.get(row["engineer"], 0.0) + row["hours"]
                    )

    for person in star["people"]:
        assert person["used"] == pytest.approx(
            expected.get(person["id"], 0.0)
        )


def test_pinned_person_is_not_taken_by_earlier_work(data, config):
    """
    Работа, закреплённая за человеком, получает его часы, даже если его
    идентификатор стоит первым и другие работы той же роли разобрали
    бы его раньше. Раньше исход зависел от случайного ID нового сотрудника.
    """

    from planner.present import build_plan

    changed_data = deepcopy(data)
    changed_data["engineers"].append(
        dict(id="ENG-000", name="Новый", roles=["Разработчик Java"], skills=["Java"],
             grade="Middle", allocations={"Team-L": 1.0})
    )
    changed_data["tasks"].append(
        dict(id="NEW-1", initiative="NEW-I", title="Новая", team="Team-L", parent="",
             status="ToDo", priority=999, sp=1, original_hh=4,
             hours={"Разработчик Java": 4}, spent={}, required_skills={},
             planned_start="", planned_end="", actual_end="", result="",
             desired_result="", source_row=None)
    )
    changed = deepcopy(config)
    changed["assignments"] = {"NEW-1": {"Разработчик Java": "ENG-000"}}
    changed["not_before"] = {"NEW-1": 3}

    plan_view = build_plan(changed_data, changed)
    entry = plan_view["entries"]["NEW-1"]
    people = {a["engineer"] for f in entry["fragments"] for a in f["assignments"]}

    assert entry["start"] >= 3
    assert people == {"ENG-000"}


def test_zero_role_fund_does_not_break_risks(data, config):
    """
    Все носители роли в отпуске: фонд спринта нулевой, а оранжевый
    алерт всё равно строится — без деления на ноль.
    """

    changed = deepcopy(config)
    changed["availability"] = {
        p["id"]: {"1": 0}
        for p in data["engineers"]
        if "Разработчик Java" in p["roles"]
    }

    state = core_state(data, changed)
    result = plan(state, SOLVE)

    risks = find_risks(result, state, SOLVE)

    assert all(r.text for r in risks)


def test_fact_replan_uses_core_rules(data):
    """
    Полный цикл через сервис: фиксация базы, факт спринта, пересчёт.

    База берёт квартальные цели и плановые SP у ядра. Пересчёт идёт
    со стабильностью, изменения считаются относительно базы, а зачёт
    SP спринта — по потраченным часам, а не по заявленным SP команд.
    """

    import tempfile

    from planner.engine import snapshot, teams_of
    from planner.present import quarter_metrics, unpack_core
    from planner.service import Service
    from planner.store import Store

    with tempfile.TemporaryDirectory() as folder:
        service = Service(Store(Path(folder) / "t.db"))
        state = service.create(deepcopy(data))
        state = service.approve(dict(project_id=state["project_id"], revision=state["revision"]))

        baseline = state["baseline"]
        core = unpack_core(baseline["plan"])
        assert set(baseline["goal_ids"]) == {
            i for i, plan_ in core.initiatives.items() if plan_.done_in_quarter
        }

        outcomes = []

        for task_id, task in snapshot(state["dataset"], state["config"], []).items():
            if task["status"] == "Done":
                continue

            entry = state["plan"]["entries"][task_id]
            spent = {}

            for fragment in entry["fragments"]:
                if fragment["sprint"] == 1:
                    for row in fragment["assignments"]:
                        spent[row["role"]] = spent.get(row["role"], 0) + row["hours"]

            outcomes.append(dict(
                task_id=task_id,
                status="Done" if entry["end"] == 1 else task["status"],
                remaining_sp=None,
                remaining_hours=None,
                spent_hours=spent,
            ))

        report = dict(sprint=1, teams={t: 1.0 for t in teams_of(state["dataset"])}, tasks=outcomes)
        state = service.actual(dict(project_id=state["project_id"], revision=state["revision"], report=report))

        assert state["plan"]["engine"]["stability"] is True
        assert unpack_core(state["plan"]).changes
        assert set(state["plan"]["risk_summary"]) == {"red", "yellow", "orange"}

        metrics = quarter_metrics(state)
        first = metrics["sprints"][0]

        assert metrics["goal_count"] == len(baseline["goal_ids"])
        assert first["reported"] == pytest.approx(len(report["teams"]))
        assert first["actual"] > first["reported"]
        assert 0 < first["percent"] <= 100 + 1e-6


def test_own_team_first_and_nobody_idle(data, config):
    """
    Работу команды сначала получают её люди, и нагрузка расходится
    по всем, а не копится на первых по табельному номеру.

    Раньше Java-задачи Team-Alpha делали разработчики Team-L,
    а свой разработчик ENG-424 стоял без работы весь квартал.
    """

    from collections import defaultdict

    from planner.present import build_plan

    plan_view = build_plan(data, config)
    teams = {p["id"]: p["allocations"] for p in data["engineers"]}
    load = defaultdict(float)
    own = defaultdict(float)

    for task_id, entry in plan_view["entries"].items():
        team = plan_view["tasks"][task_id]["team"]

        for fragment in entry["fragments"]:
            for row in fragment["assignments"]:
                load[row["engineer"]] += row["hours"]

                if team in teams[row["engineer"]]:
                    own[row["engineer"]] += row["hours"]

    assert [p["id"] for p in data["engineers"] if load[p["id"]] <= 0] == []
    assert own["ENG-424"] > 0
    assert not plan_view["validation"]


def test_absence_reduces_hours_only_in_its_sprints(data, config):
    """
    Неделя отпуска снимает половину часов человека в своём спринте
    и уменьшает фонд его роли в ядре; другие спринты не трогает.
    """

    from planner.engine import absence_impact, capacities

    person = next(p for p in data["engineers"] if p["id"] == "ENG-424")
    role = role_key(person["roles"][0])
    team = next(iter(person["allocations"]))

    changed = deepcopy(config)
    changed["absences"] = [dict(id="a1", engineer="ENG-424", start="2026-10-05", end="2026-10-09",
                                reason="Отпуск", comment="")]

    assert capacities(data, changed, 1)[0]["ENG-424"] == 40
    assert capacities(data, changed, 2)[0]["ENG-424"] == 80
    assert absence_impact(data, changed, changed["absences"][0]) == {"1": 40.0}

    before = core_state(data, config)
    after = core_state(data, changed)

    assert after.team_fund(team, role, 1) == before.team_fund(team, role, 1) - 40
    assert after.team_fund(team, role, 2) == before.team_fund(team, role, 2)

    result = plan(after, SOLVE)
    validate_plan(result, after, SOLVE)
