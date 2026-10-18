"""Регрессии QA: HTTP-контракт, авторизация и организационные операции."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import make_server


# Точный расчёт плана занимает до 35 секунд при изменении состава
# команды. Пересчёт тех же условий берётся из кэша мгновенно.
PLAN_TIMEOUT = 90


class HttpFixture:
    """Один сервер и одна глава; без повторной регистрации шести глав."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.server = make_server('127.0.0.1', 0, Path(cls.temp.name) / 'test.db')
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.codes = dict(line.split(': ', 1) for line in cls.server.code_file.read_text(encoding='utf-8').splitlines() if line.startswith('Team-'))
        cls.user = cls.server.auth.register(dict(login='head-test', name='Глава теста', password='Long-password-42', code=cls.codes['Team-L']), cls.server.project_id)
        # Проверки пересчёта идут от главы, которой выдано право на него.
        cls.user = cls.server.auth.set_org_replan(cls.server.project_id, 'head-test', True)
        cls.seed = cls.server.store.get(cls.server.project_id)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        with self.server.store.connect() as db:
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM users WHERE id != ?', (self.user['id'],))
            db.execute('DELETE FROM login_attempts')
            db.execute("UPDATE invitations SET consumed=0 WHERE role='admin' AND team != 'Team-L'")
        self.token, self.csrf = self.server.auth.new_session(self.user)
        state = self.state()
        self.server.store.save(copy.deepcopy(self.seed), state['revision'], 'Сброс сценария QA')

    def state(self):
        return self.server.store.get(self.server.project_id)

    def body(self, **extra):
        return dict(project_id=self.server.project_id, revision=self.state()['revision'], **extra)

    def call(self, path, body=None, *, raw=None, authenticated=False, headers=None):
        headers = dict(headers or {})
        if authenticated:
            headers.setdefault('Cookie', 'orbita_session=' + self.token)
            headers.setdefault('X-CSRF-Token', self.csrf)
        if body is not None:
            raw = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=PLAN_TIMEOUT)
        try:
            conn.request('POST' if raw is not None else 'GET', path, body=raw, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data), dict(response.getheaders())
        finally:
            conn.close()


class HttpContractTests(HttpFixture, unittest.TestCase):
    def test_json_media_type_rejects_jsonp_and_other_prefixes(self):
        for media in ['application/jsonp', 'application/json-extra', 'text/plain']:
            with self.subTest(media=media):
                self.assertEqual(self.call('/api/replan', self.body(), authenticated=True, headers={'Content-Type': media})[0], 415)

    def test_json_utf8_parameter_is_accepted(self):
        self.assertEqual(self.call('/api/replan', self.body(), authenticated=True, headers={'Content-Type': 'application/json; charset=utf-8'})[0], 200)

    def test_nonfinite_json_is_rejected_before_auth_or_mutation(self):
        revision = self.state()['revision']
        for value in ['NaN', 'Infinity', '-Infinity']:
            status, data, _ = self.call('/api/auth/login', raw=('{"extra": ' + value + '}').encode(), headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 400)
            self.assertIn('JSON', data['error'])
        self.assertEqual(self.state()['revision'], revision)
        with self.server.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0], 0)

    def test_unexpected_get_error_returns_json_and_server_recovers(self):
        with patch.object(self.server.team_service, 'view', side_effect=RuntimeError('private diagnostic')), patch('traceback.print_exc'):
            status, data, headers = self.call('/api/state', authenticated=True)
        self.assertEqual(status, 500)
        self.assertEqual(set(data), {'error'})
        self.assertNotIn('private diagnostic', data['error'])
        self.assertIn('application/json', headers['Content-Type'])
        self.assertEqual(self.call('/api/state', authenticated=True)[0], 200)


