"""Сценарий защиты на официальном датасете.

«Отклонение → каскадный сдвиг → помощь → возврат в квартал» — те же
шаги, что проходит глава в интерфейсе, но одним запуском и с отчётом:

1. Исходный план по датасету и фиксация базы.
2. Факт спринта 1 с отклонениями: DB-202 не начата, SRV-4011 и SRV-4031
   сделаны наполовину. Факт — обновлённый датасет после спринта
   (data/samples/sprint1_deviations.xlsx или сгенерированный заново).
3. Итоговый пересчёт: выполнение спринта, отклонения и их последствия,
   красный, жёлтый и оранжевый риски, причины сдвигов по задачам.
4. Помощь: KP-0000 передаётся из перегруженной Team-Platform в Team-L
   (согласие обеих глав) — и цель PRODF-7120 возвращается в квартал.

    python orbit/scripts/demo_scenario.py
    python orbit/scripts/demo_scenario.py --out demo-report.md

Расчёт идёт во временной базе: рабочая orbit/var/orbit.db не меняется.
"""

import argparse
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))

from planner.importer import import_xlsx  # noqa: E402
from planner.present import quarter_metrics  # noqa: E402
from planner.service import Service  # noqa: E402
from planner.sprint_data import collect_report, merge_dataset  # noqa: E402
from planner.store import Store  # noqa: E402
from planner.team_service import TeamService  # noqa: E402
from src.dataset import DATASET_PATH  # noqa: E402

SAMPLE = ROOT.parent / "data" / "samples" / "sprint1_deviations.xlsx"
DELAY = ("DB-202",)
PARTIAL = {"SRV-4011": 0.5, "SRV-4031": 0.5}
HELP = dict(kind="transfer", task_id="KP-0000", from_team="Team-Platform", to_team="Team-L")

LEVELS = {"red": "Красный", "yellow": "Жёлтый", "orange": "Оранжевый"}


def sprint_dataset(folder):
    """Датасет после спринта 1: образец из data/samples или новый по плану."""
    if SAMPLE.exists():
        return SAMPLE
    from sprint_dataset import simulate, write

    out = Path(folder) / "sprint1.xlsx"
    data, statuses, spent, velocity, day = simulate(DATASET_PATH, 1, set(DELAY), partial=PARTIAL)
    write(DATASET_PATH, out, data, statuses, spent, velocity, day)
    return out


def goals(state):
    rows = state["plan"]["initiatives"]
    return sum(i["forecast_in_quarter"] for i in rows), len(rows)


def risk_lines(state, levels=("red", "yellow", "orange")):
    return [f"- **{LEVELS[r['level']]}.** {r['text']}" for r in state["plan"]["risks"] if r["level"] in levels]


def run(out=None):
    lines = []

    def say(text=""):
        print(text)
        lines.append(text)

    started = time.time()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        core = Service(Store(Path(folder) / "demo.db"))
        teams = TeamService(core, None)

        state = core.initial(DATASET_PATH)
        done, total = goals(state)
        say("# Сценарий защиты: отклонение → каскад → помощь → возврат в квартал")
        say()
        say("## 1. Исходный план")
        say()
        pending = [t for t in state["plan"]["tasks"].values() if t["status"] != "Done"]
        in_quarter = [t for t in pending if state["plan"]["entries"][t["id"]]["status"] == "planned"]
        say(f"Инициатив в квартале: {done} из {total}. Задач: {len(in_quarter)} из {len(pending)}.")
        say()
        state = core.approve(dict(project_id=state["project_id"], revision=state["revision"]))
        say("База сравнения зафиксирована: состав целей и плановые SP спринтов больше не меняются.")
        say()

        path = sprint_dataset(folder)
        fresh = import_xlsx(path, "Обновлённый датасет после спринта 1")
        state["dataset"], _ = merge_dataset(state, fresh)
        report, info = collect_report(state, 1, fresh, {})
        meta = dict(name=fresh["name"], uploaded_by="Сценарий защиты", uploaded_at=datetime.now(timezone.utc).isoformat())
        state = core.actual(dict(project_id=state["project_id"], revision=state["revision"], report=report,
                                 dataset=meta, info=info), state=state)

        metrics = quarter_metrics(state)
        sprint = metrics["sprints"][0]
        review = state["reviews"]["1"]
        say("## 2. Факт спринта 1 и итоговый пересчёт")
        say()
        say(f"Датасет: `{path.name}`. Выполнение плана спринта: {sprint['actual']:.1f} из "
            f"{sprint['planned']:.1f} SP — {sprint['percent']:.1f} %.")
        say(f"Процент квартала: факт {metrics['actual_percent']:.0f} %, прогноз {metrics['forecast_percent']:.0f} % "
            f"({metrics['forecast_done']} из {metrics['goal_count']} исходных целей).")
        say()
        say("Отклонения спринта и их последствия:")
        say()
        for deviation in review["deviations"]:
            text = f"- {deviation['task_id']} ({deviation['team']}): {deviation['text']}"
            followed = review.get("consequences", {}).get(deviation["task_id"], [])
            if followed:
                text += "; повлекло: " + ", ".join(
                    f"{c['task_id']} (С{c['before']} → С{c['after']})" for c in followed)
            say(text)
        if review["goals_lost"]:
            say(f"- Ушли за квартал: {', '.join(review['goals_lost'])}")
        if review.get("capacity"):
            say(f"- Производительность команд: {'; '.join(review['capacity'])}")
        say()
        say("Риски после пересчёта:")
        say()
        for line in risk_lines(state):
            say(line)
        say()
        say("Почему сдвинулись задачи:")
        say()
        for change in state["changes"]:
            if change["kind"] == "schedule":
                before, after = change["before"], change["after"]
                span = (f"начало С{before['start']} → С{after['start']}, завершение С{after['end']}"
                        if before["end"] == after["end"] else f"С{before['end']} → С{after['end']}")
                say(f"- {change['task_id']}: {span}. {change['reason']}")
        say()

        before = {i["id"]: i["forecast_in_quarter"] for i in state["plan"]["initiatives"]}
        candidate, involved = teams.apply_request(state, dict(HELP))
        after = {i["id"]: i["forecast_in_quarter"] for i in candidate["plan"]["initiatives"]}
        back = sorted(i for i, ok in after.items() if ok and not before.get(i))
        done, total = goals(candidate)
        say("## 3. Помощь между командами")
        say()
        say(f"Глава {HELP['from_team']} предлагает передать {HELP['task_id']} в {HELP['to_team']}, "
            f"глава {HELP['to_team']} соглашается (раздел «Согласования»).")
        say()
        say(f"Результат: инициатив в квартале {done} из {total}."
            + (f" Вернулись в квартал: {', '.join(back)}." if back else ""))
        say()
        remaining = risk_lines(candidate, ("red", "yellow"))
        if remaining:
            say("Остаются риски:")
            say()
            for line in remaining:
                say(line)
            say()
        say(f"Расчёт занял {time.time() - started:.0f} с.")

    if out:
        Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {out}")
    return lines


def main():
    parser = argparse.ArgumentParser(description="Сценарий защиты на официальном датасете")
    parser.add_argument("--out", help="Сохранить отчёт в Markdown")
    args = parser.parse_args()
    run(args.out)


if __name__ == "__main__":
    main()
