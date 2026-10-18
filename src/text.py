"""
Числа в тексте причин и рисков: «1 спринт», «2 спринта», «5 спринтов».
"""


def plural(number: int, one: str, few: str, many: str) -> str:
    """
    Форма слова для целого числа по правилам русского языка.
    """

    tail = abs(number) % 100

    if 11 <= tail <= 14:
        return many

    tail %= 10

    if tail == 1:
        return one

    if 2 <= tail <= 4:
        return few

    return many


def sprints(number: int) -> str:
    """
    «3 спринта» — число со словом «спринт» в нужной форме.
    """

    return f"{number} {plural(number, 'спринт', 'спринта', 'спринтов')}"


# Фразы, которые не объясняют ресурсную причину: вердикт, прогноз, совет.
GENERIC_PREFIXES = (
    "Задача выполнима",
    "Включение задачи",
    "Её инициатива",
    "Прогноз",
    "Рекомендация",
    "В горизонте",
    "Задача уже в работе",
)


def first_sentences(texts, limit: int = 2) -> list[str]:
    """
    Первые содержательные фразы причин без повторов:
    «Team-Platform перегружена: 42.1 SP спроса при 34.6 за квартал.»
    Вердикты и прогнозы пропускаются — они не называют причину.
    """

    result: list[str] = []

    for text in texts:
        if not text:
            continue

        for sentence in text.split(". "):
            sentence = sentence.strip().rstrip(".") + "."

            if sentence.startswith(GENERIC_PREFIXES) or sentence == ".":
                continue

            if sentence not in result:
                result.append(sentence)

            break

        if len(result) == limit:
            break

    return result
