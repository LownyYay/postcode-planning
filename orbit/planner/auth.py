"""Приглашения, пароли и серверные сессии. Роль задаётся приглашением."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from .importer import InputError


class AccessError(InputError):
    pass


class LoginError(AccessError):
    pass


# Учётные записи жюри: глава команды, её сотрудник и глава второй команды —
# с ним проходят согласования между командами (передача задачи, помощь).
# Логины постоянные; пароли — из переменных окружения, иначе случайные.
JURY_HEAD_LOGIN = 'jury-head'
JURY_MEMBER_LOGIN = 'jury-member'
JURY_SECOND_LOGIN = 'jury-platform'
JURY_TEAM = 'Team-L'
JURY_SECOND_TEAM = 'Team-Platform'


def _secret_file(filename):
    """Файл с секретами, закрытый для других пользователей на POSIX."""
    file = Path(filename)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    output = os.fdopen(fd, 'w', encoding='utf-8')
    if os.name == 'posix':
        os.fchmod(output.fileno(), 0o600)
    return output


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768,
                          r=8, p=1, maxmem=64 * 1024 * 1024).hex()


def public_user(row):
    user = {k: row[k] for k in ('id', 'project_id', 'login', 'name', 'role', 'team', 'engineer_id')}
    # Право пересчитывать прогноз всей организации: пересчёт затрагивает
    # все команды, поэтому одной роли главы для него недостаточно.
    user['org_replan'] = bool(row['org_replan'])
    return user


class Auth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, login TEXT NOT NULL,
                  name TEXT NOT NULL, salt TEXT NOT NULL, password TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('admin','member')), team TEXT NOT NULL,
                  engineer_id TEXT, org_replan INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(project_id, login));
                CREATE UNIQUE INDEX IF NOT EXISTS one_head_per_team
                  ON users(project_id,team) WHERE role='admin';
                CREATE UNIQUE INDEX IF NOT EXISTS one_employee_account
                  ON users(project_id,engineer_id) WHERE engineer_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS invitations(
                  token TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL,
                  team TEXT NOT NULL, engineer_id TEXT, expires REAL NOT NULL,
                  consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions(
                  token TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts(
                  identity TEXT NOT NULL, attempted REAL NOT NULL);
            ''')
            # База, созданная до появления права, получает столбец без потери учёток.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'org_replan' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN org_replan INTEGER NOT NULL DEFAULT 0')

    def provision(self, project_id, teams, filename):
        """Коды глав доступны только владельцу локальной установки."""
        rows = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for team in teams:
                if db.execute("SELECT 1 FROM users WHERE project_id=? AND team=? AND role='admin'", (project_id, team)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM invitations WHERE project_id=? AND team=? AND role='admin' AND consumed=0 AND expires>?", (project_id, team, time.time())).fetchone():
                    continue
                token = secrets.token_urlsafe(24)
                db.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,0)',
                           (digest(token), project_id, 'admin', team, None, time.time() + 30 * 86400))
                rows.append((team, token))
            if rows:
                file = Path(filename)
                file.parent.mkdir(parents=True, exist_ok=True)
                # На POSIX создаём закрытый файл сразу, до записи секретов.
                # Windows наследует ACL каталога: chmod не заменяет ACL.
                fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as output:
                    if os.name == 'posix':
                        os.fchmod(output.fileno(), 0o600)
                    output.write('\nОРБИТА — коды регистрации глав команд\n')
                    output.write('Каждый код одноразовый. Передайте его только главе указанной команды.\n')
                    for team, token in rows:
                        output.write(f'{team}: {token}\n')
        return rows

    def invite(self, user, state, engineer_id):
        if user['role'] != 'admin':
            raise AccessError('Приглашать сотрудников может только глава команды')
        person = next((p for p in state['dataset']['engineers'] if p['id'] == engineer_id), None)
        if not person or person['allocations'].get(user['team'], 0) <= 0:
            raise AccessError('Можно пригласить сотрудника своей команды')
        token = secrets.token_urlsafe(24)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM users WHERE project_id=? AND engineer_id=?', (state['project_id'], engineer_id)).fetchone():
                raise InputError('У сотрудника уже есть учётная запись')
            db.execute("UPDATE invitations SET consumed=1 WHERE project_id=? AND engineer_id=? AND role='member'", (state['project_id'], engineer_id))
            db.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,0)',
                       (digest(token), state['project_id'], 'member', user['team'], engineer_id, time.time() + 7 * 86400))
        return dict(code=token, engineer_id=engineer_id, team=user['team'], expires_days=7)

    def register(self, body, project_id):
        login = str(body.get('login', '')).strip().casefold()
        name = str(body.get('name', '')).strip()
        password = body.get('password', '')
        code = str(body.get('code', '')).strip()
        if not re.fullmatch(r'[a-z0-9_.@+\-]{3,80}', login):
            raise InputError('Логин: 3–80 латинских букв, цифр или символов . _ @ + -')
        if not name or len(name) > 100:
            raise InputError('Укажите имя длиной до 100 символов')
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise InputError('Пароль должен содержать от 10 до 256 символов')
        if password.strip().isdigit() or password.strip().casefold() == login:
            raise InputError('Пароль не должен состоять только из цифр или совпадать с логином')
        if len(code) > 200:
            raise InputError('Неверный код приглашения')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                invite = db.execute('SELECT * FROM invitations WHERE token=? AND project_id=? AND consumed=0 AND expires>?',
                                    (digest(code), project_id, time.time())).fetchone()
                if invite is None:
                    raise InputError('Код приглашения неверен, использован или истёк')
                if body.get('role', invite['role']) != invite['role']:
                    raise InputError('Код предназначен для другой роли')
                uid = uuid.uuid4().hex
                # Столбцы названы явно: право пересчёта по умолчанию не выдаётся.
                db.execute('INSERT INTO users(id,project_id,login,name,salt,password,role,team,engineer_id) VALUES(?,?,?,?,?,?,?,?,?)',
                           (uid, project_id, login, name, salt, hashed, invite['role'], invite['team'], invite['engineer_id']))
                db.execute('UPDATE invitations SET consumed=1 WHERE token=?', (digest(code),))
                return public_user(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
        except sqlite3.IntegrityError:
            raise InputError('Логин занят, сотрудник зарегистрирован или у команды уже есть глава') from None

    def check_attempts(self, identity):
        with self.store.connect() as db:
            # Резервируем запись до COUNT: параллельные проверки видят уже
            # зафиксированные попытки и не обходят общий порог 12.
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute('DELETE FROM login_attempts WHERE attempted<?', (now - 900,))
            count = db.execute('SELECT COUNT(*) FROM login_attempts WHERE identity=?', (identity,)).fetchone()[0]
            if count >= 12:
                raise LoginError('Слишком много попыток входа. Попробуйте через 15 минут')
            db.execute('INSERT INTO login_attempts VALUES(?,?)', (identity, now))

    def login(self, body, project_id):
        login = str(body.get('login', '')).strip().casefold()
        password = body.get('password', '')
        if not isinstance(password, str) or len(password) > 256:
            raise LoginError('Неверный логин или пароль')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM users WHERE project_id=? AND login=?', (project_id, login)).fetchone()
        salt = row['salt'] if row else '00' * 16
        candidate = password_hash(password, salt)
        if row is None or not hmac.compare_digest(row['password'], candidate):
            raise LoginError('Неверный логин или пароль')
        return public_user(row)

    def clear_attempts(self, identity):
        with self.store.connect() as db:
            db.execute('DELETE FROM login_attempts WHERE identity=?', (identity,))

    def new_session(self, user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sessions WHERE user_id=? OR expires<?', (user['id'], time.time()))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token), user['id'], csrf, time.time() + 12 * 3600))
        return token, csrf

    def session(self, token):
        if not token or len(token) > 200:
            raise LoginError('Войдите в учётную запись')
        with self.store.connect() as db:
            row = db.execute('SELECT users.*, sessions.csrf FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=? AND sessions.expires>?',
                             (digest(token), time.time())).fetchone()
        if row is None:
            raise LoginError('Сессия истекла. Войдите ещё раз')
        return public_user(row), row['csrf']

    def logout(self, token):
        with self.store.connect() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (digest(token),))

    def set_org_replan(self, project_id, login, allowed):
        """Выдать или отозвать право пересчитывать прогноз всей организации.

        Право выдаёт владелец установки, как и коды глав. Получить его
        может только глава: пересчёт доступен лишь главам команд.
        """
        login = str(login or '').strip().casefold()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE project_id=? AND login=?', (project_id, login)).fetchone()
            if row is None:
                raise InputError(f'Учётная запись {login} не найдена')
            if row['role'] != 'admin':
                raise InputError('Право пересчёта организации выдаётся только главе команды')
            db.execute('UPDATE users SET org_replan=? WHERE id=?', (1 if allowed else 0, row['id']))
            row = db.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
        return public_user(row)

    def provision_jury(self, project_id, state, filename, head_password=None, member_password=None,
                       second_password=None):
        """Готовые учётные записи для жюри: глава команды, её сотрудник
        и глава второй команды для согласований между командами.

        Первая глава получает право пересчёта организации, чтобы жюри видело
        все возможности. Создаются один раз: повторный запуск их не трогает.
        Неиспользованный код главы этих команд гасится — у команды одна глава.
        Логины и пароли записываются в filename; пароль из аргумента
        (переменной окружения) имеет приоритет над случайным.
        """
        created = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {row['login'] for row in db.execute(
                'SELECT login FROM users WHERE project_id=? AND login IN (?,?,?)',
                (project_id, JURY_HEAD_LOGIN, JURY_MEMBER_LOGIN, JURY_SECOND_LOGIN))}
            if existing == {JURY_HEAD_LOGIN, JURY_MEMBER_LOGIN, JURY_SECOND_LOGIN}:
                return []
            teams = sorted({team for p in state['dataset']['engineers'] for team in p['allocations']})
            headed = {row['team'] for row in db.execute(
                "SELECT team FROM users WHERE project_id=? AND role='admin'", (project_id,))}
            team = JURY_TEAM if JURY_TEAM not in headed or JURY_HEAD_LOGIN in existing else next(
                (t for t in teams if t not in headed), None)
            if JURY_HEAD_LOGIN not in existing:
                if team is None:
                    raise InputError('У всех команд уже есть главы: аккаунт жюри некуда добавить')
                password = head_password or secrets.token_urlsafe(12)
                self._insert(db, project_id, JURY_HEAD_LOGIN, f'Жюри — глава {team}', password, 'admin', team, None, org_replan=1)
                db.execute("UPDATE invitations SET consumed=1 WHERE project_id=? AND team=? AND role='admin'", (project_id, team))
                created.append((JURY_HEAD_LOGIN, password, f'глава команды {team}'))
            else:
                team = db.execute('SELECT team FROM users WHERE project_id=? AND login=?', (project_id, JURY_HEAD_LOGIN)).fetchone()['team']
            if JURY_MEMBER_LOGIN not in existing:
                taken = {row['engineer_id'] for row in db.execute(
                    'SELECT engineer_id FROM users WHERE project_id=? AND engineer_id IS NOT NULL', (project_id,))}
                person = next((p for p in sorted(state['dataset']['engineers'], key=lambda p: p['id'])
                               if p['allocations'].get(team, 0) > 0 and p['id'] not in taken), None)
                if person is None:
                    raise InputError(f'В {team} нет сотрудника без учётной записи для жюри')
                password = member_password or secrets.token_urlsafe(12)
                self._insert(db, project_id, JURY_MEMBER_LOGIN, f'Жюри — сотрудник {person["id"]}', password, 'member', team, person['id'])
                db.execute("UPDATE invitations SET consumed=1 WHERE project_id=? AND engineer_id=?", (project_id, person['id']))
                created.append((JURY_MEMBER_LOGIN, password, f'сотрудник {person["id"]} ({", ".join(person["roles"])}), команда {team}'))
            if JURY_SECOND_LOGIN not in existing:
                headed = {row['team'] for row in db.execute(
                    "SELECT team FROM users WHERE project_id=? AND role='admin'", (project_id,))}
                second = JURY_SECOND_TEAM if JURY_SECOND_TEAM not in headed else next(
                    (t for t in teams if t not in headed), None)
                if second is not None:
                    password = second_password or secrets.token_urlsafe(12)
                    self._insert(db, project_id, JURY_SECOND_LOGIN, f'Жюри — глава {second}', password, 'admin', second, None)
                    db.execute("UPDATE invitations SET consumed=1 WHERE project_id=? AND team=? AND role='admin'", (project_id, second))
                    created.append((JURY_SECOND_LOGIN, password, f'глава команды {second} — для согласований между командами'))
        if created:
            with _secret_file(filename) as output:
                output.write('ОРБИТА — учётные записи жюри\n')
                output.write('Пароль показан один раз при создании; в базе хранится только хэш.\n\n')
                for login, password, who in created:
                    output.write(f'{who}\n  логин: {login}\n  пароль: {password}\n\n')
        return created

    def _insert(self, db, project_id, login, name, password, role, team, engineer_id, org_replan=0):
        if not 10 <= len(password) <= 256 or password.strip().isdigit() or password.strip().casefold() == login:
            raise InputError(f'Пароль для {login}: от 10 до 256 символов, не только цифры и не равен логину')
        salt = secrets.token_hex(16)
        db.execute('INSERT INTO users(id,project_id,login,name,salt,password,role,team,engineer_id,org_replan) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, project_id, login, name, salt, password_hash(password, salt), role, team, engineer_id, org_replan))

    def team_accounts(self, project_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,role,team,engineer_id FROM users WHERE project_id=?', (project_id,))]
