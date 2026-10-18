"""Пути до пакета orbit и расчётного ядра src/.

Тесты ОРБИТЫ импортируют planner и app из каталога orbit, а planner
опирается на ядро из корня репозитория. Без этого файла порядок
импорта тестов влиял бы на то, найдётся ли src.
"""

import sys
from pathlib import Path

ORBIT_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = ORBIT_DIR.parent

for path in (PROJECT_DIR, ORBIT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
