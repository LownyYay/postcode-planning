"""Обезличенный снимок нового датасета для проверки интерфейса без браузера."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Расчётное ядро живёт в src/ корня репозитория.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
from planner.auth import Auth
from src.dataset import DATASET_PATH
from planner.service import Service
from planner.store import Store
from planner.team_service import TeamService


def fixtures():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'fixture.db')
        core = Service(store)
        state = core.initial(DATASET_PATH)
        state = core.approve(dict(project_id=state['project_id'], revision=state['revision']))
        service = TeamService(core, Auth(store))
        common = dict(project_id=state['project_id'], team='Team-L')
        head = dict(common, id='preview-head', login='preview-head', name='Глава Team-L', role='admin', engineer_id=None)
        member = dict(common, id='preview-member', login='preview-member', name='Сотрудник Team-L', role='member', engineer_id='ENG-401')
        return dict(admin=service.view(head, state), member=service.view(member, state))


if __name__ == '__main__':
    print(json.dumps(fixtures(), ensure_ascii=False))
