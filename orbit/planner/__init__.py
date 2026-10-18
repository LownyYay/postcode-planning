"""ОРБИТА: объяснимое планирование команд.

Расчёт плана делает ядро из каталога src/ в корне репозитория.
Пакет сам добавляет корень в путь импорта: иначе ядро находилось бы
только при запуске через app.py, а тесты через unittest и скрипты
падали бы на импорте.
"""

import sys
from pathlib import Path

_PROJECT_DIR = str(Path(__file__).resolve().parents[2])

if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# 3.0 — расчёт через ядро src/ вместо собственного эвристического планировщика.
__version__ = "3.0.0"
