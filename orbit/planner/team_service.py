"""Рабочие процессы команд: права, ручной план, согласования и факт."""

import csv
import hashlib
import io
import re
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone

from .auth import AccessError
from .engine import (ABSENCE_REASONS, HOURS_PER_SPRINT, absence_impact, diff_plans, known_roles, snapshot,
                     sprint_bounds, teams_of)
from .present import build_plan, explain_plan, stress_scenarios
from .sprint_data import collect_report, dataset_summary, merge_dataset, replace_initial
from .importer import InputError, import_xlsx, number, role_name
from .service import check_outcome, numeric_map, optional_number, validate_config
from .store import ConflictError, dumps


def fingerprint(value):
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def basis(state):
    return fingerprint({k: state[k] for k in ('dataset', 'config', 'actuals', 'baseline')})


def now():
    return datetime.now(timezone.utc).isoformat()


def csv_text(value):
    """Не исполнять пользовательский текст как формулу в табличном редакторе."""
    if isinstance(value, str) and (value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def from_csv_text(value):
    if value.startswith("'") and (value[1:].startswith("'") or value[1:].lstrip().startswith(('=', '+', '-', '@'))):
        return value[1:]
    return value


def text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise InputError(f'{label}: заполните поле, максимум {maximum} символов')
    return value.strip()


class TeamService:
    def __init__(self, core, auth):
        self.core, self.store, self.auth = core, core.store, auth

    def load(self, user, body=None):
        state = self.store.get(user['project_id'])
        if body is not None:
            if body.get('project_id', user['project_id']) != user['project_id']:
                raise AccessError('Нет доступа к другому проекту')
            if body.get('revision') != state['revision']:
                raise ConflictError('План изменён в другой вкладке. Обновите страницу')
        return state

    @staticmethod
    def admin(user):
        if user['role'] != 'admin':
            raise AccessError('Это действие доступно только главе команды')

    @staticmethod
    def own_task(user, state, tid):
        task = state['plan']['tasks'].get(tid)
        if task is None:
            raise InputError('Задача не найдена')
        if task['team'] != user['team']:
            raise AccessError('Можно изменять только задачи своей команды')
        if task['status'] == 'Done':
            raise InputError('Завершённая работа зафиксирована')
        return task

    def team_tasks(self, state, team):
        return [t for t in state['plan']['tasks'].values() if t['team'] == team and t['status'] != 'Done']

    def draft_valid(self, state, team, draft):
        return bool(draft and draft.get('basis') == fingerprint(self.team_tasks(state, team)))

    def approval_round(self, state, accounts=None):
        accounts = self.auth.team_accounts(state['project_id']) if accounts is None else accounts
        heads = sorted((a for a in accounts if a['role'] == 'admin'), key=lambda a: a['team'])
        stamp = fingerprint(dict(conditions=basis(state), plan=state['plan'],
                                 heads=[(a['id'], a['team']) for a in heads]))
        previous = state.get('baseline_approval', {})
        stale = bool(previous and previous.get('basis') != stamp)
        approvals = {} if stale else dict(previous.get('approvals', {}))
        return heads, stamp, approvals, stale

    def approval_progress(self, state, user, accounts):
        if state['baseline'] or user['role'] != 'admin':
            return None
        heads, _, approvals, stale = self.approval_round(state, accounts)
        return dict(required=len(heads), confirmed=len(approvals), stale=stale,
                    own_approved=user['id'] in approvals,
                    heads=[dict(team=a['team'], name=a['name'], approved=a['id'] in approvals) for a in heads])

    def approve(self, user, body):
        self.admin(user)
        state = self.load(user, body)
        if state['baseline']:
            raise InputError('Первоначальная база уже зафиксирована')
        if state.get('plan_stale'):
            raise InputError('Данные обновлены, а план ещё не пересчитан. Согласовать можно только пересчитанный план')
        heads, stamp, approvals, _ = self.approval_round(state)
        if user['id'] not in {a['id'] for a in heads}:
            raise AccessError('Подтверждение доступно зарегистрированной главе')
        if user['id'] in approvals:
            return state
        approvals[user['id']] = dict(team=user['team'], name=user['name'], approved_at=now())
        state['baseline_approval'] = dict(basis=stamp, approvals=approvals)
        if set(approvals) == {a['id'] for a in heads}:
            # Последний голос и неизменяемая база фиксируются одним save.
            return self.core.approve(body, state=state)
        return self.store.save(state, body['revision'],
                               f'Организация: {user["team"]} согласовала исходный план ({len(approvals)}/{len(heads)})')

    def view(self, user, state=None):
        state = state or self.load(user)
        public = self.core.public(state)
        admin = user['role'] == 'admin'
        people = public['skills']['people']
        me = next((p for p in people if p['id'] == user['engineer_id']), None)
        member_teams = set(me['allocations']) if me else {user['team']}
        all_teams = teams_of(state['dataset'])
        visible_teams = set(all_teams) if admin else member_teams
        history, facts = defaultdict(list), defaultdict(list)
        for fragment in state['plan'].get('frozen', []):
            history[fragment['task_id']].append(fragment)
        for report in state['actuals']:
            for outcome in report['tasks']:
                facts[outcome['task_id']].append(dict(outcome, sprint=report['sprint']))
        task_rows = []
        for tid, task in state['plan']['tasks'].items():
            entry = state['plan']['entries'][tid]
            assigned = {a['engineer'] for f in entry['fragments'] for a in f['assignments']}
            assigned.update(task.get('manual_assignments', {}).values())
            if admin or task['team'] in visible_teams or user['engineer_id'] in assigned:
                task_rows.append(dict(task, entry=entry, mine=user['engineer_id'] in assigned,
                                      history=history[tid], facts=facts[tid]))
        visible_ids = {t['id'] for t in task_rows}
        people = [p for p in people if admin or visible_teams.intersection(p['allocations'])]
        accounts = self.auth.team_accounts(user['project_id'])
        names = {a['engineer_id']: a['name'] for a in accounts if a['engineer_id']}
        people = [dict(p, name=names.get(p['id'], p['name']), registered=p['id'] in names) for p in people]
        drafts = state.get('fact_drafts', {}).get(str(state['plan']['cutoff'] + 1), {})
        requests = [dict(r, stale=r.get('basis') != basis(state)) for r in state.get('requests', []) if user['team'] in r['teams']] if admin else []
        return dict(user=user, revision=state['revision'], project_id=state['project_id'],
            tasks=task_rows, people=people, teams=[t for t in all_teams if t in visible_teams],
            resources={t: v for t, v in public['plan']['resources'].items() if t in visible_teams},
            skills=public['skills']['cells'] if admin else [],
            star=public['skills']['org'] if admin else None,
            role_bf=public['skills']['roles'] if admin else [],
            team_profiles=public['skills']['teams'] if admin else {},
            risks=state['plan'].get('risks', []) if admin else [],
            analysis=state['plan'].get('analysis', {}) if admin else {},
            engine=state['plan'].get('engine', {}),
            edges=[e for e in state['plan']['edges'] if e[0] in visible_ids and e[1] in visible_ids],
            metrics=public['metrics'] if admin else None, dates=public['dates'],
            initiatives=state['plan']['initiatives'] if admin else [],
            baseline=bool(state['baseline']), cutoff=state['plan']['cutoff'],
            baseline_approval=self.approval_progress(state, user, accounts),
            config=dict(start_date=state['config']['start_date'], hours_per_sprint=HOURS_PER_SPRINT,
                        sprints=state['config']['sprints'], horizon=state['config']['horizon']),
            changes=state['changes'] if admin else [], requests=requests,
            warnings=[w for w in state['dataset']['issues'] + state['plan']['diagnostics'] if admin or w['entity'] in visible_ids],
            source=state['dataset'].get('source', {}) if admin else {},
            versions=self.store.versions(user['project_id'])[:20] if admin else [],
            heads=[dict(team=t, name=next((a['name'] for a in accounts if a['role']=='admin' and a['team']==t), None)) for t in all_teams] if admin else [],
            submissions=[dict(team=t, submitted=t in drafts, valid=self.draft_valid(state, t, drafts.get(t))) for t in all_teams] if admin else [],
            own_draft=drafts.get(user['team']) if admin else None,
            help=state['config']['transfers'] if admin else [],
            plan_stale=state.get('plan_stale') if admin else None,
            absences=[dict(a, lost=absence_impact(state['dataset'], state['config'], a))
                      for a in state['config'].get('absences', [])
                      if admin or a['engineer'] == user['engineer_id']],
            absence_reasons=list(ABSENCE_REASONS),
            sprint_data=self.sprint_views(state) if admin else None,
            reviews=state.get('reviews', {}) if admin else {})

    def calculate(self, state, old_plan, label):
        validate_config(state['dataset'], state['config'])
        state['plan'] = build_plan(state['dataset'], state['config'], state['actuals'], old_plan, baseline=state['baseline'])
        state['plan_stale'] = None
        state['changes'] = diff_plans(old_plan, state['plan'], label)
        return state

    def org_right(self, user):
        """Загрузка данных спринта и итоговый пересчёт затрагивают всю организацию."""
        self.admin(user)
        if not user.get('org_replan'):
            raise AccessError('Загрузка датасета и итоговый пересчёт доступны главе с правом пересчёта организации. '
                              'Его выдаёт владелец установки')

    def target_sprint(self, state, requested=None):
        """Спринт, к которому относятся данные: открытый или последний закрытый (исправление)."""
        cutoff = state['plan']['cutoff']
        if not state['baseline']:
            return 0
        sprint = cutoff + 1 if requested in (None, '') else requested
        if type(sprint) is not int or sprint not in (cutoff, cutoff + 1) or not 1 <= sprint <= state['config']['sprints']:
            raise InputError('Данные принимаются за текущий спринт или для исправления последнего закрытого')
        return sprint

    def upload_dataset(self, user, raw, revision, requested=None):
        """Обновлённый датасет: сохраняется без пересчёта плана."""
        self.org_right(user)
        state = self.load(user, dict(revision=revision))
        sprint = self.target_sprint(state, requested)
        fresh = import_xlsx(raw, 'Обновлённый датасет' if sprint else 'Исходный датасет')
        meta = dict(name=fresh['name'], sha256=fresh['source']['sha256'], uploaded_by=user['name'], uploaded_at=now())
        if sprint == 0:
            # Квартал ещё не начался: датасет заменяет исходные данные,
            # задачи и люди, добавленные в сервисе, сохраняются.
            state['dataset'], notes = replace_initial(state, fresh)
            validate_config(state['dataset'], state['config'])
            state['plan_stale'] = dict(meta, reason='Загружен новый исходный датасет', notes=notes)
            return self.store.save(state, revision, f'{user["team"]}: загружен исходный датасет (без пересчёта)')
        summary = dataset_summary(state, fresh, sprint)
        state.setdefault('sprint_uploads', {})[str(sprint)] = dict(meta, data=fresh, summary=summary, applied=False)
        closed = sprint <= state['plan']['cutoff']
        label = f'исправление спринта {sprint}' if closed else f'спринт {sprint}'
        return self.store.save(state, revision, f'{user["team"]}: загружен обновлённый датасет, {label} (без пересчёта)')

    def sprint_readiness(self, state, sprint):
        """Готовность к итоговому пересчёту: откуда возьмутся данные по задачам и SP команд."""
        if not state['baseline'] or not 1 <= sprint <= state['config']['sprints']:
            return None
        upload = state.get('sprint_uploads', {}).get(str(sprint))
        drafts = state.get('fact_drafts', {}).get(str(sprint), {})
        candidate = dict(state, dataset=merge_dataset(state, upload['data'])[0]) if upload else state
        _, info = collect_report(candidate, sprint, upload['data'] if upload else None, drafts)
        counts = {}
        for source in info['sources'].values():
            counts[source] = counts.get(source, 0) + 1
        dataset = {k: v for k, v in upload.items() if k != 'data'} if upload else None
        return dict(sprint=sprint, closed=sprint <= state['plan']['cutoff'], dataset=dataset,
                    ready=not info['missing_teams'], task_sources=counts, **{k: info[k] for k in (
                        'team_sources', 'missing_teams', 'discrepancies', 'corrections', 'untouched')})

    def sprint_views(self, state):
        """Текущий спринт и, если есть, исправление последнего закрытого."""
        cutoff = state['plan']['cutoff']
        views = dict(current=self.sprint_readiness(state, cutoff + 1), correction=None)
        upload = state.get('sprint_uploads', {}).get(str(cutoff))
        if cutoff >= 1 and upload and not upload.get('applied'):
            views['correction'] = self.sprint_readiness(state, cutoff)
        return views

    def close_sprint(self, user, body):
        """Итоговый пересчёт: датасет и отчёты команд → факт спринта → один новый план."""
        self.org_right(user)
        state = self.load(user, body)
        if not state['baseline']:
            raise InputError('Сначала зафиксируйте исходный квартальный план')
        sprint = self.target_sprint(state, body.get('sprint'))
        upload = state.get('sprint_uploads', {}).get(str(sprint))
        drafts = state.get('fact_drafts', {}).get(str(sprint), {})
        if upload is None and not drafts:
            raise InputError(f'Для спринта {sprint} нет ни обновлённого датасета, ни отчётов команд')
        notes = []
        if upload:
            state['dataset'], notes = merge_dataset(state, upload['data'])
        report, info = collect_report(state, sprint, upload['data'] if upload else None, drafts)
        if info['missing_teams']:
            raise InputError('Нет выполненных SP для команд: ' + ', '.join(info['missing_teams']) +
                             '. Загрузите датасет с новой строкой Team_history или дождитесь их отчётов')
        dataset = {k: v for k, v in upload.items() if k not in ('data',)} if upload else None
        info = dict(info, notes=notes)
        if upload:
            # Загрузка учтена: блок исправления больше не показывает её как новую.
            upload['applied'] = True
        result = self.core.actual(dict(project_id=state['project_id'], revision=body['revision'], report=report,
                                       dataset=dataset, info=info), state=state)
        if result['revision'] == body['revision']:
            # Факт совпал с уже учтённым: план не меняется, фиксируем только то, что данные сверены.
            return self.store.save(state, body['revision'], f'Спринт {sprint}: загруженные данные совпадают с учтёнными')
        return result

    def explain(self, user):
        """Подробный разбор с контрфактами по текущим условиям."""
        self.admin(user)
        state = self.load(user)
        return explain_plan(state['dataset'], state['config'], state['actuals'])

    def scenarios(self, user):
        """Стресс-сценарии по текущим условиям; план не меняется."""
        self.admin(user)
        state = self.load(user)
        return stress_scenarios(state['dataset'], state['config'], state['actuals'], state['plan'])

    def what_if(self, user, body):
        """
        Что будет, если сотрудник выпадет на выбранные спринты.

        Только расчёт, без сохранения: доступно любой главе и для людей
        других команд — это проверка Bus Factor, а не отметка отсутствия.
        """
        self.admin(user)
        state = self.load(user)
        eid = body.get('engineer_id')
        if not any(p['id'] == eid for p in state['dataset']['engineers']):
            raise InputError('Сотрудник не найден')
        cutoff, quarter = state['plan']['cutoff'], state['config']['sprints']
        first, last = body.get('from_sprint', cutoff + 1), body.get('to_sprint', quarter)
        if type(first) is not int or type(last) is not int or not cutoff < first <= last <= quarter:
            raise InputError(f'Выберите незакрытые спринты квартала: {cutoff + 1}–{quarter}')
        start, _ = sprint_bounds(state['config'], first)
        _, end = sprint_bounds(state['config'], last)
        absence = dict(id='what-if', engineer=eid, start=start.isoformat(), end=end.isoformat(),
                       reason='Другое', comment='Проверка: что будет без сотрудника', created_by=user['name'], created_at=now())
        candidate = deepcopy(state)
        candidate['config'].setdefault('absences', []).append(absence)
        lost = absence_impact(candidate['dataset'], candidate['config'], absence)
        self.calculate(candidate, state['plan'], f'Проверка: {eid} недоступен в спринтах {first}–{last}')
        before = {i['id']: i['forecast_in_quarter'] for i in state['plan']['initiatives']}
        after = {i['id']: i for i in candidate['plan']['initiatives']}
        return dict(engineer_id=eid, from_sprint=first, to_sprint=last, lost=lost,
                    goals_lost=sorted(i for i, item in after.items() if before.get(i) and not item['forecast_in_quarter']),
                    goals_kept=sum(item['forecast_in_quarter'] for item in after.values()),
                    goals_total=len(after),
                    initiatives=[dict(id=i, end=item['end'], reason=item['reason']) for i, item in sorted(after.items())
                                 if before.get(i) and not item['forecast_in_quarter']],
                    changes=[c for c in candidate['changes'] if c['kind'] == 'schedule'],
                    risks=self.risk_summary(candidate))

    @staticmethod
    def risk_summary(state):
        """Риски ядра в полученном плане: красный, жёлтый, оранжевый."""
        return state['plan'].get('risks', [])

    def task(self, user, body):
        self.admin(user)
        state = self.load(user, body)
        old = state['plan']
        tid = body.get('task_id')
        task = self.own_task(user, state, tid)
        allowed = {'priority', 'remaining_sp', 'remaining_hours', 'assignments', 'not_before', 'required_skills'}
        changes = body.get('changes', {})
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise InputError('Неверные поля ручного управления')
        config = state['config']
        override = config['task_overrides'].setdefault(tid, {})
        for key in ('priority', 'remaining_sp', 'remaining_hours', 'required_skills'):
            if key in changes:
                override[key] = deepcopy(changes[key])
        override['after_sprint'] = state['plan']['cutoff']
        if 'assignments' in changes:
            value = changes['assignments']
            if not isinstance(value, dict):
                raise InputError('Укажите исполнителя по каждой роли')
            hours = override.get('remaining_hours', task['remaining_hours'])
            people = {p['id']: p for p in state['dataset']['engineers']}
            for role, eid in value.items():
                if role not in hours or eid not in people:
                    raise InputError('Неизвестная роль или сотрудник')
                has_team = people[eid]['allocations'].get(user['team'], 0) > 0
                has_help = any(t['engineer']==eid and t['to_team']==user['team'] and t['end']>state['plan']['cutoff'] for t in config['transfers'])
                if not has_team and not has_help:
                    raise AccessError('Для назначения сотрудника другой команды сначала согласуйте помощь')
            config.setdefault('assignments', {})[tid] = value
        if 'not_before' in changes:
            start = changes['not_before']
            if type(start) is not int or not state['plan']['cutoff'] < start <= state['config']['horizon']:
                raise InputError(f"Выберите незакрытый спринт 1–{state['config']['horizon']}")
            config.setdefault('not_before', {})[tid] = start
        self.calculate(state, old, f'Ручное управление {tid}')
        if body.get('preview'):
            return dict(preview=True, entry=state['plan']['entries'][tid], changes=state['changes'], risks=self.risk_summary(state))
        return self.store.save(state, body['revision'], f'{user["team"]}: изменена задача {tid}')

    def add_employee(self, user, body):
        self.admin(user)
        state = self.load(user, body)
        if body.get('team', user['team']) != user['team']:
            raise AccessError('Можно добавлять сотрудников только в свою команду')
        name = text(body.get('name'), 'Имя', 100)
        roles = body.get('roles')
        skills = body.get('skills', [])
        if not isinstance(roles, list) or not roles or len(roles)>20 or any(not isinstance(r,str) or not r.strip() or len(r)>100 for r in roles):
            raise InputError('Укажите подтверждённые роли сотрудника')
        if not isinstance(skills, list) or len(skills)>50 or any(not isinstance(s,str) or not s.strip() or len(s)>100 for s in skills):
            raise InputError('Проверьте список компетенций')
        rate = number(body.get('capacity_rate', 1), 'Доля занятости')
        if not 0 < rate <= 1:
            raise InputError('Доля занятости должна быть от 0 до 1, не включая 0')
        state['dataset']['engineers'].append(dict(id='ENG-'+uuid.uuid4().hex[:8].upper(), name=name,
            roles=sorted({role_name(r, known_roles(state['dataset'], state['config'])) for r in roles}), skills=sorted(set(skills)), grade=str(body.get('grade','Middle'))[:30], allocations={user['team']:rate}))
        self.calculate(state, state['plan'], 'Добавлен сотрудник')
        return self.store.save(state, body['revision'], f'{user["team"]}: добавлен сотрудник {name}')

    def add_task(self, user, body):
        self.admin(user)
        state = self.load(user, body)
        if body.get('team', user['team']) != user['team']:
            raise AccessError('Можно добавлять задачи только в свою команду')
        tid = str(body.get('task_id') or 'TASK-'+uuid.uuid4().hex[:8].upper()).strip()
        if not re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9_.-]{1,60}', tid) or tid in state['plan']['tasks']:
            raise InputError('Код задачи некорректен или уже занят')
        sp = number(body.get('sp'), 'SP')
        hours = numeric_map(body.get('hours'), 'Часы по ролям', known_roles(state['dataset'], state['config']))
        if not sp or not hours:
            raise InputError('Задаче нужны положительная оценка SP и часы хотя бы одной роли')
        predecessors = body.get('predecessors', [])
        if not isinstance(predecessors, list) or any(p not in state['plan']['tasks'] for p in predecessors):
            raise InputError('Выберите существующие задачи-предшественники')
        state['dataset']['tasks'].append(dict(id=tid, title=text(body.get('title'),'Название',300),
            initiative=text(body.get('initiative'),'Инициатива',80), team=user['team'], status='ToDo',
            parent='', priority=number(body.get('priority',50),'Приоритет'), sp=sp, original_hh=sum(hours.values()),
            hours=hours, spent={}, required_skills={}, planned_start='',planned_end='',actual_end='',
            result='',desired_result=str(body.get('desired_result',''))[:500],source_row=None))
        state['dataset']['dependencies'].extend(dict(first=p,second=tid,kind='has to be done before') for p in set(predecessors))
        self.calculate(state, state['plan'], f'Добавлена задача {tid}')
        return self.store.save(state, body['revision'], f'{user["team"]}: добавлена задача {tid}')

    def absence(self, user, body):
        """Отпуск, болезнь или обучение сотрудника: часы уменьшаются, план пересчитывается сразу.

        preview=True — только расчёт последствий, без сохранения.
        """
        self.admin(user)
        state = self.load(user, body)
        eid = body.get('engineer_id')
        person = next((p for p in state['dataset']['engineers'] if p['id'] == eid), None)
        if person is None or user['team'] not in person['allocations']:
            raise AccessError('Отсутствие отмечает глава команды, в которой работает сотрудник')
        try:
            start, end = date.fromisoformat(str(body.get('start'))), date.fromisoformat(str(body.get('end')))
        except ValueError:
            raise InputError('Укажите даты отсутствия') from None
        open_from, _ = sprint_bounds(state['config'], state['plan']['cutoff'] + 1)
        if end < open_from:
            raise InputError(f'Отсутствие должно захватывать незакрытые спринты: с {open_from.isoformat()}')
        absence = dict(id=uuid.uuid4().hex[:8], engineer=eid, start=max(start, open_from).isoformat(), end=end.isoformat(),
                       reason=body.get('reason'), comment=str(body.get('comment', ''))[:300],
                       created_by=user['name'], created_at=now())
        state['config'].setdefault('absences', []).append(absence)
        lost = absence_impact(state['dataset'], state['config'], absence)
        if not lost:
            raise InputError('На эти даты не приходится ни одного рабочего дня')
        label = (f'Отсутствие {eid}: {absence["reason"].lower()} {absence["start"]} — {absence["end"]}, '
                 f'−{sum(lost.values()):g} ч')
        self.calculate(state, state['plan'], label)
        if body.get('preview'):
            return dict(preview=True, changes=state['changes'], risks=self.risk_summary(state), lost=lost)
        return self.store.save(state, body['revision'], f'{user["team"]}: {label}')

    def remove_absence(self, user, body):
        """Отмена отсутствия: часы возвращаются, план пересчитывается."""
        self.admin(user)
        state = self.load(user, body)
        absences = state['config'].get('absences', [])
        found = next((a for a in absences if a['id'] == body.get('absence_id')), None)
        if found is None:
            raise InputError('Отсутствие не найдено')
        person = next((p for p in state['dataset']['engineers'] if p['id'] == found['engineer']), None)
        if person is None or user['team'] not in person['allocations']:
            raise AccessError('Отменить отсутствие может глава команды сотрудника')
        state['config']['absences'] = [a for a in absences if a is not found]
        label = f'Отменено отсутствие {found["engineer"]}: {found["start"]} — {found["end"]}'
        self.calculate(state, state['plan'], label)
        return self.store.save(state, body['revision'], f'{user["team"]}: {label}')

    def availability(self, user, body):
        self.admin(user)
        state=self.load(user,body)
        eid=body.get('engineer_id')
        person=next((p for p in state['dataset']['engineers'] if p['id']==eid),None)
        if not person or set(person['allocations']) != {user['team']}:
            raise AccessError('Для общей занятости в нескольких командах требуется согласование глав')
        values=body.get('hours')
        if not isinstance(values,dict) or any(str(s) not in {str(i) for i in range(state['plan']['cutoff']+1,13)} for s in values):
            raise InputError('Можно менять доступность только будущих спринтов')
        state['config']['availability'].setdefault(eid,{}).update(values)
        self.calculate(state,state['plan'],'Изменена доступность сотрудника')
        return self.store.save(state,body['revision'],f'{user["team"]}: доступность {eid}')

    def apply_request(self, state, payload):
        candidate = deepcopy(state)
        tasks = {t['id']:t for t in candidate['dataset']['tasks']}
        kind = payload.get('kind')
        teams = set(teams_of(candidate['dataset']))
        if kind in ('transfer','exchange'):
            tid,target = payload.get('task_id'),payload.get('to_team')
            if tid not in tasks or target not in teams:
                raise InputError('Выберите задачу и принимающую команду')
            source=tasks[tid]['team']
            if source==target or state['plan']['tasks'][tid]['status']=='Done':
                raise InputError('Передать можно незавершённую задачу другой команде')
            if payload.get('from_team',source)!=source:
                raise ConflictError('Команда задачи изменилась')
            tasks[tid]['team']=target
            candidate['config'].setdefault('assignments',{}).pop(tid,None)
            if kind=='exchange':
                other=payload.get('other_task_id')
                if other==tid or other not in tasks or tasks[other]['team']!=target or state['plan']['tasks'][other]['status']=='Done':
                    raise InputError('Для обмена выберите незавершённую задачу принимающей команды')
                tasks[other]['team']=source
                candidate['config']['assignments'].pop(other,None)
            involved=[source,target]
        elif kind=='help':
            transfer={k:payload.get(k) for k in ('engineer','from_team','to_team','hours','start','end')}
            if type(transfer['start']) is not int or transfer['start']<=state['plan']['cutoff']:
                raise InputError('Передавать часы можно только для будущих спринтов')
            candidate['config']['transfers'].append(transfer)
            involved=[transfer['from_team'],transfer['to_team']]
        else:
            raise InputError('Неизвестный вид согласования')
        self.calculate(candidate,state['plan'],'Согласованная передача между командами')
        return candidate,involved

    def propose(self,user,body):
        self.admin(user)
        state=self.load(user,body)
        payload=body.get('proposal',{})
        if not isinstance(payload,dict):
            raise InputError('Неверная заявка')
        if payload.get('kind') in ('transfer','exchange'):
            task=self.own_task(user,state,payload.get('task_id'))
            payload=dict(payload,from_team=task['team'])
        candidate,involved=self.apply_request(state,payload)
        if user['team'] not in involved:
            raise AccessError('Заявка должна затрагивать вашу команду')
        preview=dict(risks=self.risk_summary(candidate),changes=candidate['changes'])
        if body.get('preview'):
            return dict(preview=True,**preview)
        request=dict(id=uuid.uuid4().hex,kind=payload['kind'],payload=payload,teams=involved,
            approvals=[user['team']],status='pending',basis=basis(state),created=now(),author=user['name'],
            comment=str(body.get('comment',''))[:1000],preview=preview)
        state.setdefault('requests',[]).append(request)
        return self.store.save(state,body['revision'],f'{user["team"]}: отправлена заявка на согласование')

    def decide(self,user,body):
        self.admin(user)
        state=self.load(user,body)
        request=next((r for r in state.get('requests',[]) if r['id']==body.get('request_id')),None)
        if not request or user['team'] not in request['teams']:
            raise AccessError('Нет доступа к этой заявке')
        if request['status']!='pending':
            raise ConflictError('Заявка уже обработана')
        action=body.get('decision')
        if action=='reject':
            request.update(status='rejected',decided=now(),decided_by=user['name'])
        elif action in ('approve','refresh'):
            stale=request['basis']!=basis(state)
            if stale and action!='refresh':
                raise ConflictError('Условия плана изменились. Обновите расчёт заявки: согласования будут собраны заново')
            candidate,_=self.apply_request(state,request['payload'])
            if action=='refresh':
                request.update(basis=basis(state),approvals=[user['team']],preview=dict(risks=self.risk_summary(candidate),changes=candidate['changes']))
            else:
                if user['team'] in request['approvals']:
                    raise InputError('Ваше согласие уже учтено')
                request['approvals'].append(user['team'])
                if set(request['approvals'])==set(request['teams']):
                    request.update(status='approved',decided=now(),decided_by=user['name'])
                    state.update(dataset=candidate['dataset'],config=candidate['config'],plan=candidate['plan'],changes=candidate['changes'])
        else:
            raise InputError('Выберите согласовать или отклонить')
        return self.store.save(state,body['revision'],f'{user["team"]}: решение по заявке')

    def facts(self,user,body):
        self.admin(user)
        state=self.load(user,body)
        if not state['baseline']:
            raise InputError('Сначала зафиксируйте исходный квартальный план')
        report=deepcopy(body.get('report',{}))
        sprint=report.get('sprint')
        if type(sprint) is not int or not 1<=sprint<=state['config']['sprints']:
            raise InputError(f"Спринт должен быть от 1 до {state['config']['sprints']}")
        if body.get('team',user['team'])!=user['team'] or report.get('team',user['team'])!=user['team']:
            raise AccessError('Глава подтверждает факт только своей команды')
        if sprint!=state['plan']['cutoff']+1:
            prior=state.get('fact_drafts',{}).get(str(sprint),{}).get(user['team'])
            if prior and prior['achieved_sp']==report.get('achieved_sp'):
                if prior['tasks']==report.get('tasks') and prior['note']==report.get('note',''):
                    return state
            raise InputError('Этот спринт закрыт или ещё не наступил')
        tasks=self.team_tasks(state,user['team'])
        outcomes=report.get('tasks')
        if not isinstance(outcomes,list) or any(not isinstance(t,dict) for t in outcomes):
            raise InputError('Нужны результаты задач вашей команды')
        ids=[t.get('task_id') for t in outcomes]
        if len(ids)!=len(set(ids)) or set(ids)!={t['id'] for t in tasks}:
            raise InputError('Подтвердите все незавершённые задачи своей команды без повторов')
        for t in outcomes:
            if t.get('status') not in ('Done','ToDo','InProgress'):
                raise InputError('Неизвестный статус задачи')
            check_outcome(t, known_roles(state['dataset'], state['config']))
        clean=dict(sprint=sprint,achieved_sp=number(report.get('achieved_sp'),'Фактически выполненные SP команды'),
                   tasks=outcomes,note=str(report.get('note',''))[:1000],basis=fingerprint(tasks),submitted_by=user['name'])
        drafts=state.setdefault('fact_drafts',{}).setdefault(str(sprint),{})
        if drafts.get(user['team'])==clean:
            return state
        drafts[user['team']]=clean
        # Отчёт сохраняется без пересчёта: план пересчитывает отдельная
        # кнопка итогового пересчёта после обновления данных спринта.
        return self.store.save(state,body['revision'],f'{user["team"]}: сохранён отчёт об итогах спринта {sprint}')

    def fact_csv(self,user):
        self.admin(user)
        state=self.load(user)
        stream=io.StringIO(newline='')
        fields=['sprint','team_sp','task_id','status','remaining_sp','role','remaining_hours','spent_hours']
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';')
        writer.writeheader()
        # Остатки в шаблоне пустые: пустой остаток считается правилом ядра
        # по потраченным часам. Глава заполняет их, только если знает лучше.
        writer.writerow(dict(sprint=state['plan']['cutoff']+1,team_sp='',task_id='',status='',remaining_sp='',role='',remaining_hours='',spent_hours=''))
        for t in self.team_tasks(state,user['team']):
            for role in (t['remaining_hours'] or {'':0}):
                writer.writerow(dict(sprint=state['plan']['cutoff']+1,team_sp='',task_id=csv_text(t['id']),status=t['status'],
                    remaining_sp='',role=csv_text(role),remaining_hours='',spent_hours=0))
        return ('\ufeff'+stream.getvalue()).encode('utf-8')

    def parse_fact_csv(self,user,raw):
        self.admin(user)
        known=known_roles(self.load(user)['dataset'])
        try:
            content=raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise InputError('Сохраните CSV в кодировке UTF-8') from None
        if len(content)>2_000_000:
            raise InputError('CSV превышает 2 МБ')
        if not content.strip():
            raise InputError('CSV пуст')
        delimiter=';' if ';' in content.splitlines()[0] else ','
        reader=csv.DictReader(io.StringIO(content),delimiter=delimiter)
        expected={'sprint','team_sp','task_id','status','remaining_sp','role','remaining_hours','spent_hours'}
        if set(reader.fieldnames or [])!=expected:
            raise InputError('Столбцы CSV не совпадают с шаблоном. Скачайте новый шаблон')
        tasks,seen,sp_values,sprint_values={},{},set(),set()
        for line,row in enumerate(reader,2):
            if None in row or any(v is None for v in row.values()):
                raise InputError(f'Строка {line}: неверное число столбцов')
            sprint_value=number(row['sprint'],'Спринт')
            if not sprint_value.is_integer():
                raise InputError('Номер спринта должен быть целым')
            sprint_values.add(int(sprint_value))
            if row['team_sp'].strip():
                sp_values.add(number(row['team_sp'],'Факт SP команды'))
            tid,role=from_csv_text(row['task_id']).strip(),role_name(from_csv_text(row['role']),known)
            if not tid:
                continue
            if (tid,role) in seen:
                raise InputError(f'Строка {line}: повтор роли у задачи {tid}')
            seen[tid,role]=True
            status=row['status'].strip()
            if status not in ('Done','InProgress','ToDo'):
                raise InputError(f'{tid}: неизвестный статус {status}')
            remaining=optional_number(row['remaining_sp'].strip(),'Остаток SP')
            t=tasks.setdefault(tid,dict(task_id=tid,status=status,remaining_sp=remaining,remaining_hours={},spent_hours={}))
            if t['status']!=status or t['remaining_sp']!=remaining:
                raise InputError(f'{tid}: статус и остаток SP должны совпадать во всех строках')
            hours=optional_number(row['remaining_hours'].strip(),'Остаток часов') or 0
            spent=number(row['spent_hours'],'Затраченные часы',0)
            if not role and (hours or spent):
                raise InputError('Укажите роль для ненулевых часов')
            if role:
                if hours:t['remaining_hours'][role]=hours
                if spent:t['spent_hours'][role]=spent
        if len(sp_values)!=1 or len(sprint_values)!=1:
            raise InputError('Укажите один спринт и общий факт SP команды хотя бы в одной строке; повторные значения должны совпадать')
        state=self.load(user)
        sprint=sprint_values.pop()
        if sprint != state['plan']['cutoff']+1 or not 1<=sprint<=state['config']['sprints']:
            raise InputError('CSV должен содержать текущий незакрытый спринт. Скачайте новый шаблон')
        if set(tasks)!={t['id'] for t in self.team_tasks(state,user['team'])}:
            raise InputError('CSV должен содержать незавершённые задачи только вашей команды')
        for t in tasks.values():
            if t['status']=='Done' and (t['remaining_sp'] or t['remaining_hours']):
                raise InputError(f'{t["task_id"]}: у завершённой задачи должны быть нулевые остатки')
            if t['status']!='Done' and t['remaining_sp'] is not None and t['remaining_sp']<=0:
                raise InputError(f'{t["task_id"]}: у незавершённой задачи нужен положительный остаток SP')
        return dict(sprint=sprint,achieved_sp=sp_values.pop(),tasks=list(tasks.values()),note='Импорт CSV')

    def report_csv(self,user):
        state=self.load(user)
        view=self.view(user,state)
        out=io.StringIO(newline='')
        writer=csv.writer(out,delimiter=';')
        headers=['Задача','Название','Инициатива','Команда','Статус','Приоритет','Исходные SP','Остаток SP','Остаток часов',
                 'Исполнители','Начало (спринт)','Завершение (спринт)','Прогноз','Риски','В исходном плане']
        headers += [f'План SP, спринт {i}' for i in range(1,state['config']['sprints']+1)]
        headers += ['Потрачено часов всего'] + [f'Факт часов, спринт {i}' for i in range(1,state['config']['sprints']+1)]
        writer.writerow(headers)
        for t in view['tasks']:
            e=t['entry']
            fragments=t['history']+e['fragments']
            assignees=sorted({a['engineer'] for f in fragments for a in f['assignments']} | set(t['manual_assignments'].values()))
            start=min((f['sprint'] for f in fragments),default=e['start'])
            facts={f['sprint']:f for f in t['facts']}
            risks=e['reasons'] if e['status'] in ('blocked','partial','beyond') else []
            row=[t['id'],t['title'],t['initiative'],t['team'],t['status'],t['priority'],t['sp'],t['remaining_sp'],
                 round(sum(t['remaining_hours'].values()),3),', '.join(assignees),start or '',e['end'] if e['end'] is not None else '',
                 e['status'],'; '.join(risks),'Да' if state['baseline'] and t['initiative'] in state['baseline']['goal_ids'] else 'Нет']
            row += [round(sum(f['sp'] for f in fragments if f['sprint']==i),3) for i in range(1,state['config']['sprints']+1)]
            row += [round(sum(t['spent'].values()),3)]
            row += [round(sum(facts[i].get('spent_hours',{}).values()),3) if i in facts else '' for i in range(1,state['config']['sprints']+1)]
            writer.writerow([csv_text(v) for v in row])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
