"""SQLite с неизменяемыми версиями и оптимистической блокировкой."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .importer import InputError


class ConflictError(InputError):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(project TEXT NOT NULL, revision INTEGER NOT NULL, label TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project, revision));
            """)

    @contextmanager
    def connect(self):
        """Соединение на время блока: транзакция и закрытие файла.

        sqlite3 в `with` фиксирует транзакцию, но соединение не закрывает.
        Открытый файл держит базу занятой, и на Windows её нельзя удалить.
        """
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def projects(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id, name, revision FROM projects ORDER BY rowid")]

    def create(self, state):
        pid = uuid.uuid4().hex[:16]
        state = dict(state, project_id=pid, revision=1)
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES(?,?,?)", (pid, state["dataset"]["name"], 1))
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?)", (pid, 1, "Импорт и первый расчёт", datetime.now(timezone.utc).isoformat(), dumps(state)))
        return state

    def get(self, pid, revision=None):
        with self.connect() as db:
            if revision is None:
                row = db.execute("SELECT revision FROM projects WHERE id=?", (pid,)).fetchone()
                if row is None:
                    raise InputError("Проект не найден")
                revision = row["revision"]
            row = db.execute("SELECT payload FROM versions WHERE project=? AND revision=?", (pid, revision)).fetchone()
            if row is None:
                raise InputError("Версия не найдена")
            return json.loads(row["payload"])

    def save(self, state, expected, label):
        if type(expected) is not int:
            raise ConflictError("Не указана исходная версия. Обновите страницу")
        pid = state["project_id"]
        state = dict(state, revision=expected + 1)
        with self.connect() as db:
            updated = db.execute("UPDATE projects SET revision=? WHERE id=? AND revision=?", (expected + 1, pid, expected))
            if updated.rowcount != 1:
                raise ConflictError("Проект изменён в другой вкладке. Обновите страницу и повторите действие")
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?)", (pid, expected + 1, label, datetime.now(timezone.utc).isoformat(), dumps(state)))
        return state

    def versions(self, pid):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT revision,label,created FROM versions WHERE project=? ORDER BY revision DESC", (pid,))]
