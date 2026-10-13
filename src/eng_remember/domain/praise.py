"""Praise after an answer, and the rule that keeps it from repeating.

Two things live here: the text bank, and a selector that behaves like a shuffled
bag rather than a random pick. Each template is used once before any of them is
used again, and the template that closed a cycle is excluded from the start of
the next one — so a learner never sees the same praise twice in a row, and with
~140 templates never sees a repeat for a long stretch of reviews.

Templates are placeholders filled by :func:`render`; ``{term}`` and ``{interval}``
are the only ones the regular banks use, ``{streak}`` belongs to milestones.

Every line must be gender-neutral: the bot does not know whether "ты запомнил"
or "ты запомнила" is right, so it never says either.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BANKS",
    "PraiseCategory",
    "PraisePick",
    "category_for_rating",
    "render",
    "select",
]

__all__ += ["PraiseHistory", "empty_history"]


class PraiseCategory(StrEnum):
    """Which bank to draw from. ``MILESTONE`` replaces the others on the day."""

    GOOD = "good"
    EASY = "easy"
    HARD = "hard"
    AGAIN = "again"
    MILESTONE = "milestone"


# --- the banks ---------------------------------------------------------------

_GOOD: tuple[str, ...] = (
    "«{term}» возвращается через {interval} — слово уходит в долгую память.",
    "Отлично! Следующий раз через {interval}.",
    "Есть! «{term}» закрепляется.",
    "Слово легло в память. Вернёмся к нему через {interval}.",
    "Хорошая работа — интервал вырос до {interval}.",
    "«{term}» теперь вернётся только через {interval}.",
    "Точно в цель. Через {interval} проверим ещё раз.",
    "Так и растёт словарный запас — по одному слову за раз.",
    "Запомнено. Пауза до следующего повторения: {interval}.",
    "Верный ответ. Следующая остановка — через {interval}.",
    "«{term}» держится. Интервал: {interval}.",
    "Ровно то, что нужно. Через {interval} посмотрим снова.",
    "Мозг поставил галочку. Возврат через {interval}.",
    "Крепко. Следующее повторение через {interval}.",
    "Хорошо. Слово уходит в тень на {interval}.",
    "«{term}» записано в активный словарь.",
    "Верно. Дальше — {interval} без этого слова.",
    "Отличный ответ, интервал вырос до {interval}.",
    "Держи темп. Следующий заход через {interval}.",
    "Слово осело. Вернёмся через {interval}.",
    "Прошло гладко. Пауза — {interval}.",
    "«{term}» становится привычным.",
    "Аккуратно и по делу. Через {interval} — проверка.",
    "Ещё одно слово ближе к автоматизму. Интервал {interval}.",
    "Верно! В следующий раз оно вернётся через {interval}.",
    "Слово в кармане. Следующий раз — через {interval}.",
    "Чисто. Через {interval} напомню.",
    "Прогресс налицо: {interval} до следующей встречи.",
    "«{term}» держится крепче, чем в прошлый раз.",
    "Хороший знак. Отложил на {interval}.",
    "Запомнено надёжно. Интервал: {interval}.",
    "Отлично — можно не думать об этом слове {interval}.",
    "Так держать. Следующее повторение через {interval}.",
    "Знание закрепилось. Пауза {interval}.",
    "Правильно. Оно уже почти твоё.",
    "«{term}» переходит на длинную дистанцию: {interval}.",
    "Ровный ответ. Через {interval} сверимся.",
    "Слово улеглось. Возврат через {interval}.",
    "Есть прогресс — интервал теперь {interval}.",
    "Записано. Дальше по расписанию: {interval}.",
    "То, что надо. «{term}» ждёт {interval}.",
    "Спокойно и верно. Следующий раз через {interval}.",
    "Минус одно слово из списка сложных.",
    "Хороший ответ. Отдых до {interval}.",
    "Верно — и интервал это подтверждает: {interval}.",
)

_EASY: tuple[str, ...] = (
    "Легко и уверенно — «{term}» можно отложить на {interval}.",
    "Такое не забывается. Пауза {interval}.",
    "Мгновенный ответ. Интервал: {interval}.",
    "«{term}» явно знакомо. Вернёмся через {interval}.",
    "Без запинки! Следующее повторение через {interval}.",
    "Уверенное знание — {interval} в запасе.",
    "Слишком просто для тебя. Отложил на {interval}.",
    "Чистая работа. Через {interval} проверим, не выветрилось ли.",
    "«{term}» в активном словаре. Интервал {interval}.",
    "Легко. Дальше — {interval}.",
    "Ответ без раздумий — это и есть автоматизм.",
    "Отложено на {interval}. «{term}» явно прижилось.",
    "Быстро и верно. Следующий раз через {interval}.",
    "Такие слова можно переспрашивать редко: {interval}.",
    "Уверенно. Пауза до {interval}.",
    "«{term}» уже твоё. Вернёмся через {interval}.",
    "Молчание вместо поиска в памяти — хороший признак.",
    "Легко — значит, интервал можно растянуть до {interval}.",
    "Ответ как выстрел. Через {interval} снова.",
    "Прочно. {interval} без повторов.",
    "Знание не требует усилий — отличный результат.",
    "«{term}» прошло проверку на автомате.",
    "Лёгкий ответ, длинный интервал: {interval}.",
    "Уверенно и быстро. Следующая встреча через {interval}.",
    "Отлично — это слово уже не занимает место в голове.",
)

_HARD: tuple[str, ...] = (
    "Было трудно, и всё равно верно. Уважение.",
    "«{term}» далось с усилием — именно так оно и запоминается.",
    "Сквозь сопротивление, но ответ есть. Интервал {interval}.",
    "Трудные слова помнятся лучше всего. Через {interval}.",
    "Медленно, зато своими силами. Пауза {interval}.",
    "Это был нелёгкий ответ, и он засчитан.",
    "«{term}» сопротивляется — значит, ещё немного и сдастся.",
    "С усилием, но вспомнилось. Интервал {interval}.",
    "Трудное даётся дольше — и держится крепче.",
    "Вытащено из памяти с боем. Именно этот процесс и работает.",
    "Не быстро, но верно. Дальше {interval}.",
    "«{term}» почти улеглось. Через {interval} проверим.",
    "Усилие важнее скорости. Следующий раз через {interval}.",
    "Ответ через сопротивление — это дороже лёгкого ответа.",
    "Тяжело пошло, зато вспомнилось без подсказки.",
    "«{term}» пока держится на усилии. Интервал {interval}.",
    "Медленный ответ лучше отсутствующего.",
    "Трудно — и всё равно есть. Пауза {interval}.",
    "Вот такие слова и строят настоящий запас.",
    "Ещё пара таких ответов — и «{term}» станет лёгким.",
    "Сопротивление есть, прогресс тоже.",
    "Вспомнилось своими силами, пусть и не сразу.",
    "Трудный ответ засчитан. Через {interval} снова.",
    "Интервал {interval} — честная награда за усилие.",
    "Трудно сегодня, привычно завтра.",
)

_AGAIN: tuple[str, ...] = (
    "Ничего страшного — «{term}» вернётся через {interval}.",
    "Забыть — часть процесса. Скоро повторим.",
    "Это не провал, а сигнал: слово нужно ещё раз.",
    "«{term}» вернётся через {interval}. Так и надо.",
    "Ошибиться здесь полезнее, чем не ответить вовсе.",
    "Память сама подскажет, когда вспомнить легче: {interval}.",
    "Пока не сегодня. Через {interval} попробуем снова.",
    "Оно ускользнуло — вернём в оборот через {interval}.",
    "Пропуск в памяти — обычное дело на этом этапе.",
    "Слово вернётся скоро. {interval} — и снова в бой.",
    "Забывание и есть то, против чего мы работаем.",
    "Не вспомнилось сейчас — вспомнится через {interval}.",
    "Так бывает у всех. «{term}» ждёт {interval}.",
    "Ещё не закреплено. Через {interval} вернёмся.",
    "Промах засчитан, слово остаётся в работе.",
    "Пауза короткая: {interval}, и снова проверим.",
    "Память требует повтора — дадим ей его.",
    "Ошибка дешевле, чем кажется: интервал пересчитан.",
    "«{term}» пока не твоё. Но станет.",
    "Ничего не потеряно — карточка просто вернётся раньше.",
    "Сейчас не получилось, через {interval} получится.",
    "Тут нужен ещё один заход. Скоро.",
    "Забывание — не приговор, а расписание.",
    "Вернёмся к нему через {interval} и закрепим.",
    "Не страшно. Дорогу осилит идущий.",
)

_MILESTONE: tuple[str, ...] = (
    "{streak} дней подряд. Это уже привычка.",
    "Серия {streak} дней! Так и держится дисциплина.",
    "{streak} дней без пропусков — впечатляет.",
    "Есть {streak} дней подряд. Ровно то, что работает.",
    "Серия {streak}. Пропуск теперь стоит дорого.",
    "«{streak} дней» — звучит как результат. Так и есть.",
    "{streak} дней подряд — слово за словом, день за днём.",
    "Серия {streak} дней. Это уже не случайность.",
    "{streak} дней подряд! Регулярность важнее объёма.",
    "Серия {streak}. С каждым днём словарь крепче.",
    "{streak} дней подряд — лучший показатель, чем число выученных слов.",
    "Серия {streak} дней. Так держать и дальше.",
    "{streak} дней без перерывов. Впечатляющая настойчивость.",
    "Серия {streak}. Немногие доходят до этой отметки.",
    "{streak} дней подряд — и это только начало.",
    "Серия {streak} дней! Память благодарит за регулярность.",
    "{streak} дней подряд. Заслуженная отметка.",
    "Серия {streak} дней — теперь её жалко терять.",
    "{streak} дней подряд. Отличный темп.",
    "Серия {streak}. Ежедневность делает всю работу.",
)

BANKS: Mapping[PraiseCategory, tuple[str, ...]] = {
    PraiseCategory.GOOD: _GOOD,
    PraiseCategory.EASY: _EASY,
    PraiseCategory.HARD: _HARD,
    PraiseCategory.AGAIN: _AGAIN,
    PraiseCategory.MILESTONE: _MILESTONE,
}


# --- selection ---------------------------------------------------------------

# category -> indices already used in the current cycle, oldest first.
PraiseHistory = dict[str, list[int]]


def empty_history() -> PraiseHistory:
    return {}


@dataclass(frozen=True, slots=True)
class PraisePick:
    """One selected template plus the history it must be recorded into."""

    text: str
    key: str
    category: PraiseCategory
    history: PraiseHistory


def category_for_rating(rating: str) -> PraiseCategory:
    """Map a review rating onto the bank that fits it.

    ``again`` is not praise: nothing went right, so the user gets support rather
    than a compliment. Congratulating a miss would read as not paying attention.
    """
    return {
        "good": PraiseCategory.GOOD,
        "easy": PraiseCategory.EASY,
        "hard": PraiseCategory.HARD,
        "again": PraiseCategory.AGAIN,
    }.get(rating, PraiseCategory.GOOD)


def select(
    category: PraiseCategory,
    history: PraiseHistory | None = None,
    *,
    rng: random.Random | None = None,
) -> PraisePick:
    """Draw a template that has not been used yet in the current cycle.

    Works like a shuffled bag: every template in the bank is used once before any
    repeats, so the praise cannot become predictable. When the bag empties it is
    refilled with the previous pick excluded, which also removes the one place a
    repeat could otherwise slip through — the seam between two cycles.
    """
    bank = BANKS[category]
    source = history or {}
    used = [index for index in source.get(category.value, []) if 0 <= index < len(bank)]

    chosen = rng or random
    available = [index for index in range(len(bank)) if index not in set(used)]
    if not available:
        last = used[-1] if used else None
        available = [index for index in range(len(bank)) if index != last]
        used = [last] if last is not None else []

    index = chosen.choice(available)
    updated: PraiseHistory = {**source, category.value: [*used, index]}
    return PraisePick(
        text=bank[index],
        key=f"{category.value}:{index}",
        category=category,
        history=updated,
    )


# --- rendering ---------------------------------------------------------------


class _SafeValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        # An unknown placeholder stays visible instead of raising inside a
        # handler: a typo in one of ~140 templates must not break a review.
        return "{" + key + "}"


def render(template: str, **values: str) -> str:
    """Fill placeholders, leaving unknown ones as they are."""
    return template.format_map(_SafeValues(**values))
