import copy
import math
import random
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from demo import demo_data
from planner.engine import (default_config, snapshot, skills_map,
                            capacities)
from planner.present import build_plan, quarter_metrics
from planner.flow import allocate
from planner.importer import import_xlsx, InputError, number
from planner.service import Service, validate_config
from planner.store import Store, ConflictError
from src.dataset import DATASET_PATH

ROOT = Path(__file__).resolve().parents[1]


def small_data(sp=8, hours=None):
    data = demo_data()
    data['tasks'] = [data['tasks'][0]]
    data['tasks'][0].update(id='T1', sp=sp, hours=hours or {'Разработчик': 80}, required_skills={})
    data['engineers'] = [data['engineers'][0]]
    data['history'] = [dict(team='Команда Продукт', date='2026-09-01', achieved=10, planned=10)]
    data['dependencies'] = []
    return data


def report_for(state, sprint=1, achieved=0):
    return dict(sprint=sprint, teams={team: achieved for team in state['plan']['resources']},
        tasks=[dict(task_id=tid, status=t['status'], remaining_sp=t['remaining_sp'],
                    remaining_hours=copy.deepcopy(t['remaining_hours']))
               for tid, t in state['plan']['tasks'].items() if t['status'] != 'Done'], note='Тестовый факт')


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = import_xlsx(DATASET_PATH)

    def test_real_counts_and_shared_people(self):
        d = self.data
        self.assertEqual((len(d['tasks']), len(d['engineers']), len(d['dependencies']), len(d['history'])), (45, 30, 19, 12))
        self.assertEqual(d['source']['estimate_tasks'], 45)
        self.assertFalse(any(i['code']=='estimate_without_task' for i in d['issues']))
        p = next(p for p in d['engineers'] if p['id'] == 'ENG-405')
        self.assertEqual(p['allocations'], {'Team-L': .5, 'Team-Alpha': .5})

    def test_detailed_estimates_override_inconsistent_totals(self):
        t = next(t for t in self.data['tasks'] if t['id'] == 'MOB-7013')
        self.assertEqual(t['original_hh'], 120)
        self.assertEqual(sum(t['hours'].values()), 110)
        self.assertIn('hours_mismatch', {i['code'] for i in self.data['issues'] if i['entity'] == t['id']})

    def test_exhausted_hours_do_not_complete_task(self):
        tasks = snapshot(self.data, default_config(), [])
        self.assertEqual(tasks['MOB-7011']['status'], 'InProgress')
        # inprogress_sp = by_hours: задача в работе стоит долю оценки, равную
        # доле оставшихся часов. Все 165 часов выработаны — 0 SP из 5.
        self.assertEqual(tasks['MOB-7011']['remaining_sp'], 0)
        self.assertEqual(tasks['MOB-7011']['remaining_hours'], {})
        # Задача с частью выработанных часов стоит долю оценки: DB-202 — 247 ч из 324.
        self.assertAlmostEqual(tasks['DB-202']['remaining_sp'], 8 * 247 / 324)
        plan = build_plan(self.data, default_config())
        # Оценка часов исчерпана, а задача не закрыта: она закрывается
        # в первом спринте без затрат очков и часов, а расхождение
        # выводится предупреждением, а не молча блокирует задачу.
        self.assertEqual(plan['entries']['MOB-7011']['status'], 'planned')
        self.assertEqual([f['sp'] for f in plan['entries']['MOB-7011']['fragments']], [0.0])
        self.assertIn('без затрат', plan['entries']['MOB-7011']['reasons'][0])
        self.assertTrue(any('MOB-7011' in w['entity'] or 'MOB-7011' in w['message']
                            for w in plan['diagnostics']))

    def test_real_velocities_and_complete_plan_invariants(self):
        p = build_plan(self.data, default_config())
        expected = {'Team-L':8, 'Team-K':12.8, 'Team-Platform':7.2, 'Team-Alpha':10.8, 'Team-Beta':8.4, 'Team-Delta':7.6}
        for t, v in expected.items():
            self.assertAlmostEqual(p['resources'][t]['capacity'], v)
        self.assertFalse(p['validation'])

    def test_numeric_invalid_values(self):
        for value in ['NaN', 'inf', -1, True, None]:
            with self.assertRaises(InputError):
                number(value)
        self.assertEqual(number('0,5'), .5)


