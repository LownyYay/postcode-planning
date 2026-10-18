import re
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
ALIASES_PATH = PROJECT_DIR / "config" / "aliases.yaml"

# Разделители, которыми одно и то же название роли пишут по-разному:
# пробелы любой ширины, подчёркивание, дефисы и тире, точка, средняя точка.
# «Дев_опс», «Дев опс», «Дев-опс», «ДевОпс» и «девопс» дают один ключ.
ROLE_SEPARATORS = re.compile(r"[\s_\-\u2010-\u2015.\u00b7]+")


def role_key(role: object) -> str:
    """
    Ключ сравнения роли: без разделителей, без регистра, «ё» как «е».

    Два названия с одинаковым ключом — одна и та же роль,
    записанная разными способами.
    """

    return ROLE_SEPARATORS.sub("", str(role)).casefold().replace("ё", "е")


def clean_role(role: object) -> str:
    """
    Название роли без лишних пробелов по краям и внутри.
    """

    return " ".join(str(role).split())


def load_role_aliases(
    path: Path = ALIASES_PATH,
) -> dict[str, str]:
    """
    Загружает словарь вариантов названий ролей.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Файл алиасов не найден: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:
        config = yaml.safe_load(file) or {}

    aliases = config.get("roles", {})

    if not isinstance(aliases, dict):
        raise ValueError(
            "Раздел roles в aliases.yaml "
            "должен быть словарём"
        )

    return {
        str(source).strip(): str(target).strip()
        for source, target in aliases.items()
    }


def normalize_role(
    role: object,
    aliases: dict[str, str],
) -> object:
    """
    Нормализует одно название роли.
    """

    if pd.isna(role):
        return role

    cleaned_role = clean_role(role)

    if cleaned_role in aliases:
        return aliases[cleaned_role]

    # Написание, которого нет в словаре дословно, но которое совпадает
    # по ключу с вариантом или с целевым названием из словаря.
    key = role_key(cleaned_role)

    for source, target in aliases.items():
        if key in (role_key(source), role_key(target)):
            return target

    return cleaned_role


def unify_roles(
    values,
    aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Сводит написания одной роли к одному названию.

    values — все встреченные названия, с повторами: по ним выбирается
    основное написание. Если группа есть в словаре aliases, берётся
    его целевое название; иначе — самое частое написание, при равенстве —
    первое по алфавиту. Возвращает словарь «написание → название».
    """

    aliases = aliases or {}
    groups: dict[str, Counter] = {}

    for value in values:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            continue

        cleaned = clean_role(value)
        groups.setdefault(role_key(cleaned), Counter())[cleaned] += 1

    targets = {role_key(target): target for target in aliases.values()}
    mapping: dict[str, str] = {}

    for key, spellings in groups.items():
        canonical = targets.get(key) or min(
            spellings,
            key=lambda spelling: (-spellings[spelling], spelling),
        )

        for spelling in spellings:
            mapping[spelling] = canonical

    return mapping


def unify_role_columns(
    frames: list[pd.DataFrame],
    aliases: dict[str, str] | None = None,
) -> list[pd.DataFrame]:
    """
    Одинаковые по ключу роли во всех таблицах получают одно название.

    Сводить нужно по всем таблицам сразу: иначе «Девопс» в смете
    и «ДевОпс» в профилях инженеров останутся разными ролями, и работу
    будет некому назначить.
    """

    values = [role for frame in frames for role in frame["role"].tolist()]
    mapping = unify_roles(values, aliases)
    result = []

    for frame in frames:
        copy = frame.copy()
        copy["role"] = copy["role"].apply(
            lambda role: mapping.get(clean_role(role), role)
            if isinstance(role, str)
            else role
        )
        result.append(copy)

    return result


def normalize_role_column(
    dataframe: pd.DataFrame,
    aliases: dict[str, str],
) -> pd.DataFrame:
    """
    Возвращает копию таблицы с нормализованным
    столбцом role.
    """

    if "role" not in dataframe.columns:
        raise ValueError(
            "В таблице отсутствует столбец role"
        )

    result = dataframe.copy()

    result["role"] = result["role"].apply(
        lambda role: normalize_role(
            role,
            aliases,
        )
    )

    return result