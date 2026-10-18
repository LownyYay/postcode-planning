"""ОРБИТА 2: локальный сервер с учётными записями и правами команд."""

import argparse
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import sys
import threading
from collections import defaultdict
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Расчётное ядро живёт в src/ корня репозитория: добавляем его в путь
# до импорта planner, который на него опирается.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner import __version__
from planner.auth import Auth, AccessError, LoginError
from src.dataset import DATASET_PATH
from planner.engine import teams_of
from planner.importer import InputError
from planner.service import Service
from planner.store import ConflictError, Store, dumps
from planner.team_service import TeamService

ROOT = Path(__file__).resolve().parent
MAX_BODY = 2_000_000


def reject_json_constant(value):
    raise InputError('JSON не допускает NaN и Infinity')


def warn_if_insecure_bind(host):
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == 'localhost'
    if not loopback and os.environ.get('ORBIT_COOKIE_SECURE') != '1':
        print('ПРЕДУПРЕЖДЕНИЕ: сервер доступен по сети, cookie без Secure. '
              'Для сетевого доступа используйте HTTPS-прокси и ORBIT_COOKIE_SECURE=1. '
              'Не публикуйте HTTP-порт в интернет напрямую.', file=sys.stderr, flush=True)


def open_browser_when_ready(server, timeout=15):
    """Открываем вкладку только после ответа уже обслуживающего запросы сервера."""
    host = server.server_address[0]
    if host == '0.0.0.0':
        host = '127.0.0.1'
    url = f'http://{host}:{server.server_port}'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = http.client.HTTPConnection(host, server.server_port, timeout=1)
        try:
            conn.request('GET', '/api/health')
            response = conn.getresponse()
            data = response.read()
            if response.status == 200 and json.loads(data).get('status') == 'ok':
                webbrowser.open(url)
                return True
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            pass
        finally:
            conn.close()
        threading.Event().wait(.1)
    print(f'Браузер не открыт: дождитесь готовности и откройте {url} вручную.', flush=True)
    return False