class PlannerTests(unittest.TestCase):
    def test_sp_limit_splits_large_task(self):
        d = small_data(sp=11, hours={'Разработчик': 40})
        p = build_plan(d, default_config())
        entry = p['entries']['T1']
        self.assertEqual(entry['end'], 2)
        self.assertAlmostEqual(sum(f['sp'] for f in entry['fragments']), 11)
        self.assertAlmostEqual(entry['fragments'][0]['sp'], 8)

    def test_hours_limit_even_when_sp_is_available(self):
        d = small_data(sp=2, hours={'Разработчик': 160})
        e = build_plan(d, default_config())['entries']['T1']
        self.assertEqual(e['end'], 2)
        self.assertAlmostEqual(e['fragments'][0]['sp'], 1, places=5)

    def test_missing_role_is_external_and_reported(self):
        """Роль, которой нет ни у кого, выносится за штат и показывается.

        Прежний планировщик блокировал такую работу навсегда: на
        официальном датасете это давало 33 заблокированные задачи из 45
        при шести ролях без носителей."""
        d = small_data(hours={'Разработчик': 20, 'Менеджер': 10})
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['status'], 'planned')
        external = [w for w in p['diagnostics'] if w['code'] == 'external_role']
        self.assertEqual([w['entity'] for w in external], ['Менеджер'])
        self.assertIn('10 ч', external[0]['message'])

    def test_multi_role_person_shares_single_capacity(self):
        d = small_data(hours={'Разработчик': 80, 'QA': 80})
        d['engineers'][0]['roles'].append('QA')
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['end'], 2)
        for s in p['usage']:
            self.assertLessEqual(s['people']['DEV-1']['used'], 80.00001)

    def test_flow_reroutes_flexible_person_to_scarce_role(self):
        ok, rows = allocate({'Dev': 40, 'QA': 40}, {'A':40, 'B':40}, {'Dev':['A','B'], 'QA':['A']})
        self.assertTrue(ok)
        self.assertEqual({(r['engineer'], r['role']):r['hours'] for r in rows}, {('A','QA'):40, ('B','Dev'):40})

    def test_person_in_two_teams_not_double_booked(self):
        d = small_data(sp=8)
        second = copy.deepcopy(d['tasks'][0]); second.update(id='T2', team='Команда Два')
        d['tasks'].append(second)
        d['engineers'][0]['allocations'] = {'Команда Продукт':.5, 'Команда Два':.5}
        d['history'].append(dict(team='Команда Два', date='2026-09-01', achieved=10, planned=10))
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['end'], 2)
        self.assertEqual(p['entries']['T2']['end'], 2)
        self.assertAlmostEqual(p['usage'][0]['people']['DEV-1']['used'], 80, places=4)

    def test_dependency_leaves_an_empty_sprint(self):
        """Онбординг: между блокирующей и блокируемой задачей зазор
        минимум в один спринт, а не просто следующий спринт."""
        d = small_data(sp=1, hours={'Разработчик': 10})
        t = copy.deepcopy(d['tasks'][0]);t.update(id='T2')
        d['tasks'].append(t)
        d['dependencies'] = [dict(first='T1',second='T2',kind='has to be done before')]
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['end'], 1)
        self.assertEqual(p['entries']['T2']['start'], 3)
        self.assertFalse(p['validation'])

    def test_cycle_is_reported_and_unknown_predecessor_ignored(self):
        d = small_data()
        d['dependencies'] = [dict(first='T1',second='T1',kind='depends on')]
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['status'], 'blocked')
        self.assertTrue(any(i['code']=='cycle' for i in p['diagnostics']))

        # Связь на несуществующую задачу не блокирует работу: она
        # не учитывается, а о ней предупреждает импорт.
        d['dependencies'] = [dict(first='UNKNOWN',second='T1',kind='is required for')]
        p = build_plan(d, default_config())
        self.assertEqual(p['entries']['T1']['status'], 'planned')

    def test_dependency_direction_follows_the_columns(self):
        """Первый столбец блокирует второй при любом типе связи.

        На официальном датасете такое чтение не даёт ни одного
        противоречия со статусами Done, а разворот depends on даёт."""
        d = small_data(sp=1, hours={'Разработчик': 10})
        t = copy.deepcopy(d['tasks'][0]);t.update(id='T2')
        d['tasks'].append(t)
        d['dependencies'] = [dict(first='T1',second='T2',kind='depends on')]
        p = build_plan(d, default_config())
        self.assertIn(['T1','T2'], p['edges'])
        self.assertGreater(p['entries']['T2']['start'], p['entries']['T1']['end'])

    def test_absence_changes_hours_not_bus_factor(self):
        d = small_data();c = default_config();c['availability']={'DEV-1':{'1':0}}
        p = build_plan(d,c)
        self.assertEqual(p['entries']['T1']['start'], 2)
        skill = next(s for s in skills_map(d,c,p,1)['cells'] if s['skill']=='Python')
        self.assertEqual((skill['bf'], skill['available_hours']), (1,0))

    def test_skill_constraint_is_checked(self):
        d = small_data();d['tasks'][0]['required_skills']={'Разработчик':['Rust']}
        self.assertEqual(build_plan(d, default_config())['entries']['T1']['status'], 'blocked')

    def test_validation_is_carried_with_the_plan(self):
        """Независимая проверка ядра выполняется при каждом расчёте
        и приезжает вместе с планом, а не бросает исключение."""
        d=small_data();c=default_config()
        self.assertEqual(build_plan(d,c)['validation'], [])

        # Расчёт не удался — сервер получает план с причиной, а не падает.
        broken=small_data()
        broken['dependencies']=[dict(first='T1',second='T1',kind='has to be done before')]
        p=build_plan(broken,c)
        self.assertTrue(p['validation'])
        self.assertTrue(any(w['code']=='cycle' for w in p['diagnostics']))

    def test_random_resources_respect_global_bounds(self):
        rng = random.Random(19)
        for _ in range(25):
            d=small_data(sp=rng.randint(1,30), hours={'Разработчик':rng.randint(20,600)})
            c=default_config();c['availability']={'DEV-1':{str(s):rng.choice([0,20,40,80]) for s in range(1,13)}}
            p=build_plan(d,c)
            self.assertFalse(p['validation'])
            self.assertEqual(p,build_plan(d,c))