class LoginLimitTests(unittest.TestCase):
    def setUp(self):
        from planner.auth import Auth
        from planner.store import Store
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.auth = Auth(Store(Path(self.temp.name) / 'auth.db'))

    def test_login_attempt_limit_without_http_scrypt(self):
        from planner.auth import LoginError
        for _ in range(12):
            self.auth.check_attempts('client')
        with self.assertRaises(LoginError):
            self.auth.check_attempts('client')
        self.auth.check_attempts('other-client')
        self.auth.clear_attempts('client')
        self.auth.check_attempts('client')

    def test_concurrent_attempts_allow_exactly_twelve(self):
        from concurrent.futures import ThreadPoolExecutor
        from planner.auth import LoginError
        barrier = threading.Barrier(20)
        def attempt(_):
            barrier.wait(timeout=5)
            try:
                self.auth.check_attempts('same-client')
                return True
            except LoginError:
                return False
        with ThreadPoolExecutor(max_workers=20) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 12)
        with self.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0], 12)

    def test_expired_attempts_do_not_consume_limit(self):
        with patch('planner.auth.time.time', return_value=1000):
            for _ in range(12):
                self.auth.check_attempts('client')
        with patch('planner.auth.time.time', return_value=1901):
            self.auth.check_attempts('client')
        with self.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0], 1)


