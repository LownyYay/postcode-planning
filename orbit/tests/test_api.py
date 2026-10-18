"""HTTP-проверки прав и полных рабочих процессов Орбиты 2."""
import copy
import csv
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import make_server
from planner.auth import InputError


# Точный расчёт плана занимает до 35 секунд при изменении состава
# команды. Пересчёт тех же условий берётся из кэша мгновенно.
PLAN_TIMEOUT = 90


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.server=make_server('127.0.0.1',0,Path(cls.temp.name)/'api.db')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.codes={line.split(': ',1)[0]:line.split(': ',1)[1] for line in cls.server.code_file.read_text(encoding='utf-8').splitlines() if ': ' in line and line.startswith('Team-')}
        cls.admins={}
        for i,(team,code) in enumerate(cls.codes.items()):
            user=cls.server.auth.register(dict(login=f'head{i}',name=f'Глава {team}',password='Long-password-42',code=code,role='admin'),cls.server.project_id)
            token,csrf=cls.server.auth.new_session(user)
            cls.admins[team]=dict(user=user,token=token,csrf=csrf)
        cls.admin=cls.admins['Team-L']
        # Пересчёт организации проверяется от главы Team-L, у остальных права нет.
        cls.admin['user']=cls.server.auth.set_org_replan(cls.server.project_id,cls.admin['user']['login'],True)
        state=cls.server.store.get(cls.server.project_id)
        invite=cls.server.auth.invite(cls.admin['user'],state,'ENG-401')
        member=cls.server.auth.register(dict(login='member',name='Сотрудник',password='Long-password-42',code=invite['code'],role='member'),cls.server.project_id)
        token,csrf=cls.server.auth.new_session(member)
        cls.member=dict(user=member,token=token,csrf=csrf)
        cls.seed=copy.deepcopy(state)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2);cls.temp.cleanup()

    def setUp(self):
        # Новый вход отзывает старую cookie; у каждого сценария свои сессии.
        for session in [*self.admins.values(), self.member]:
            session['token'], session['csrf'] = self.server.auth.new_session(session['user'])
        s=self.server.store.get(self.server.project_id)
        fresh=copy.deepcopy(self.seed)
        fresh['revision']=s['revision']
        self.server.store.save(fresh,s['revision'],'Сброс тестового сценария')

    def call(self,path,body=None,session=None,raw=None,headers=None):
        headers=dict(headers or {})
        if session:
            headers.setdefault('Cookie','orbita_session='+session['token'])
            headers.setdefault('X-CSRF-Token',session['csrf'])
        if body is not None:
            raw=json.dumps(body).encode();headers.setdefault('Content-Type','application/json')
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=PLAN_TIMEOUT)
        conn.request('POST' if raw is not None else 'GET',path,body=raw,headers=headers)
        response=conn.getresponse();data=response.read();status=response.status
        response_headers=dict(response.getheaders())
        mime=response.getheader('Content-Type','');conn.close()
        return status,json.loads(data) if 'application/json' in mime else data,response_headers

    def body(self,**kwargs):
        s=self.server.store.get(self.server.project_id)
        return {'project_id':self.server.project_id,'revision':s['revision'],**kwargs}

    def state(self):return self.server.store.get(self.server.project_id)

    def approve_baseline(self):
        for session in self.admins.values():
            code, value, _ = self.call('/api/approve', self.body(), session)
            self.assertEqual(code, 200, value)
        self.assertTrue(self.state()['baseline'])

    def report(self,team):
        s=self.state()
        # Нулевой остаток у незавершённой задачи не указывают: его посчитает правило ядра.
        return dict(sprint=s['plan']['cutoff']+1,achieved_sp=0,note='',tasks=[dict(task_id=t['id'],status=t['status'],remaining_sp=t['remaining_sp'] or None,remaining_hours=copy.deepcopy(t['remaining_hours']) or None,spent_hours={}) for t in s['plan']['tasks'].values() if t['team']==team and t['status']!='Done'])

    def test_public_assets_and_authentication_boundary(self):
        for path in ['/','/app.js','/style.css','/icon.svg','/api/health']:
            self.assertEqual(self.call(path)[0],200,path)
        for path in ['/api/state','/api/report.csv','/api/facts/template.csv']:
            self.assertEqual(self.call(path)[0],401,path)
        self.assertEqual(self.call('/api/state?project=other',session=self.admin)[0],400)
        for path in ['/api/plan','/api/actuals','/api/scenario/accept','/api/import']:
            self.assertEqual(self.call(path,self.body(),self.admin)[0],404,path)
        self.assertEqual(self.call('/../var/orbit.db',session=self.admin)[0],404)

    def test_login_cookie_csrf_logout_and_password_hash(self):
        code,value,headers=self.call('/api/auth/login',dict(login='head'+str(list(self.codes).index('Team-L')),password='Long-password-42'))
        self.assertEqual(code,200)
        cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        token=cookie.split(';')[0].split('=',1)[1]
        session=dict(token=token,csrf=value['csrf'])
        self.assertEqual(self.call('/api/replan',self.body(),session,headers={'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.call('/api/replan',self.body(),session,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.call('/api/auth/logout',{},session)[0],200)
        self.assertEqual(self.call('/api/state',session=session)[0],401)
        self.assertEqual(self.call('/api/auth/login',dict(login='head0',password='wrong'))[0],401)
        with self.server.store.connect() as db:
            stored=db.execute('SELECT password FROM users LIMIT 1').fetchone()[0]
        self.assertNotEqual(stored,'Long-password-42')

    def test_registration_cannot_choose_admin_role_or_reuse_invitation(self):
        invite=self.server.auth.invite(self.admin['user'],self.state(),'ENG-402')
        payload=dict(login='new-employee',name='Новый сотрудник',password='Long-password-42',code=invite['code'],role='admin')
        self.assertEqual(self.call('/api/auth/register',payload)[0],400)
        payload['role']='member'
        code,value,_=self.call('/api/auth/register',payload)
        self.assertEqual(code,200)
        self.assertEqual((value['user']['role'],value['user']['team']),('member','Team-L'))
        payload['login']='another-login'
        self.assertEqual(self.call('/api/auth/register',payload)[0],400)
        with self.server.store.connect() as db:
            counts=db.execute("SELECT team,COUNT(*) AS n FROM users WHERE role='admin' GROUP BY team").fetchall()
        self.assertEqual(len(counts),6);self.assertTrue(all(r['n']==1 for r in counts))
        with self.assertRaises(InputError):
            self.server.auth.register(dict(login='another-head',name='Head',password='Long-password-42',code=self.codes['Team-L']),self.server.project_id)

    def test_core_risks_analysis_and_explain(self):
        """Глава видит риски, разбор и звёздную карту ядра; разбор по запросу план не меняет."""
        _,view,_=self.call('/api/state',session=self.admin)
        self.assertTrue(view['risks']);self.assertTrue({r['level'] for r in view['risks']}<={'red','yellow','orange'})
        self.assertIn('engines',view['analysis']);self.assertTrue(view['star']['bus_factor'])
        _,mine,_=self.call('/api/state',session=self.member)
        self.assertEqual((mine['risks'],mine['analysis'],mine['star']),([],{},None))
        before=self.state()['revision']
        code,value,_=self.call('/api/explain',{},self.admin)
        self.assertEqual(code,200,value);self.assertIn('tasks',value);self.assertIn('initiatives',value)
        # Контрфакт считается для каждой отложенной работы и сорванной цели.
        postponed={t for t,e in self.state()['plan']['entries'].items() if e['status'] in ('beyond','blocked','partial')}
        self.assertEqual({t['id'] for t in value['tasks']}>=postponed,True)
        self.assertTrue(all(i['counterfactual']['status'] for i in value['initiatives']+value['tasks']))
        self.assertEqual(self.state()['revision'],before)
        self.assertEqual(self.call('/api/explain',{},self.member)[0],403)

    def test_stress_scenarios_and_what_if_do_not_change_plan(self):
        """Стресс-сценарии и проверка «что будет без сотрудника» — только расчёт, без сохранения."""
        before=self.state()['revision']
        code,value,_=self.call('/api/scenarios',{},self.admin)
        self.assertEqual(code,200,value)
        keys={s['key']:s for s in value['scenarios']}
        self.assertEqual(set(keys),{'capacity','no_external','no_borrowing'})
        # При ёмкости на 20 % ниже часть целей уходит, и у каждой перенесённой работы есть решение и причина.
        self.assertLess(keys['capacity']['initiatives_done'],value['base_done'])
        self.assertTrue(keys['capacity']['tasks'])
        self.assertTrue(all(t['decision'] in ('postpone','cancel') and t['reason'] for t in keys['capacity']['tasks']))
        # ENG-419 — единственный Девопс: без него цели уходят за квартал.
        code,value,_=self.call('/api/whatif/absence',dict(engineer_id='ENG-419',from_sprint=1,to_sprint=6),self.admin)
        self.assertEqual(code,200,value)
        self.assertTrue(value['goals_lost'])
        self.assertEqual(sum(value['lost'].values()),480)
        self.assertEqual(self.state()['revision'],before)
        self.assertEqual(self.call('/api/whatif/absence',dict(engineer_id='ENG-419',from_sprint=0,to_sprint=6),self.admin)[0],400)
        self.assertEqual(self.call('/api/scenarios',{},self.member)[0],403)
        self.assertEqual(self.call('/api/whatif/absence',dict(engineer_id='ENG-419'),self.member)[0],403)

    def test_org_replan_requires_the_right(self):
        """Пересчёт затрагивает все команды, поэтому роли главы мало."""
        other=self.admins['Team-Alpha']
        before=self.state()['revision']
        code,value,_=self.call('/api/replan',self.body(),other)
        self.assertEqual(code,403)
        self.assertIn('право',value['error'])
        self.assertEqual(self.state()['revision'],before)
        code,view,_=self.call('/api/state',session=other)
        self.assertFalse(view['user']['org_replan'])
        code,view,_=self.call('/api/state',session=self.admin)
        self.assertTrue(view['user']['org_replan'])
        # Выданное право действует сразу, без повторного входа, и отзывается.
        pid=self.server.project_id
        self.server.auth.set_org_replan(pid,other['user']['login'],True)
        try:
            self.assertEqual(self.call('/api/replan',self.body(),other)[0],200)
        finally:
            self.server.auth.set_org_replan(pid,other['user']['login'],False)
        self.assertEqual(self.call('/api/replan',self.body(),other)[0],403)
        # Участнику право не выдаётся: пересчёт доступен только главам.
        with self.assertRaises(InputError):
            self.server.auth.set_org_replan(pid,self.member['user']['login'],True)
        with self.assertRaises(InputError):
            self.server.auth.set_org_replan(pid,'nobody',True)

    def test_member_scope_and_mutation_denials(self):
        code,s,_=self.call('/api/state',session=self.member)
        self.assertEqual(code,200)
        # Участник видит свою команду и, сверх неё, только те работы,
        # на которые назначен лично: при общем фонде специалистов
        # инженера привлекают и в чужие задачи.
        foreign=[t for t in s['tasks'] if t['team']!='Team-L']
        self.assertIn('Team-L',{t['team'] for t in s['tasks']})
        for t in foreign:
            self.assertTrue(t['mine'],f"{t['id']}: чужая задача без назначения")
        self.assertEqual((s['skills'],s['requests'],s['versions'],s['source']),([],[],[],{}))
        before=self.state()['revision']
        for path in ['/api/tasks/update','/api/tasks/add','/api/employees/add','/api/employees/availability','/api/requests/create','/api/requests/decide','/api/facts','/api/approve','/api/replan','/api/invitations']:
            self.assertEqual(self.call(path,self.body(),self.member)[0],403,path)
        self.assertEqual(self.state()['revision'],before)

    def test_admin_cannot_manage_foreign_team_or_project(self):
        self.assertEqual(self.call('/api/tasks/update',self.body(task_id='MOB-7012',changes={'priority':999}),self.admin)[0],403)
        self.assertEqual(self.call('/api/tasks/add',self.body(team='Team-K'),self.admin)[0],403)
        outsider=next(p['id'] for p in self.state()['dataset']['engineers'] if not p['allocations'].get('Team-L'))
        self.assertEqual(self.call('/api/invitations',self.body(engineer_id=outsider),self.admin)[0],403)
        self.assertEqual(self.call('/api/replan',self.body(project_id='other'),self.admin)[0],403)
        self.assertEqual(self.call('/api/replan',self.body(config={'transfers':[]}),self.admin)[0],400)

    def test_transfer_requires_both_heads_and_preserves_baseline(self):
        self.approve_baseline();baseline=copy.deepcopy(self.state()['baseline'])
        proposal=dict(kind='transfer',task_id='ONK-2914',to_team='Team-Alpha')
        code,s,_=self.call('/api/requests/create',self.body(proposal=proposal),self.admin)
        self.assertEqual(code,200)
        request=s['requests'][-1];self.assertEqual(self.state()['plan']['tasks']['ONK-2914']['team'],'Team-L')
        self.assertEqual(self.call('/api/requests/decide',self.body(request_id=request['id'],decision='approve'),self.admin)[0],400)
        self.assertEqual(self.call('/api/requests/decide',self.body(request_id=request['id'],decision='approve'),self.admins['Team-K'])[0],403)
        code,s,_=self.call('/api/requests/decide',self.body(request_id=request['id'],decision='approve'),self.admins['Team-Alpha'])
        self.assertEqual(code,200)
        self.assertEqual(self.state()['plan']['tasks']['ONK-2914']['team'],'Team-Alpha')
        self.assertEqual(self.state()['baseline'],baseline)
        self.assertEqual(s['requests'][-1]['status'],'approved')
        self.assertEqual(self.call('/api/requests/decide',self.body(request_id=request['id'],decision='approve'),self.admins['Team-Alpha'])[0],409)

    def test_stale_approval_requires_new_consent(self):
        _,s,_=self.call('/api/requests/create',self.body(proposal=dict(kind='transfer',task_id='ONK-2914',to_team='Team-Alpha')),self.admin)
        rid=s['requests'][-1]['id']
        self.call('/api/tasks/update',self.body(task_id='ONK-2914',changes={'priority':100}),self.admin)
        peer=self.admins['Team-Alpha']
        self.assertEqual(self.call('/api/requests/decide',self.body(request_id=rid,decision='approve'),peer)[0],409)
        code,s,_=self.call('/api/requests/decide',self.body(request_id=rid,decision='refresh'),peer)
        self.assertEqual(code,200);self.assertEqual(s['requests'][-1]['approvals'],['Team-Alpha'])
        self.assertEqual(self.state()['plan']['tasks']['ONK-2914']['team'],'Team-L')
        self.assertEqual(self.call('/api/requests/decide',self.body(request_id=rid,decision='approve'),self.admin)[0],200)

    def test_exchange_and_help_are_atomic(self):
        other=next(t['id'] for t in self.state()['plan']['tasks'].values() if t['team']=='Team-Alpha' and t['status']!='Done')
        _,s,_=self.call('/api/requests/create',self.body(proposal=dict(kind='exchange',task_id='ONK-2914',other_task_id=other,to_team='Team-Alpha')),self.admin)
        self.assertEqual(self.state()['plan']['tasks'][other]['team'],'Team-Alpha')
        code,_,_=self.call('/api/requests/decide',self.body(request_id=s['requests'][-1]['id'],decision='approve'),self.admins['Team-Alpha'])
        self.assertEqual(code,200)
        self.assertEqual(self.state()['plan']['tasks'][other]['team'],'Team-L')
        _,s,_=self.call('/api/requests/create',self.body(proposal=dict(kind='help',engineer='ENG-405',from_team='Team-L',to_team='Team-Alpha',hours=8,start=1,end=2)),self.admin)
        self.assertFalse(self.state()['config']['transfers'])
        code,_,_=self.call('/api/requests/decide',self.body(request_id=s['requests'][-1]['id'],decision='approve'),self.admins['Team-Alpha'])
        self.assertEqual(code,200)
        state=self.state();self.assertEqual(state['config']['transfers'][-1]['hours'],8)
        self.assertFalse(state['plan']['validation'])

    def test_new_employee_task_manual_assignment_and_preview(self):
        code,s,_=self.call('/api/employees/add',self.body(name='Новый разработчик',roles=['Разработчик Java'],skills=['Java'],capacity_rate=1),self.admin)
        self.assertEqual(code,200);eid=next(p['id'] for p in s['people'] if p['name']=='Новый разработчик')
        code,s,_=self.call('/api/tasks/add',self.body(task_id='NEW-1',title='=HYPERLINK("x")',initiative='NEW-I',sp=1,priority=999,hours={'Разработчик Java':4}),self.admin)
        self.assertEqual(code,200)
        changes=dict(assignments={'Разработчик Java':eid},not_before=3)
        before=self.state()['revision']
        code,value,_=self.call('/api/tasks/update',self.body(task_id='NEW-1',changes=changes,preview=True),self.admin)
        self.assertEqual(code,200);self.assertEqual(value['entry']['start'],3);self.assertEqual(self.state()['revision'],before)
        code,_,_=self.call('/api/tasks/update',self.body(task_id='NEW-1',changes=changes),self.admin)
        self.assertEqual(code,200);t=self.state()['plan']['entries']['NEW-1']
        # Движок и раскладка выводятся в сообщение: если расчёт упёрся
        # в страховочный лимит и перешёл на запасной план, расписание
        # другое, и это должно быть видно сразу.
        plan=self.state()['plan']
        self.assertEqual({a['engineer'] for f in t['fragments'] for a in f['assignments']},{eid},
                         f"движок {plan['engine']}, фрагменты {t['fragments']}")
        code,data,_=self.call('/api/report.csv',session=self.admin)
        self.assertIn("'=HYPERLINK",data.decode('utf-8-sig'))

    def test_facts_wait_for_every_team_and_are_idempotent(self):
        self.approve_baseline();baseline=copy.deepcopy(self.state()['baseline'])
        first=self.report('Team-L')
        code,_,_=self.call('/api/facts',self.body(report=first),self.admin)
        self.assertEqual(code,200);self.assertEqual(self.state()['plan']['cutoff'],0)
        revision=self.state()['revision']
        self.assertEqual(self.call('/api/facts',self.body(report=first),self.admin)[0],200)
        self.assertEqual(self.state()['revision'],revision)
        for team,account in self.admins.items():
            if team=='Team-L':continue
            last=self.report(team)
            code,value,_=self.call('/api/facts',self.body(report=last),account)
            self.assertEqual(code,200,value)
        # Отчёты сохраняются без пересчёта: спринт закрывает итоговый пересчёт.
        self.assertEqual(self.state()['plan']['cutoff'],0)
        self.assertEqual(self.call('/api/sprint/close',self.body(sprint=1),self.admins['Team-Alpha'])[0],403)
        code,value,_=self.call('/api/sprint/close',self.body(sprint=1),self.admin)
        self.assertEqual(code,200,value)
        self.assertEqual(self.state()['plan']['cutoff'],1)
        self.assertEqual(self.state()['baseline'],baseline)
        self.assertAlmostEqual(self.state()['plan']['resources']['Team-L']['capacity'],(8+12+0)/3*.8)
        revision=self.state()['revision']
        self.assertEqual(self.call('/api/facts',self.body(report=last),account)[0],200)
        self.assertEqual(self.state()['revision'],revision)
        foreign=self.report('Team-L');foreign['team']='Team-K'
        self.assertEqual(self.call('/api/facts',self.body(report=foreign),self.admin)[0],403)

    def test_updated_dataset_upload_and_final_recalculation(self):
        """Обновлённый датасет после спринта: загрузка без пересчёта, итоговый пересчёт, исправление."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from sprint_dataset import simulate, write
        from src.dataset import DATASET_PATH
        self.approve_baseline();baseline=copy.deepcopy(self.state()['baseline'])
        folder=Path(self.temp.name)
        data,statuses,spent,velocity,day=simulate(DATASET_PATH,1,{'DB-202'})
        write(DATASET_PATH,folder/'s1.xlsx',data,statuses,spent,velocity,day)
        raw=(folder/'s1.xlsx').read_bytes()
        xlsx='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        def upload(session,sprint=None):
            headers={'Content-Type':xlsx,'X-Revision':str(self.state()['revision'])}
            if sprint is not None:headers['X-Sprint']=str(sprint)
            return self.call('/api/dataset/upload',raw=raw,session=session,headers=headers)
        self.assertEqual(upload(self.admins['Team-Alpha'])[0],403)
        before=self.state()['plan']
        code,view,_=upload(self.admin)
        self.assertEqual(code,200,view)
        # Загрузка не пересчитывает план.
        self.assertEqual(self.state()['plan'],before)
        current=view['sprint_data']['current']
        self.assertEqual(current['sprint'],1);self.assertTrue(current['ready'])
        self.assertEqual(set(current['team_sources'].values()),{'dataset'})
        self.assertEqual(len(current['dataset']['summary']['closed']),sum(s=='Done' for s in statuses.values()))
        code,view,_=self.call('/api/sprint/close',self.body(sprint=1),self.admin)
        self.assertEqual(code,200,view)
        state=self.state()
        self.assertEqual(state['plan']['cutoff'],1);self.assertEqual(state['baseline'],baseline)
        self.assertIn('DB-202',{d['task_id'] for d in state['reviews']['1']['deviations']})
        self.assertEqual(state['actuals'][0]['teams'],{t:v['achieved'] for t,v in velocity.items()})
        # Команды, сделавшие план спринта, не теряют производительность; отставшая — теряет.
        resources=state['plan']['resources']
        self.assertAlmostEqual(resources['Team-L']['capacity'],8.0)
        self.assertLess(resources['Team-Platform']['capacity'],7.2)
        # Выполнение спринта: отстала только DB-202, остальное — ровно по плану.
        sprint=self.call('/api/state',session=self.admin)[1]['metrics']['sprints'][0]
        self.assertGreater(sprint['percent'],95);self.assertLess(sprint['percent'],100)
        # У отклонения видны последствия, у сдвига — своя причина.
        self.assertTrue(all(c['reasons'] for c in state['changes']))
        self.assertIn('consequences',state['reviews']['1'])
        self.assertIsNone(view['sprint_data']['correction'])
        # Исправление закрытого спринта: спринт не удаляется, его факт заменяется.
        code,view,_=upload(self.admin,1)
        self.assertEqual(code,200,view);self.assertTrue(view['sprint_data']['correction'])
        self.assertEqual(self.call('/api/sprint/close',self.body(sprint=1),self.admin)[0],200)
        state=self.state()
        self.assertEqual([r['sprint'] for r in state['actuals']],[1]);self.assertEqual(state['plan']['cutoff'],1)
        self.assertEqual(upload(self.admin,3)[0],400)

    def test_initial_dataset_upload_waits_for_recalculation(self):
        """До фиксации базы датасет заменяет исходные данные; план пересчитывает отдельная кнопка."""
        from src.dataset import DATASET_PATH
        raw=Path(DATASET_PATH).read_bytes()
        headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-Revision':str(self.state()['revision'])}
        before=self.state()['plan']
        code,view,_=self.call('/api/dataset/upload',raw=raw,session=self.admin,headers=headers)
        self.assertEqual(code,200,view)
        self.assertTrue(view['plan_stale']);self.assertEqual(self.state()['plan'],before)
        # Согласовать можно только пересчитанный план.
        self.assertEqual(self.call('/api/approve',self.body(),self.admin)[0],400)
        self.assertEqual(self.call('/api/replan',self.body(),self.admin)[0],200)
        self.assertFalse(self.state().get('plan_stale'))
        self.assertEqual(self.call('/api/dataset/upload',raw=b'not an excel',session=self.admin,
                                   headers=dict(headers,**{'X-Revision':str(self.state()['revision'])}))[0],400)

    def test_absence_recalculates_plan_and_can_be_cancelled(self):
        """Отпуск или болезнь сотрудника: проверка рисков, сохранение с пересчётом, отмена."""
        engineer=next(p for p in self.state()['dataset']['engineers'] if p['allocations']=={'Team-L':1.0})['id']
        absence=dict(engineer_id=engineer,start='2026-10-05',end='2026-10-09',reason='Болезнь',comment='ОРВИ')
        before=self.state()['revision']
        code,value,_=self.call('/api/employees/absence',self.body(preview=True,**absence),self.admin)
        self.assertEqual(code,200,value);self.assertTrue(value['preview']);self.assertEqual(value['lost'],{'1':40.0})
        self.assertEqual(self.state()['revision'],before)
        self.assertEqual(self.call('/api/employees/absence',self.body(**absence),self.admins['Team-K'])[0],403)
        self.assertEqual(self.call('/api/employees/absence',self.body(**absence),self.member)[0],403)
        self.assertEqual(self.call('/api/employees/absence',self.body(**dict(absence,reason='Прогул')),self.admin)[0],400)
        code,view,_=self.call('/api/employees/absence',self.body(**absence),self.admin)
        self.assertEqual(code,200,view)
        saved=self.state()
        self.assertEqual(len(saved['config']['absences']),1)
        self.assertEqual(view['absences'][0]['lost'],{'1':40.0})
        used=sum(a['hours'] for e in saved['plan']['entries'].values() for f in e['fragments'] if f['sprint']==1 for a in f['assignments'] if a['engineer']==engineer)
        self.assertLessEqual(used,40+1e-6)
        self.assertTrue(any('Отсутствие' in c['reason'] for c in saved['changes']) or not saved['changes'])
        code,_,_=self.call('/api/employees/absence/delete',self.body(absence_id=saved['config']['absences'][0]['id']),self.admin)
        self.assertEqual(code,200);self.assertEqual(self.state()['config']['absences'],[])

    def test_csv_export_and_fact_import_roundtrip(self):
        code,data,headers=self.call('/api/report.csv',session=self.member)
        self.assertEqual(code,200);self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig')),delimiter=';'))
        # Отчёт участника повторяет его область видимости: своя команда
        # плюс чужие работы, на которые он назначен лично.
        self.assertTrue(rows);self.assertIn('Team-L',{r['Команда'] for r in rows})
        own={t['id'] for t in self.state()['plan']['tasks'].values() if t['team']=='Team-L'}
        for r in rows:
            if r['Команда']!='Team-L':
                self.assertIn('ENG-401',r['Исполнители'],r['Задача'])
            else:
                self.assertIn(r['Задача'],own)
        self.assertEqual(self.call('/api/tasks/add',self.body(task_id='CSV-1',title='Проверка CSV',initiative='CSV',sp=1,hours={'=FORMULA':1,"'Литерал":2}),self.admin)[0],200)
        _,template,_=self.call('/api/facts/template.csv',session=self.admin)
        self.assertIn("'=FORMULA",template.decode('utf-8-sig'))
        rows=list(csv.DictReader(io.StringIO(template.decode('utf-8-sig')),delimiter=';'))
        rows[0]['team_sp']='0'
        # Остатки в шаблоне пустые — их считает правило ядра. Глава
        # вписывает свою оценку, и она переживает круг через CSV.
        csv_rows=[r for r in rows if r['task_id']=='CSV-1']
        self.assertEqual({r['remaining_hours'] for r in csv_rows},{''})
        for r in csv_rows:
            r['remaining_hours']='1' if 'FORMULA' in r['role'] else '2'
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=rows[0].keys(),delimiter=';');writer.writeheader();writer.writerows(rows)
        before=self.state()['revision']
        code,value,_=self.call('/api/facts/parse-csv',raw=out.getvalue().encode(),session=self.admin,headers={'Content-Type':'text/csv'})
        self.assertEqual(code,200,value);self.assertEqual(len(value['report']['tasks']),len(self.report('Team-L')['tasks']))
        self.assertEqual(next(t for t in value['report']['tasks'] if t['task_id']=='CSV-1')['remaining_hours'],{'=FORMULA':1,"'Литерал":2})
        self.assertEqual(self.state()['revision'],before)
        for raw in [b'\n',b'wrong;header\nx;y',(out.getvalue()+out.getvalue().splitlines()[-1]+'\n').encode()]:
            self.assertEqual(self.call('/api/facts/parse-csv',raw=raw,session=self.admin)[0],400)
        for row in rows:row['sprint']='2'
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=rows[0].keys(),delimiter=';');writer.writeheader();writer.writerows(rows)
        self.assertEqual(self.call('/api/facts/parse-csv',raw=out.getvalue().encode(),session=self.admin)[0],400)

    def test_closed_sprint_keeps_assignments_and_fact_in_report(self):
        role='Разработчик Java'
        self.assertEqual(self.call('/api/tasks/add',self.body(task_id='HIST-1',title='Задача с историей',initiative='HIST',sp=1,priority=999,hours={role:4}),self.admin)[0],200)
        self.assertEqual(self.call('/api/tasks/update',self.body(task_id='HIST-1',changes={'assignments':{role:'ENG-401'}}),self.admin)[0],200)
        self.assertEqual(self.state()['plan']['entries']['HIST-1']['start'],1)
        self.approve_baseline()
        baseline=copy.deepcopy(self.state()['baseline'])
        for team,account in self.admins.items():
            report=self.report(team)
            for task in report['tasks']:
                if task['task_id']=='HIST-1':
                    task.update(status='Done',remaining_sp=0,remaining_hours={},spent_hours={role:6})
            self.assertEqual(self.call('/api/facts',self.body(report=report),account)[0],200)
        self.assertEqual(self.call('/api/sprint/close',self.body(sprint=1),self.admin)[0],200)
        self.assertEqual(self.state()['plan']['cutoff'],1)
        before=copy.deepcopy(self.state()['plan']['frozen'])
        self.assertEqual(self.call('/api/replan',self.body(),self.admin)[0],200)
        self.assertEqual(self.state()['plan']['frozen'],before)
        self.assertEqual(self.state()['baseline'],baseline)
        _,state,_=self.call('/api/state',session=self.member)
        task=next(t for t in state['tasks'] if t['id']=='HIST-1')
        self.assertFalse(task['entry']['fragments'])
        self.assertEqual(task['history'][0]['sprint'],1)
        self.assertEqual(task['facts'][0]['spent_hours'],{role:6})
        _,data,_=self.call('/api/report.csv',session=self.member)
        row=next(r for r in csv.DictReader(io.StringIO(data.decode('utf-8-sig')),delimiter=';') if r['Задача']=='HIST-1')
        self.assertEqual(row['Исполнители'],'ENG-401')
        self.assertEqual(float(row['План SP, спринт 1']),1)
        self.assertEqual(float(row['Факт часов, спринт 1']),6)
        self.assertEqual(float(row['Потрачено часов всего']),6)
        self.assertEqual(row['Факт часов, спринт 2'],'')
        self.assertEqual((row['Начало (спринт)'],row['Завершение (спринт)']),('1','1'))


if __name__=='__main__':unittest.main()