def make_server(host, port, db_path, *, dataset=None, provision=True, jury=False):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    core = Service(store)
    initial = core.initial(dataset or DATASET_PATH)
    project_id = initial['project_id']
    auth = Auth(store)
    service = TeamService(core, auth)
    code_file = Path(db_path).parent / 'team_access.txt'
    jury_file = Path(db_path).parent / 'jury_access.txt'
    # Жюри создаётся до кодов глав: его команда не получает лишний код.
    if jury:
        auth.provision_jury(project_id, initial, jury_file,
                            os.environ.get('ORBIT_JURY_HEAD_PASSWORD'), os.environ.get('ORBIT_JURY_MEMBER_PASSWORD'),
                            os.environ.get('ORBIT_JURY_PLATFORM_PASSWORD'))
    if provision:
        auth.provision(project_id, teams_of(initial['dataset']), code_file)
    # Блокировка на проект, а не на весь сервер: расчёт плана занимает
    # десятки секунд, и общий замок заставлял ждать всех, включая тех,
    # кто работает с другим проектом. Вход и регистрацию сериализует
    # отдельный замок: у них свой счётчик попыток.
    locks = defaultdict(threading.RLock)
    locks_guard = threading.Lock()
    auth_lock = threading.RLock()

    def project_lock(pid):
        with locks_guard:
            return locks[pid]

    secure = os.environ.get('ORBIT_COOKIE_SECURE') == '1'

    class Handler(BaseHTTPRequestHandler):
        server_version = f'Orbita/{__version__}'

        def send(self, value, status=200, mime='application/json; charset=utf-8', headers=None):
            content = value if isinstance(value, bytes) else dumps(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            for key, val in (headers or {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(content)

        def token(self):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
                return cookies['orbita_session'].value if 'orbita_session' in cookies else ''
            except Exception:
                return ''

        def cookie(self, token, age=43200):
            return f'orbita_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('; Secure' if secure else '')

        def session(self):
            return auth.session(self.token())

        def failure(self, exc):
            status = 409 if isinstance(exc, ConflictError) else 401 if isinstance(exc, LoginError) else 403 if isinstance(exc, AccessError) else 400
            self.send({'error': str(exc)}, status)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == '/api/health':
                    return self.send(dict(status='ok', version=__version__))
                allowed = {'/':'index.html', '/index.html':'index.html', '/app.js':'app.js', '/style.css':'style.css', '/icon.svg':'icon.svg'}
                if path in allowed:
                    file = ROOT / 'static' / allowed[path]
                    mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                    return self.send(file.read_bytes(), mime=mime + ('; charset=utf-8' if mime.startswith('text/') or 'javascript' in mime else ''))
                if path == '/api/auth/status':
                    return self.send(dict(teams=teams_of(initial['dataset'])))
                user, csrf = self.session()
                if path == '/api/auth/me':
                    return self.send(dict(user=user, csrf=csrf))
                if parsed.query:
                    raise InputError('Проект определяется учётной записью; параметры адреса не поддерживаются')
                if path == '/api/state':
                    return self.send(service.view(user))
                if path == '/api/report.csv':
                    return self.send(service.report_csv(user), mime='text/csv; charset=utf-8', headers={'Content-Disposition':'attachment; filename="orbita-report.csv"'})
                if path == '/api/facts/template.csv':
                    return self.send(service.fact_csv(user), mime='text/csv; charset=utf-8', headers={'Content-Disposition':'attachment; filename="orbita-facts-template.csv"'})
                return self.send({'error':'Страница не найдена'},404)
            except (InputError, ValueError, TypeError, KeyError) as exc:
                self.failure(exc)
            except Exception:
                import traceback
                traceback.print_exc()
                self.send({'error':'Не удалось загрузить данные. Попробуйте ещё раз'},500)

        def do_POST(self):
            try:
                origin = self.headers.get('Origin')
                if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                    raise AccessError('Запрос должен быть отправлен из интерфейса приложения')
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise AccessError('Межсайтовый запрос запрещён')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    return self.send({'error':'Допустимый размер запроса — до 2 МБ'},413)
                path = urlsplit(self.path).path
                raw = self.rfile.read(length)
                if path == '/api/facts/parse-csv':
                    user, csrf = self.session()
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''), csrf):
                        raise AccessError('Обновите страницу: неверный токен формы')
                    return self.send(dict(report=service.parse_fact_csv(user,raw)))
                if path == '/api/dataset/upload':
                    # Excel приходит как есть; версия и спринт — в заголовках.
                    user, csrf = self.session()
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''), csrf):
                        raise AccessError('Обновите страницу: неверный токен формы')
                    try:
                        revision = int(self.headers.get('X-Revision',''))
                        sprint = self.headers.get('X-Sprint','').strip()
                        sprint = int(sprint) if sprint else None
                    except ValueError:
                        raise InputError('Неверная версия плана или номер спринта') from None
                    with project_lock(user['project_id']):
                        state = service.upload_dataset(user, raw, revision, sprint)
                        return self.send(service.view(user, state))
                media_type = self.headers.get('Content-Type','').split(';',1)[0].strip().lower()
                if media_type != 'application/json':
                    return self.send({'error':'Ожидается JSON-запрос интерфейса'},415)
                body = json.loads(raw, parse_constant=reject_json_constant)
                if not isinstance(body,dict):
                    raise InputError('Неверный формат запроса')
                if path in ('/api/auth/login','/api/auth/register'):
                    with auth_lock:
                        auth.check_attempts(self.client_address[0])
                        user = auth.login(body,project_id) if path.endswith('login') else auth.register(body,project_id)
                        auth.clear_attempts(self.client_address[0])
                        token,csrf = auth.new_session(user)
                        return self.send(dict(user=user,csrf=csrf),headers={'Set-Cookie':self.cookie(token)})
                user,csrf = self.session()
                if path == '/api/explain':
                    # Только чтение, но долгое: замок проекта не берём,
                    # чтобы разбор не задерживал изменения других глав.
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                        raise AccessError('Обновите страницу: неверный токен формы')
                    return self.send(service.explain(user))
                if path in ('/api/scenarios', '/api/whatif/absence'):
                    # Тоже только чтение: расчёт по копии состояния без сохранения.
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                        raise AccessError('Обновите страницу: неверный токен формы')
                    if path == '/api/scenarios':
                        return self.send(service.scenarios(user))
                    return self.send(service.what_if(user, body))
                with project_lock(user['project_id']):
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                        raise AccessError('Обновите страницу: неверный токен формы')
                    if path == '/api/auth/logout':
                        auth.logout(self.token())
                        return self.send({'ok':True},headers={'Set-Cookie':self.cookie('',0)})
                    if path == '/api/invitations':
                        state=service.load(user,body)
                        return self.send(auth.invite(user,state,body.get('engineer_id')))
                    routes={'/api/tasks/update':service.task, '/api/tasks/add':service.add_task,
                            '/api/employees/add':service.add_employee, '/api/employees/availability':service.availability,
                            '/api/requests/create':service.propose, '/api/requests/decide':service.decide, '/api/facts':service.facts,
                            '/api/sprint/close':service.close_sprint,
                            '/api/employees/absence':service.absence, '/api/employees/absence/delete':service.remove_absence}
                    if path in routes:
                        result=routes[path](user,body)
                        return self.send(result if result.get('preview') else service.view(user,result))
                    if path in ('/api/approve','/api/replan'):
                        service.admin(user)
                        if path=='/api/replan' and not user['org_replan']:
                            raise AccessError('Пересчёт прогноза всей организации доступен главе с таким правом. Его выдаёт владелец установки')
                        state=service.load(user,body)
                        if set(body)-{'project_id','revision'}:
                            raise InputError('В этой операции нельзя менять параметры плана')
                        if path=='/api/approve':
                            state=service.approve(user,body)
                        else:
                            service.calculate(state,state['plan'],'Пересчёт по текущим условиям')
                            state=store.save(state,body['revision'],f'Организация: {user["team"]} — пересчитан прогноз ({user["name"]})')
                        return self.send(service.view(user,state))
                    return self.send({'error':'Операция не найдена'},404)
            except (InputError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.failure(exc)
            except Exception:
                import traceback
                traceback.print_exc()
                self.send({'error':'Не удалось выполнить действие. Предыдущая версия сохранена'},500)

    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    server.store, server.auth, server.team_service, server.project_id = store, auth, service, project_id
    server.code_file = code_file
    server.jury_file = jury_file if jury else None
    return server


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='ОРБИТА — планы и команды')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--db',default=str(ROOT/'var'/'orbit.db'))
    parser.add_argument('--open-browser',action='store_true',help='Открыть браузер после готовности сервера')
    parser.add_argument('--grant-org-replan',metavar='ЛОГИН',help='Выдать главе право пересчитывать прогноз всей организации и выйти')
    parser.add_argument('--revoke-org-replan',metavar='ЛОГИН',help='Отозвать это право и выйти')
    parser.add_argument('--no-jury',action='store_true',help='Не создавать учётные записи жюри')
    args=parser.parse_args()
    if args.grant_org_replan or args.revoke_org_replan:
        # Право выдаёт владелец установки, как и коды глав: через командную
        # строку на той же машине, без интерфейса и без запуска сервера.
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        store=Store(args.db)
        project_id=Service(store).initial(DATASET_PATH)['project_id']
        login=args.grant_org_replan or args.revoke_org_replan
        try:
            user=Auth(store).set_org_replan(project_id,login,bool(args.grant_org_replan))
        except InputError as exc:
            sys.exit(f'Не удалось: {exc}')
        state='выдано' if user['org_replan'] else 'отозвано'
        print(f'{user["name"]} ({user["login"]}, {user["team"]}): право пересчёта организации {state}')
        sys.exit(0)
    warn_if_insecure_bind(args.host)
    server=make_server(args.host,args.port,args.db,jury=not args.no_jury)
    print(f'ОРБИТА запущена: http://{args.host}:{server.server_port}',flush=True)
    print(f'Коды регистрации глав команд: {server.code_file}',flush=True)
    print('Откройте файл с кодами и выдайте каждой главе код её команды.',flush=True)
    if server.jury_file and server.jury_file.exists():
        print(f'Учётные записи жюри: {server.jury_file}',flush=True)
    print('Для остановки нажмите Ctrl+C',flush=True)
    try:
        if args.open_browser:
            threading.Thread(target=open_browser_when_ready,args=(server,),daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nИзменения сохранены.')
    finally:
        server.server_close()