class BrowserStartupTests(unittest.TestCase):
    def test_browser_opens_only_after_successful_health_response(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app import open_browser_when_ready
        server = SimpleNamespace(server_address=('0.0.0.0', 8000), server_port=8000)
        conn = Mock()
        with patch('app.webbrowser.open') as browser, patch('app.http.client.HTTPConnection', return_value=conn):
            def response():
                browser.assert_not_called()
                if conn.request.call_count == 1:
                    raise OSError('ещё не готов')
                return SimpleNamespace(status=200, read=lambda: b'{"status":"ok"}')
            conn.getresponse.side_effect = response
            self.assertTrue(open_browser_when_ready(server))
            browser.assert_called_once_with('http://127.0.0.1:8000')
        self.assertEqual(conn.close.call_count, 2)

    def test_browser_is_not_opened_if_health_never_becomes_ready(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app import open_browser_when_ready
        server = SimpleNamespace(server_address=('127.0.0.1', 8000), server_port=8000)
        conn = Mock()
        conn.getresponse.return_value = SimpleNamespace(status=503, read=lambda: b'{}')
        with patch('app.webbrowser.open') as browser, patch('app.http.client.HTTPConnection', return_value=conn), patch('app.time.monotonic', side_effect=[0, 0, 16]), patch('builtins.print'):
            self.assertFalse(open_browser_when_ready(server))
            browser.assert_not_called()
        conn.close.assert_called_once()


class SecureCookieTests(HttpFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict('os.environ', {'ORBIT_COOKIE_SECURE': '1'}):
            super().setUpClass()

    def test_secure_cookie_on_login_and_logout(self):
        status, data, headers = self.call('/api/auth/login', dict(login='head-test', password='Long-password-42'))
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.token = headers['Set-Cookie'].split(';')[0].split('=', 1)[1]
        self.csrf = data['csrf']
        status, _, headers = self.call('/api/auth/logout', {}, authenticated=True)
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('Max-Age=0', headers['Set-Cookie'])


class NetworkStartupTests(unittest.TestCase):
    def test_nonloopback_without_secure_cookie_warns(self):
        from app import warn_if_insecure_bind
        for host in ['0.0.0.0', '192.168.1.2', 'orbita.example']:
            with self.subTest(host=host), patch.dict('os.environ', {'ORBIT_COOKIE_SECURE': '0'}), patch('builtins.print') as output:
                warn_if_insecure_bind(host)
                self.assertIn('ПРЕДУПРЕЖДЕНИЕ', output.call_args.args[0])
                self.assertIn('HTTPS', output.call_args.args[0])

    def test_loopback_or_secure_cookie_does_not_warn(self):
        from app import warn_if_insecure_bind
        with patch.dict('os.environ', {'ORBIT_COOKIE_SECURE': '0'}), patch('builtins.print') as output:
            for host in ['127.0.0.1', '127.0.0.2', 'localhost', '::1']:
                warn_if_insecure_bind(host)
            output.assert_not_called()
        with patch.dict('os.environ', {'ORBIT_COOKIE_SECURE': '1'}), patch('builtins.print') as output:
            warn_if_insecure_bind('0.0.0.0')
            output.assert_not_called()


class CollectiveApprovalTests(HttpFixture, unittest.TestCase):
    def register_head(self, team):
        user = self.server.auth.register(dict(login='head-' + team.casefold(), name='Глава ' + team, password='Long-password-42', code=self.codes[team]), self.server.project_id)
        token, csrf = self.server.auth.new_session(user)
        return {'Cookie': 'orbita_session=' + token, 'X-CSRF-Token': csrf}

    def approve(self, headers=None):
        return self.call('/api/approve', self.body(), authenticated=True, headers=headers)

    def test_single_registered_head_can_fix_baseline(self):
        before = self.state()['revision']
        status, value, _ = self.approve()
        self.assertEqual(status, 200)
        self.assertTrue(value['baseline'])
        self.assertEqual(self.state()['revision'], before + 1)
        self.assertEqual(len(self.state()['baseline_approval']['approvals']), 1)

    def test_six_heads_must_all_agree_and_final_save_is_atomic(self):
        heads = [None] + [self.register_head(team) for team in self.codes if team != 'Team-L']
        for index, headers in enumerate(heads):
            before = self.state()['revision']
            status, value, _ = self.approve(headers)
            self.assertEqual(status, 200, value)
            self.assertEqual(value['baseline'], index == 5)
            self.assertEqual(self.state()['revision'], before + 1)
            self.assertEqual(len(value['teams']), 6)
            if index < 5:
                self.assertEqual(value['baseline_approval']['confirmed'], index + 1)
                self.assertEqual(value['baseline_approval']['required'], 6)
        baseline = self.state()['baseline']
        self.assertEqual(len(self.state()['baseline_approval']['approvals']), 6)
        self.assertEqual(self.approve()[0], 400)
        self.assertEqual(self.state()['baseline'], baseline)

    def test_repeat_consent_does_not_add_vote_or_version(self):
        self.register_head('Team-Alpha')
        self.approve()
        revision = self.state()['revision']
        status, value, _ = self.approve()
        self.assertEqual(status, 200)
        self.assertFalse(value['baseline'])
        self.assertEqual(value['baseline_approval']['confirmed'], 1)
        self.assertTrue(value['baseline_approval']['own_approved'])
        self.assertEqual(value['revision'], revision)

    def test_plan_change_invalidates_previous_consents(self):
        peer = self.register_head('Team-Alpha')
        self.approve()
        self.assertEqual(self.call('/api/tasks/update', self.body(task_id='ONK-2914', changes={'priority': 123}), authenticated=True)[0], 200)
        progress = self.call('/api/state', authenticated=True)[1]['baseline_approval']
        self.assertTrue(progress['stale'])
        self.assertEqual(progress['confirmed'], 0)
        status, value, _ = self.approve(peer)
        self.assertEqual(status, 200)
        self.assertFalse(value['baseline'])
        self.assertEqual(value['baseline_approval']['confirmed'], 1)
        self.assertTrue(self.approve()[1]['baseline'])
        self.assertEqual(self.state()['baseline']['plan']['tasks']['ONK-2914']['priority'], 123)

    def test_new_head_invalidates_previous_round(self):
        peer = self.register_head('Team-Alpha')
        self.approve()
        third = self.register_head('Team-K')
        progress = self.call('/api/state', authenticated=True)[1]['baseline_approval']
        self.assertTrue(progress['stale'])
        self.assertEqual(progress['required'], 3)
        self.assertFalse(self.approve(peer)[1]['baseline'])
        self.assertFalse(self.approve(third)[1]['baseline'])
        self.assertTrue(self.approve()[1]['baseline'])

    def test_replan_keeps_consents_and_is_logged_for_organization(self):
        self.register_head('Team-Alpha')
        self.approve()
        plan = self.state()['plan']
        status, value, _ = self.call('/api/replan', self.body(), authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.state()['plan'], plan)
        self.assertEqual(value['baseline_approval']['confirmed'], 1)
        self.assertIn('Организация', value['versions'][0]['label'])
        self.assertIn('Team-L', value['versions'][0]['label'])
        self.assertEqual({t['team'] for t in value['tasks']}, set(self.codes))
        self.assertEqual(self.call('/api/replan', self.body(config={}), authenticated=True)[0], 400)

    def test_failed_final_save_does_not_leave_partial_baseline(self):
        from planner.store import ConflictError
        before = self.state()['revision']
        with patch.object(self.server.store, 'save', side_effect=ConflictError('Конфликт версии')):
            self.assertEqual(self.approve()[0], 409)
        state = self.state()
        self.assertIsNone(state['baseline'])
        self.assertNotIn('baseline_approval', state)
        self.assertEqual(state['revision'], before)
        self.assertTrue(self.approve()[1]['baseline'])


class SessionTests(HttpFixture, unittest.TestCase):
    def test_second_login_invalidates_previous_session(self):
        old_token, old_csrf = self.token, self.csrf
        status, value, headers = self.call('/api/auth/login', dict(login='head-test', password='Long-password-42'))
        self.assertEqual(status, 200)
        self.assertEqual(self.call('/api/state', authenticated=True)[0], 401)
        self.assertEqual(self.call('/api/replan', self.body(), authenticated=True)[0], 401)
        self.token = headers['Set-Cookie'].split(';')[0].split('=', 1)[1]
        self.csrf = value['csrf']
        self.assertNotEqual(self.token, old_token)
        self.assertNotEqual(self.csrf, old_csrf)
        self.assertEqual(self.call('/api/state', authenticated=True)[0], 200)
        self.assertEqual(self.call('/api/replan', self.body(), authenticated=True, headers={'X-CSRF-Token': old_csrf})[0], 403)

    def test_failed_login_preserves_current_session(self):
        self.assertEqual(self.call('/api/auth/login', dict(login='head-test', password='wrong-password'))[0], 401)
        self.assertEqual(self.call('/api/state', authenticated=True)[0], 200)

    def test_concurrent_new_sessions_leave_only_one_valid_token(self):
        from concurrent.futures import ThreadPoolExecutor
        from planner.auth import LoginError
        barrier = threading.Barrier(8)
        def create(_):
            barrier.wait(timeout=5)
            return self.server.auth.new_session(self.user)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(create, range(8)))
        valid = []
        for token in tokens:
            try:
                self.server.auth.session(token)
                valid.append(token)
            except LoginError:
                pass
        self.assertEqual(len(valid), 1)
        with self.server.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (self.user['id'],)).fetchone()[0], 1)


