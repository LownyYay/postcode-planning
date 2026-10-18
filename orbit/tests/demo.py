"""Явно синтетический сценарий для демонстрации; не подменяет Excel."""


def demo_data():
    def task(tid, title, team, initiative, sp, hours, priority=80, skills=None):
        return dict(id=tid, title=title, team=team, initiative=initiative, parent="", status="ToDo", priority=priority,
                    sp=sp, hours=hours, spent={}, original_hh=sum(hours.values()), required_skills=skills or {},
                    planned_start="", planned_end="", actual_end="", result="", desired_result="Выпущено в эксплуатацию", source_row=None)
    def person(eid, name, role, skills, allocations):
        return dict(id=eid, name=name, roles=[role], skills=skills, grade="Middle", allocations=allocations)
    tasks = [
        task("API-1", "API личного кабинета", "Команда Продукт", "GOAL-1", 6, {"Разработчик": 120}, 95, {"Разработчик": ["Python"]}),
        task("QA-1", "Автотесты личного кабинета", "Команда Продукт", "GOAL-1", 4, {"Автотестировщик": 80}, 95, {"Автотестировщик": ["Playwright"]}),
        task("REL-1", "Выпуск личного кабинета", "Команда Продукт", "GOAL-1", 2, {"Разработчик": 16}, 95),
        task("API-2", "История заказов", "Команда Продукт", "GOAL-2", 6, {"Разработчик": 80}, 80),
        task("QA-2", "Проверка истории заказов", "Команда Продукт", "GOAL-2", 4, {"Автотестировщик": 80}, 80),
        task("APP-3", "Персональные уведомления", "Команда Продукт", "GOAL-3", 6, {"Разработчик": 80, "Автотестировщик": 40}, 70),
        task("CORE-1", "Метрики сервисов", "Команда Платформа", "GOAL-4", 4, {"Разработчик": 48}, 90),
        task("CORE-2", "Регресс платформы", "Команда Платформа", "GOAL-4", 2, {"Автотестировщик": 16}, 90),
    ]
    people = [
        person("DEV-1", "Антон", "Разработчик", ["Python", "PostgreSQL"], {"Команда Продукт": 1.0}),
        person("QA-01", "Ирина", "Автотестировщик", ["Playwright", "API testing"], {"Команда Продукт": .5}),
        person("DEV-2", "Мария", "Разработчик", ["Python", "Docker"], {"Команда Платформа": 1.0}),
        person("QA-02", "Сергей", "Автотестировщик", ["Playwright", "API testing"], {"Команда Платформа": .5}),
    ]
    deps = [("API-1", "QA-1"), ("QA-1", "REL-1"), ("API-2", "QA-2"), ("CORE-1", "CORE-2")]
    return dict(name="Учебный пример · 4 инициативы", kind="demo", tasks=tasks, engineers=people,
                history=[dict(team=t, date=d, achieved=12, planned=12) for t in ("Команда Продукт", "Команда Платформа") for d in ("2026-09-07", "2026-09-21")],
                dependencies=[dict(first=a, second=b, kind="has to be done before", source_row=None) for a, b in deps],
                issues=[], source={"note": "Синтетические данные, отдельно от официального набора"})
