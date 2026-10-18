from pathlib import Path

import pandas as pd


# Excel хранит даты числом дней,
# отсчитываемых от 30 декабря 1899 года.
EXCEL_EPOCH = pd.Timestamp("1899-12-30")

TASK_DATE_COLUMNS = [
    "created_at",
    "planned_start",
    "planned_end",
    "actual_start",
    "actual_end",
]

# Справочники расположены под таблицами
# и задают допустимые значения статусов.
REFERENCE_BLOCKS = {
    "goal_options": "Итог разработки",
    "variance_reasons": "Причины расхождения планов",
    "closure_results": "Результаты работ при закрытии",
}


def find_row_by_text(
    dataframe: pd.DataFrame,
    search_text: str,
) -> int:
    """
    Ищет строку Excel, содержащую указанный текст.

    Возвращает индекс найденной строки.
    Индексы pandas начинаются с нуля.
    """

    for row_index, row in dataframe.iterrows():
        for value in row:
            if pd.isna(value):
                continue

            if search_text in str(value):
                return row_index

    raise ValueError(
        f"В Excel не найден блок: {search_text}"
    )


def load_raw_excel(
    path: Path,
    sheet_name: str,
) -> pd.DataFrame:
    """
    Читает весь лист Excel без выбора общей
    строки заголовков.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Файл не найден: {path}"
        )

    return pd.read_excel(
        path,
        sheet_name=sheet_name,
        header=None,
    )


def to_datetime_column(
    values: pd.Series,
) -> pd.Series:
    """
    Приводит столбец с датами к типу даты.

    Часть ячеек читается сразу как дата,
    часть остаётся числом дней Excel,
    поэтому обрабатываются оба варианта.
    """

    serial_numbers = pd.to_numeric(
        values,
        errors="coerce",
    )

    from_serial = (
        EXCEL_EPOCH
        + pd.to_timedelta(
            serial_numbers,
            unit="D",
        )
    )

    from_value = pd.to_datetime(
        values,
        errors="coerce",
    )

    return from_serial.fillna(from_value)


def split_skills(
    value: object,
) -> list[str]:
    """
    Разбирает перечень технологий инженера
    в список отдельных навыков.
    """

    if pd.isna(value):
        return []

    text = str(value).strip()

    if not text or text == "nan":
        return []

    # Режем только по запятым вне скобок:
    # «CI/CD (GitLab, Jenkins)» — один навык.
    skills = []
    current = []
    depth = 0

    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)

        if char == "," and depth == 0:
            skills.append("".join(current))
            current = []
        else:
            current.append(char)

    skills.append("".join(current))

    return [
        skill.strip()
        for skill in skills
        if skill.strip()
    ]


def extract_tasks(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает таблицу Tasks из общего листа Excel.
    """

    tasks_title_row = find_row_by_text(
        raw,
        "Tasks (",
    )

    spent_title_row = find_row_by_text(
        raw,
        "Spent_time_roles",
    )

    # Заголовки находятся на следующей строке
    # после названия блока Tasks.
    header_row = tasks_title_row + 1

    # Получаем только заполненные заголовки.
    headers = (
        raw.iloc[header_row]
        .dropna()
        .astype(str)
        .tolist()
    )

    # Берём строки после заголовков
    # и до начала следующего блока.
    tasks = raw.iloc[
        header_row + 1 : spent_title_row,
        : len(headers),
    ].copy()

    tasks.columns = headers

    # Строка считается задачей, только если
    # у неё заполнен task_id.
    tasks = tasks.dropna(
        subset=["task_id"]
    )

    # Очищаем текстовые идентификаторы от пробелов.
    tasks["task_id"] = (
        tasks["task_id"]
        .astype(str)
        .str.strip()
    )

    tasks["team_id"] = (
        tasks["team_id"]
        .astype(str)
        .str.strip()
    )

    tasks["status"] = (
        tasks["status"]
        .astype(str)
        .str.strip()
    )

    # Приводим плановые и фактические даты
    # к настоящим датам.
    for column in TASK_DATE_COLUMNS:
        if column not in tasks.columns:
            continue

        tasks[column] = to_datetime_column(
            tasks[column]
        )

    return tasks.reset_index(drop=True)