class PublicAuthTests(HttpFixture, unittest.TestCase):
    def test_health_auth_status_and_me(self):
        status, value, _ = self.call('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(value['status'], 'ok')
        self.assertIn('version', value)
        status, before, _ = self.call('/api/auth/status')
        self.assertEqual(status, 200)
        self.assertEqual(set(before), {'teams'})
        self.assertTrue(all(isinstance(team, str) for team in before['teams']))
        self.assertEqual(set(before['teams']), set(self.codes))
        self.server.auth.register(dict(login='another-head', name='Вторая глава', password='Long-password-42', code=self.codes['Team-Alpha']), self.server.project_id)
        self.assertEqual(self.call('/api/auth/status')[1], before)
        self.assertEqual(self.call('/api/auth/me')[0], 401)
        status, me, _ = self.call('/api/auth/me', authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(me['user']['id'], self.user['id'])
        self.assertEqual(me['csrf'], self.csrf)
        self.assertNotIn('password', me['user'])
        self.assertNotIn('salt', me['user'])


class PasswordPolicyTests(HttpFixture, unittest.TestCase):
    def register(self, password, login='matching-login'):
        return self.call('/api/auth/register', dict(login=login, name='Новая глава', password=password, code=self.codes['Team-Alpha'], role='admin'))

    def test_password_minimum_and_maximum_are_enforced(self):
        for password in ['Short-42', 'a' * 257, 1234567890]:
            with self.subTest(password_type=type(password).__name__):
                self.assertEqual(self.register(password)[0], 400)

    def test_numeric_password_is_rejected(self):
        for password in ['1234567890', ' 1234567890 ', '١٢٣٤٥٦٧٨٩٠']:
            self.assertEqual(self.register(password)[0], 400)
        # Отклонённые пароли не расходуют одноразовое приглашение.
        self.assertEqual(self.register('Long-password-42')[0], 200)

    def test_password_matching_login_is_rejected(self):
        for password in ['matching-login', 'MATCHING-LOGIN', ' matching-login ']:
            self.assertEqual(self.register(password)[0], 400)

    def test_normal_password_registers_with_invitation_team(self):
        status, value, headers = self.register('Long-password-42')
        self.assertEqual(status, 200)
        self.assertEqual(value['user']['team'], 'Team-Alpha')
        self.assertEqual(value['user']['role'], 'admin')
        self.assertIn('HttpOnly', headers['Set-Cookie'])


class OrgReplanMigrationTests(unittest.TestCase):
    def test_old_database_gets_the_column_and_keeps_accounts(self):
        """База, созданная до появления права, открывается без потери учёток."""
        import sqlite3
        from planner.auth import Auth
        from planner.store import Store
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / 'old.db'
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE users(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, login TEXT NOT NULL,
                  name TEXT NOT NULL, salt TEXT NOT NULL, password TEXT NOT NULL,
                  role TEXT NOT NULL, team TEXT NOT NULL, engineer_id TEXT,
                  UNIQUE(project_id, login));
                INSERT INTO users VALUES('u1','p1','old-head','Глава','00','x','admin','Team-L',NULL);
            ''')
            db.commit()
            db.close()
            auth = Auth(Store(path))
            user = auth.set_org_replan('p1', 'old-head', True)
            self.assertTrue(user['org_replan'])
            self.assertEqual(user['name'], 'Глава')


class InvitationFileTests(unittest.TestCase):
    def test_provision_is_utf8_private_and_not_duplicated(self):
        import os
        import stat
        from planner.auth import Auth
        from planner.store import Store
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            auth = Auth(Store(Path(folder) / 'auth.db'))
            filename = Path(folder) / 'team_access.txt'
            # У уже существовавшего файла также сужаются POSIX-права.
            filename.write_text('Существующий текст\n', encoding='utf-8')
            if os.name == 'posix':
                filename.chmod(0o644)
            issued = auth.provision('project', ['Команда Проверка'], filename)
            content = filename.read_bytes().decode('utf-8')
            self.assertIn('Команда Проверка: ' + issued[0][1], content)
            self.assertTrue(content.startswith('Существующий текст'))
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)
            self.assertEqual(auth.provision('project', ['Команда Проверка'], filename), [])
            self.assertEqual(filename.read_bytes().decode('utf-8'), content)


class JuryAccountTests(unittest.TestCase):
    """Готовые учётные записи жюри: две главы команд и сотрудник."""

    def test_jury_accounts_are_created_once_and_can_log_in(self):
        import os
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            os.environ['ORBIT_JURY_HEAD_PASSWORD'] = 'Jury-head-password'
            try:
                server = make_server('127.0.0.1', 0, Path(folder) / 'jury.db', jury=True)
            finally:
                os.environ.pop('ORBIT_JURY_HEAD_PASSWORD', None)
            try:
                text = server.jury_file.read_text(encoding='utf-8')
                self.assertIn('jury-head', text)
                self.assertIn('jury-member', text)
                self.assertNotIn('Team-L:', server.code_file.read_text(encoding='utf-8'))
                head = server.auth.login(dict(login='jury-head', password='Jury-head-password'), server.project_id)
                self.assertEqual((head['role'], head['team'], head['org_replan']), ('admin', 'Team-L', True))
                password = next(line.split(': ', 1)[1] for line in text.splitlines()[text.splitlines().index('  логин: jury-member') + 1:] if 'пароль' in line)
                member = server.auth.login(dict(login='jury-member', password=password), server.project_id)
                self.assertEqual((member['role'], member['team']), ('member', 'Team-L'))
                self.assertTrue(member['engineer_id'])
                # Глава второй команды — для согласований между командами.
                self.assertIn('jury-platform', text)
                self.assertNotIn('Team-Platform:', server.code_file.read_text(encoding='utf-8'))
                password = next(line.split(': ', 1)[1] for line in text.splitlines()[text.splitlines().index('  логин: jury-platform') + 1:] if 'пароль' in line)
                second = server.auth.login(dict(login='jury-platform', password=password), server.project_id)
                self.assertEqual((second['role'], second['team'], second['org_replan']), ('admin', 'Team-Platform', False))
            finally:
                server.server_close()
            again = make_server('127.0.0.1', 0, Path(folder) / 'jury.db', jury=True)
            try:
                self.assertEqual(again.jury_file.read_text(encoding='utf-8'), text)
            finally:
                again.server_close()
