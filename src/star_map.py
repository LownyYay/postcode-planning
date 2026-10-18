"""
Звёздная карта: Bus Factor по ролям и технологиям, похожие профили.

Bus Factor компетенции — число разных людей, которые ею владеют.
Совместитель, записанный в нескольких командах, считается одним
человеком. BF = 1 означает, что без одного сотрудника компетенции
в организации (или в команде) не остаётся.

Считается на двух уровнях:
- по ролям — роль задаёт, кто может выполнять часы сметы задачи,
  поэтому BF роли прямо показывает, какие работы встанут без человека;
- по технологиям из заявленного стека (skills_declared) — где знание
  сосредоточено в одной голове, даже если роль есть у нескольких.

Похожие профили ищутся кластеризацией DBSCAN по стеку: человек без
кластера — уникальный профиль, его трудно подменить целиком.
"""

from collections import defaultdict

import pandas as pd
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import MultiLabelBinarizer

from src.importer import split_skills


# Профили похожи, если косинусная близость их наборов технологий
# не ниже 0.4 (расстояние не больше 0.6). На официальном датасете так
# складываются естественные группы: Java-разработчики, аналитики,
# фронтенд, ручное тестирование, автотестирование. При 0.3 уникальными
# оказывались 25 человек из 30, при 0.7 в одну группу сливались
# тестировщики и аналитики.
SIMILARITY_DISTANCE = 0.6


def collapse_engineers(engineers: pd.DataFrame) -> pd.DataFrame:
    """
    Сворачивает таблицу распределения до одной строки на инженера.

    Совместитель записан в таблице по разу на каждую команду.
    Для Bus Factor и кластеризации нужен человек, а не строка
    распределения, иначе его навыки посчитаются дважды.
    """

    df = engineers.copy()

    # Навыки берём уже разобранными при импорте,
    # если колонки нет — разбираем здесь тем же правилом.
    if "skills" not in df.columns:
        df["skills"] = df["skills_declared"].apply(split_skills)

    teams = (
        df.groupby("engineer_id")["team_id"]
        .apply(lambda values: ", ".join(sorted(set(values))))
    )

    people = (
        df.drop_duplicates("engineer_id")
        .set_index("engineer_id")
        .drop(columns=["team_id"])
    )

    people["team_id"] = teams
    people["skills_list"] = people["skills"]

    return people.reset_index()


def analyze_bus_factor(engineers: pd.DataFrame):
    """
    Bus Factor технологий и похожие профили по стеку.

    Возвращает:
    1. Датафрейм инженеров (одна строка на человека) с колонкой cluster_id.
       В team_id у совместителя перечислены все его команды.
    2. Датафрейм с уникальными специалистами (cluster_id == -1).
    3. Словарь технология → Bus Factor: сколько разных людей ею владеет.
    """
    df = collapse_engineers(engineers)

    # Защита от пустых списков навыков
    if df["skills_list"].apply(len).sum() == 0:
        df["cluster_id"] = 0
        return df, pd.DataFrame(columns=df.columns), {}

    # Векторизация (One-Hot Encoding для навыков)
    mlb = MultiLabelBinarizer()
    skills_matrix = mlb.fit_transform(df["skills_list"])

    # Кластеризация DBSCAN: двое похожих уже образуют группу.
    dbscan = DBSCAN(eps=SIMILARITY_DISTANCE, min_samples=2, metric="cosine")
    df["cluster_id"] = dbscan.fit_predict(skills_matrix)

    # Выделение уникальных инженеров
    unique_engineers = df[df["cluster_id"] == -1].copy()

    # Расчет Bus Factor (сколько людей знают каждую технологию)
    skills_classes = mlb.classes_
    skills_counts = skills_matrix.sum(axis=0)

    bus_factor_report = {
        str(skill): int(count)
        for skill, count in zip(skills_classes, skills_counts)
    }

    return df, unique_engineers, bus_factor_report


def role_bus_factor(
    people: list[dict],
    tasks: list[dict],
    hours_per_sprint: float = 80.0,
) -> list[dict]:
    """
    Bus Factor ролей и работы, которые от них зависят.

    people — люди: id, roles, allocations (команда → доля ставки);
    tasks — открытые задачи: id, team, initiative, remaining_hours.

    На каждую роль, которая есть у людей или нужна задачам:
    - holders — носители роли, bf — их число;
    - fund_hours — часов роли за спринт при полной доступности
      (доля ставки × часы спринта по всем носителям);
    - demand_hours — остаток часов роли в открытых задачах;
    - tasks, initiatives, teams — где роль нужна;
    - holder_teams — где работают носители.

    bf = 1: без единственного носителя все эти работы встают, другого
    исполнителя роли в организации нет. bf = 0: роли нет ни у кого —
    работа выполняется вне штата.
    """

    holders: dict[str, list[dict]] = defaultdict(list)

    for person in people:
        for role in person.get("roles", []):
            holders[role].append(person)

    demand: dict[str, float] = defaultdict(float)
    role_tasks: dict[str, set[str]] = defaultdict(set)
    role_initiatives: dict[str, set[str]] = defaultdict(set)
    role_teams: dict[str, set[str]] = defaultdict(set)

    for task in tasks:
        for role, hours in task.get("remaining_hours", {}).items():
            if hours <= 0:
                continue

            demand[role] += hours
            role_tasks[role].add(task["id"])
            role_initiatives[role].add(str(task["initiative"]))
            role_teams[role].add(task["team"])

    rows = []

    for role in sorted(set(holders) | set(demand)):
        members = sorted(holders.get(role, []), key=lambda person: person["id"])
        rows.append(
            {
                "role": role,
                "holders": [person["id"] for person in members],
                "bf": len(members),
                "fund_hours": sum(
                    sum(person.get("allocations", {}).values()) * hours_per_sprint
                    for person in members
                ),
                "demand_hours": demand.get(role, 0.0),
                "tasks": sorted(role_tasks.get(role, ())),
                "initiatives": sorted(role_initiatives.get(role, ())),
                "teams": sorted(role_teams.get(role, ())),
                "holder_teams": sorted(
                    {team for person in members for team in person.get("allocations", {})}
                ),
            }
        )

    return sorted(rows, key=lambda row: (row["bf"] if row["bf"] else 99, -row["demand_hours"], row["role"]))