def demo_config(**changes):
    """Настройки учебного набора: QA-01 недоступен в четвёртом спринте."""
    config=default_config();config['availability']={'QA-01':{'4':0}}
    config.update(changes)
    return config


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.db')
        self.service=Service(self.store)
        self.state=self.service.create(demo_data(),demo_config())

    def tearDown(self):
        self.temp.cleanup()

    def body(self, state=None, **kwargs):
        state=state or self.state
        return dict(project_id=state['project_id'],revision=state['revision'],**kwargs)

    def test_actual_requires_baseline_and_all_teams(self):
        with self.assertRaises(InputError):self.service.actual(self.body(report=report_for(self.state)))
        self.state=self.service.approve(self.body())
        r=report_for(self.state);r['teams']={}
        with self.assertRaises(InputError):self.service.actual(self.body(report=r))

    def test_low_actual_updates_average_preserves_baseline_and_is_idempotent(self):
        self.state=self.service.approve(self.body())
        base=copy.deepcopy(self.state['baseline'])
        report=report_for(self.state,achieved=0)
        new=self.service.actual(self.body(report=report))
        self.assertAlmostEqual(new['plan']['resources']['Команда Продукт']['capacity'],6.4)
        self.assertEqual(new['baseline'],base)
        self.assertTrue(new['changes'])
        again=self.service.actual(self.body(new,report=report))
        self.assertEqual(again['revision'],new['revision'])
        self.assertEqual(len(again['actuals']),1)
        self.assertTrue(all(f['sprint']>1 for e in new['plan']['entries'].values() for f in e['fragments']))

    def test_partial_fact_explicit_remaining_is_used(self):
        self.state=self.service.approve(self.body())
        r=report_for(self.state)
        r['tasks'][0].update(status='InProgress',remaining_sp=3,remaining_hours={'Разработчик':70})
        new=self.service.actual(self.body(report=r))
        self.assertEqual(new['plan']['tasks']['API-1']['remaining_sp'],3)
        self.assertEqual(new['plan']['tasks']['API-1']['remaining_hours'],{'Разработчик':70})

    def test_fact_preserves_manual_priority_and_skill_requirements(self):
        required={'Разработчик':['Python']}
        changes={'API-1':dict(priority=999,required_skills=required,remaining_sp=5,remaining_hours={'Разработчик':90},after_sprint=0)}
        self.state=self.service.create(demo_data(),demo_config(task_overrides=changes))
        self.state=self.service.approve(self.body())
        baseline=copy.deepcopy(self.state['baseline'])
        for sprint in (1,2):
            report=report_for(self.state,sprint=sprint)
            next(t for t in report['tasks'] if t['task_id']=='API-1').update(status='InProgress',remaining_sp=3,remaining_hours={'Разработчик':70})
            self.state=self.service.actual(self.body(report=report))
            task=self.state['plan']['tasks']['API-1']
            self.assertEqual(task['priority'],999)
            self.assertEqual(task['required_skills'],required)
            self.assertEqual((task['remaining_sp'],task['remaining_hours']),(3,{'Разработчик':70}))
            self.assertEqual(self.state['baseline'],baseline)

    def test_done_work_is_frozen_and_not_scheduled_again(self):
        self.state=self.service.approve(self.body())
        r=report_for(self.state)
        for t in r['tasks']:
            if t['task_id']=='CORE-1': t.update(status='Done',remaining_sp=0,remaining_hours={})
        new=self.service.actual(self.body(report=r))
        self.assertEqual(new['plan']['entries']['CORE-1']['status'],'done')
        self.assertFalse(new['plan']['entries']['CORE-1']['fragments'])
        self.assertTrue(any(f['task_id']=='CORE-1' for f in new['plan']['frozen']))

    def test_transfer_changes_hours_not_velocity(self):
        """Переданные часы меняют раскладку работ, но не производительность
        команды: ёмкость в SP считается по истории, а не по часам."""
        transfer=dict(engineer='QA-02',from_team='Команда Платформа',to_team='Команда Продукт',hours=40,start=3,end=6)
        before=build_plan(demo_data(),demo_config())
        after=build_plan(demo_data(),demo_config(transfers=[transfer]))
        self.assertEqual(after['resources'],before['resources'])
        self.assertNotEqual({k:(v['start'],v['end']) for k,v in before['entries'].items()},
                            {k:(v['start'],v['end']) for k,v in after['entries'].items()})

    def test_stale_writer_rejected(self):
        # Кто-то сохранил новую версию, пока эта вкладка держала старую.
        self.store.save(self.state,self.state['revision'],'Изменение в другой вкладке')
        with self.assertRaises(ConflictError):self.service.approve(self.body())

    def test_impossible_transfer_and_overtime_rejected(self):
        data=demo_data()
        transfer=dict(engineer='QA-02',from_team='Команда Платформа',to_team='Команда Продукт',hours=41,start=3,end=3)
        with self.assertRaises(InputError):validate_config(data,demo_config(transfers=[transfer]))
        with self.assertRaises(InputError):validate_config(data,demo_config(availability={'QA-01':{'1':80}}))

    def test_zero_denominator_is_not_calculated(self):
        p=self.state['plan']
        m=quarter_metrics(self.state)
        self.assertIsNone(m['actual_percent'])
        self.assertIsNone(m['sprints'][0]['percent'])

    def test_sqlite_survives_new_service_instance(self):
        new=Service(Store(Path(self.temp.name)/'test.db'))
        self.assertEqual(new.store.get(self.state['project_id']),self.state)


if __name__=='__main__':unittest.main()