def extract_estimates(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает матрицу сметы в человеко-часах.

    В исходном Excel:
    - строки являются ролями;
    - столбцы являются задачами;
    - значения являются количеством часов.

    Функция преобразует матрицу в таблицу:
    task_id | role | estimated_hours
    """

    estimates_title_row = find_row_by_text(
        raw,
        "Estimates.",
    )

    dependencies_title_row = find_row_by_text(
        raw,
        "Task_dependencies",
    )

    # Заголовки сметы находятся на следующей
    # строке после названия блока.
    header_row = estimates_title_row + 1

    # Находим последний заполненный столбец
    # в строке заголовков сметы.
    last_column = (
        raw.iloc[header_row]
        .last_valid_index()
    )

    if last_column is None:
        raise ValueError(
            "В блоке Estimates не найдены заголовки"
        )

    # Берём строки с ролями до начала
    # блока зависимостей.
    estimates = raw.iloc[
        header_row + 1 : dependencies_title_row,
        : last_column + 1,
    ].copy()

    # Получаем названия столбцов:
    # первый столбец — Роль,
    # остальные — task_id.
    headers = (
        raw.iloc[
            header_row,
            : last_column + 1,
        ]
        .astype(str)
        .tolist()
    )

    estimates.columns = headers

    # Удаляем пустые строки.
    estimates = estimates.dropna(
        subset=["Роль"]
    )

    # Итоговая строка не является ролью.
    estimates = estimates[
        estimates["Роль"] != "Итого по ролям"
    ]

    # Преобразуем широкую матрицу:
    #
    # Роль | DB-201 | DB-202
    #
    # в длинную таблицу:
    #
    # task_id | role | estimated_hours
    estimates_long = estimates.melt(
        id_vars=["Роль"],
        var_name="task_id",
        value_name="estimated_hours",
    )

    estimates_long = estimates_long.rename(
        columns={
            "Роль": "role",
        }
    )

    # Преобразуем часы в числа.
    # Пустые или некорректные значения
    # временно превращаем в нули.
    estimates_long["estimated_hours"] = (
        pd.to_numeric(
            estimates_long["estimated_hours"],
            errors="coerce",
        )
        .fillna(0)
    )

    # Оставляем только реальные требования.
    # Нулевые часы нам не нужны.
    estimates_long = estimates_long[
        estimates_long["estimated_hours"] > 0
    ]

    # Убираем случайные пробелы.
    estimates_long["task_id"] = (
        estimates_long["task_id"]
        .astype(str)
        .str.strip()
    )

    estimates_long["role"] = (
        estimates_long["role"]
        .astype(str)
        .str.strip()
    )

    return estimates_long.reset_index(drop=True)

def extract_spent_hours(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает фактически затраченные часы
    по ролям для задач InProgress.

    Результат:
    task_id | role | spent_hours
    """

    spent_title_row = find_row_by_text(
        raw,
        "Spent_time_roles",
    )

    estimates_title_row = find_row_by_text(
        raw,
        "Estimates.",
    )

    # Ищем строку заголовков внутри блока.
    header_row = None

    for row_index in range(
        spent_title_row + 1,
        estimates_title_row,
    ):
        first_cell = raw.iloc[row_index, 0]

        if (
            not pd.isna(first_cell)
            and str(first_cell).strip() == "task_id"
        ):
            header_row = row_index
            break

    if header_row is None:
        raise ValueError(
            "В блоке Spent_time_roles "
            "не найдена строка заголовков"
        )

    last_column = (
        raw.iloc[header_row]
        .last_valid_index()
    )

    if last_column is None:
        raise ValueError(
            "В блоке Spent_time_roles "
            "не найдены столбцы"
        )

    spent = raw.iloc[
        header_row + 1 : estimates_title_row,
        : last_column + 1,
    ].copy()

    headers = (
        raw.iloc[
            header_row,
            : last_column + 1,
        ]
        .astype(str)
        .tolist()
    )

    spent.columns = headers

    # Удаляем пустые строки.
    spent = spent.dropna(
        subset=["task_id"]
    )

    # «Исходное время» не является ролью.
    role_columns = [
        column
        for column in spent.columns
        if column not in {
            "task_id",
            "Исходное время",
        }
    ]

    # Превращаем широкую матрицу
    # в список затраченных часов.
    spent_long = spent.melt(
        id_vars=["task_id"],
        value_vars=role_columns,
        var_name="role",
        value_name="spent_hours",
    )

    spent_long["spent_hours"] = (
        pd.to_numeric(
            spent_long["spent_hours"],
            errors="coerce",
        )
        .fillna(0)
    )

    # Оставляем только реально затраченные часы.
    spent_long = spent_long[
        spent_long["spent_hours"] > 0
    ]

    spent_long["task_id"] = (
        spent_long["task_id"]
        .astype(str)
        .str.strip()
    )

    spent_long["role"] = (
        spent_long["role"]
        .astype(str)
        .str.strip()
    )

    return spent_long.reset_index(drop=True)

def extract_engineers(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает профили инженеров.

    Одна строка означает распределение инженера
    в конкретную команду.

    Один инженер может встречаться несколько раз,
    если он разделён между командами.
    """

    engineers_title_row = find_row_by_text(
        raw,
        "Engineers_profiles",
    )

    history_title_row = find_row_by_text(
        raw,
        "Team_history",
    )

    # Заголовки расположены на следующей строке
    # после названия блока.
    header_row = engineers_title_row + 1

    last_column = (
        raw.iloc[header_row]
        .last_valid_index()
    )

    if last_column is None:
        raise ValueError(
            "В блоке Engineers_profiles "
            "не найдены заголовки"
        )

    engineers = raw.iloc[
        header_row + 1 : history_title_row,
        : last_column + 1,
    ].copy()

    headers = (
        raw.iloc[
            header_row,
            : last_column + 1,
        ]
        .astype(str)
        .tolist()
    )

    engineers.columns = headers

    # Удаляем пустые строки.
    engineers = engineers.dropna(
        subset=["engineer_id"]
    )

    # Очищаем текстовые поля от пробелов.
    text_columns = [
        "engineer_id",
        "team_id",
        "role",
        "grade",
        "skills_declared",
    ]

    for column in text_columns:
        engineers[column] = (
            engineers[column]
            .astype(str)
            .str.strip()
        )

    # Преобразуем ставку в число.
    # Если значение некорректно, получится NaN.
    # Позже валидатор покажет такую ошибку.
    engineers["capacity_rate"] = (
        pd.to_numeric(
            engineers["capacity_rate"],
            errors="coerce",
        )
    )

    # Стек технологий нужен списком:
    # на нём строится звёздная карта.
    engineers["skills"] = (
        engineers["skills_declared"]
        .apply(split_skills)
    )

    return engineers.reset_index(drop=True)


def extract_team_history(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает историю производительности команд.

    Результат содержит:
    - дату спринта;
    - команду;
    - фактически выполненные SP;
    - первоначально запланированные SP.
    """

    history_title_row = find_row_by_text(
        raw,
        "Team_history",
    )

    next_section_row = find_row_by_text(
        raw,
        "Итог разработки",
    )

    # Заголовки находятся на следующей строке
    # после названия блока.
    header_row = history_title_row + 1

    last_column = (
        raw.iloc[header_row]
        .last_valid_index()
    )

    if last_column is None:
        raise ValueError(
            "В блоке Team_history "
            "не найдены заголовки"
        )

    history = raw.iloc[
        header_row + 1 : next_section_row,
        : last_column + 1,
    ].copy()

    headers = (
        raw.iloc[
            header_row,
            : last_column + 1,
        ]
        .astype(str)
        .tolist()
    )

    history.columns = headers

    # Удаляем пустые строки.
    history = history.dropna(
        subset=["team_id"]
    )

    # Очищаем названия команд.
    history["team_id"] = (
        history["team_id"]
        .astype(str)
        .str.strip()
    )

    # Преобразуем дату.
    history["snapshot_date"] = (
        pd.to_datetime(
            history["snapshot_date"],
            errors="coerce",
        )
    )

    # Преобразуем SP в числа.
    history["velocity_achieved"] = (
        pd.to_numeric(
            history["velocity_achieved"],
            errors="coerce",
        )
    )

    history["planned_sp"] = (
        pd.to_numeric(
            history["planned_sp"],
            errors="coerce",
        )
    )

    return history.reset_index(drop=True)

def extract_dependencies(
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Извлекает зависимости между задачами.

    В исходном Excel заголовки блока смещены:
    данные фактически расположены в первых
    трёх столбцах.

    Временное допущение:
    первая задача блокирует вторую.
    """

    dependencies_title_row = find_row_by_text(
        raw,
        "Task_dependencies",
    )

    engineers_title_row = find_row_by_text(
        raw,
        "Engineers_profiles",
    )

    # Заголовок расположен сразу после
    # названия блока.
    header_row = dependencies_title_row + 1

    # Из-за смещения заголовков берём
    # первые три фактически заполненных столбца.
    dependencies = raw.iloc[
        header_row + 1 : engineers_title_row,
        0:3,
    ].copy()

    dependencies.columns = [
        "blocking_task_id",
        "blocked_task_id",
        "dependency_type",
    ]

    # Удаляем пустые строки.
    dependencies = dependencies.dropna(
        subset=[
            "blocking_task_id",
            "blocked_task_id",
        ]
    )

    text_columns = [
        "blocking_task_id",
        "blocked_task_id",
        "dependency_type",
    ]

    for column in text_columns:
        dependencies[column] = (
            dependencies[column]
            .astype(str)
            .str.strip()
        )

    return dependencies.reset_index(drop=True)


def extract_reference_lists(
    raw: pd.DataFrame,
) -> dict[str, list[str]]:
    """
    Извлекает справочники под таблицами листа.

    Результат содержит три списка:
    - goal_options - варианты выбора цели работ;
    - variance_reasons - причины расхождения
      планов заказчика и исполнителя;
    - closure_results - результаты работ
      при закрытии задачи.
    """

    title_rows = {
        name: find_row_by_text(
            raw,
            search_text,
        )
        for name, search_text in REFERENCE_BLOCKS.items()
    }

    result: dict[str, list[str]] = {}

    for name, title_row in title_rows.items():
        # Справочник заканчивается там,
        # где начинается следующий.
        following_rows = [
            row
            for row in title_rows.values()
            if row > title_row
        ]

        end_row = (
            min(following_rows)
            if following_rows
            else len(raw)
        )

        values = (
            raw.iloc[
                title_row + 1 : end_row,
                0,
            ]
            .dropna()
            .astype(str)
            .str.strip()
        )

        result[name] = [
            value
            for value in values.tolist()
            if value
        ]

    return result