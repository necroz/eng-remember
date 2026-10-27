"""Every string the user sees.

Collected in one module so the wording can be reviewed as a whole and so the
HTML parse mode never has to be guessed at from inside a handler. Dynamic values
are escaped here, at the point of interpolation — a word the user typed must not
be able to break the markup.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from eng_remember.domain.answers import Verdict
from eng_remember.domain.digest import HEATMAP_DAYS
from eng_remember.domain.digest import heatmap as digest_heatmap
from eng_remember.domain.enums import CardDirection
from eng_remember.domain.gamification import StreakState, StreakTransition
from eng_remember.domain.praise import render
from eng_remember.models.card import Card
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.cards import ImportOutcome, UserStats
from eng_remember.services.csv_io import CsvRow
from eng_remember.services.discovery import DiscoveryResult
from eng_remember.services.reminders import WeeklyDigest
from eng_remember.utils.time import ensure_utc, humanize_interval, utcnow

# How many imported rows the preview shows before it starts counting.
PREVIEW_ROWS = 10

__all__ = [
    "BTN_ADD",
    "BTN_DECKS",
    "BTN_HELP",
    "BTN_REVIEW",
    "BTN_STATS",
    "MAIN_MENU_BUTTONS",
    "already_checking",
    "answer_accepted",
    "answer_result",
    "badge",
    "cannot_parse",
    "card_back",
    "card_front",
    "card_unavailable",
    "clarification",
    "clarification_answered",
    "clarification_skipped",
    "deck_list",
    "discovery_empty",
    "discovery_needs_context",
    "discovery_preview",
    "discovery_unavailable",
    "evening_reminder",
    "explain",
    "file_export_caption",
    "file_import_cancelled",
    "file_import_empty",
    "file_import_preview",
    "file_import_prompt",
    "file_import_result",
    "file_import_too_big",
    "file_import_unreadable",
    "global_error",
    "heatmap_grid",
    "hint_unavailable",
    "import_cancelled",
    "import_preview",
    "import_result",
    "leech_example_done",
    "leech_left",
    "leech_mnemonic_done",
    "leech_offer",
    "leech_paused",
    "level_invalid",
    "level_menu",
    "level_reset",
    "level_set",
    "llm_failed",
    "llm_required",
    "looking_for_more",
    "more_found_nothing",
    "more_needs_topic",
    "more_quota_exceeded",
    "more_unavailable",
    "no_cards_yet",
    "nothing_due",
    "nothing_to_save",
    "paused_empty",
    "paused_list",
    "practice_correct",
    "practice_finished",
    "practice_question",
    "practice_unavailable",
    "practice_wrong",
    "praise_line",
    "queue_reminder",
    "review_finished",
    "stale_button",
    "stats",
    "streak_notes",
    "streak_summary",
    "unknown_command",
    "weekly_digest",
    "welcome_back",
    "welcome_new",
]

BTN_ADD = "➕ Добавить слова"
BTN_REVIEW = "🎯 Повторять"
BTN_PRACTICE = "🏋️ Тренировка"
BTN_DECKS = "📚 Колоды"
BTN_STATS = "📊 Статистика"
BTN_HELP = "❓ Помощь"

MAIN_MENU_BUTTONS = (BTN_ADD, BTN_REVIEW, BTN_PRACTICE, BTN_DECKS, BTN_STATS, BTN_HELP)

# A translation is a few words; anything longer is a paste, and showing a
# paragraph of it helps nobody. The whole text still goes to grading — only
# the revealed line is capped.
ANSWER_DISPLAY_LIMIT = 200


def welcome_new(first_name: str | None, deck_name: str) -> str:
    name = f", {escape(first_name)}" if first_name else ""
    return (
        f"Привет{name}! 👋\n\n"
        "Я помогу запоминать английские слова с интервальным повторением: "
        "слово возвращается ровно тогда, когда вы почти готовы его забыть.\n\n"
        f"Колода «{escape(deck_name)}» уже создана.\n\n"
        "<b>Как добавлять слова</b>\n"
        "Просто напишите мне сообщение в свободной форме — я разберу его сам:\n"
        "• <code>ubiquitous — вездесущий</code>\n"
        "• <code>добавь слова resilient и thorough</code>\n"
        "• <code>to run, to jump, to swim</code>\n\n"
        f"Дальше — «{BTN_REVIEW}», и я буду показывать то, что пора повторить.\n\n"
        "Если слово никак не даётся, я сам предложу помощь, а приостановленные "
        "слова ждут в /paused. Хочется просто погонять слова — /practice.\n"
        "Выгрузить или загрузить свой список файлом — /export и /import."
    )


def welcome_back(
    first_name: str | None,
    stats: UserStats,
    streak: StreakState | None = None,
) -> str:
    name = f", {escape(first_name)}" if first_name else ""
    if stats.total_cards == 0:
        tail = "Слов пока нет — напишите первое сообщение со словом."
    else:
        tail = f"К повторению сейчас: <b>{stats.due_now}</b>."

    lines = [f"С возвращением{name}! {tail}"]
    if streak is not None and streak.is_alive:
        lines.append("")
        lines.append(streak_summary(streak))
    return "\n".join(lines)


def cannot_parse(engine_is_llm: bool) -> str:
    if engine_is_llm:
        return (
            "Не разобрал сообщение. 🤔\n\n"
            "Напишите слово и перевод, например:\n"
            "<code>ubiquitous — вездесущий</code>\n\n"
            "Или просто пришлите английское слово — перевод добавлю сам."
        )
    return (
        "Не разобрал сообщение. 🤔\n\n"
        "Без подключённой модели я понимаю только пары «слово — перевод»:\n"
        "<code>ubiquitous — вездесущий</code>"
    )


def no_cards_yet() -> str:
    return "В колоде пока нет слов. Добавьте первое — просто напишите его мне."


def nothing_due(next_due_at: datetime | None) -> str:
    if next_due_at is None:
        return no_cards_yet()
    return (
        "🎉 На сейчас всё повторено.\n\n"
        f"Следующая карточка будет через <b>{humanize_interval(_seconds_until(next_due_at))}</b>."
    )


def review_finished(answered: int) -> str:
    # Both branches open with the same words: a user who tapped "finish" and a
    # user who ran out of cards should read the same confirmation.
    if answered == 0:
        return "Повторение завершено. Отвеченных карточек нет."
    word = _plural(answered, "карточку", "карточки", "карточек")
    return f"Повторение завершено. Повторили {answered} {word}. 👍"


def card_front(card: Card, *, hint: str | None = None) -> str:
    if card.direction is CardDirection.REVERSE:
        lines = [
            "🇬🇧 <i>Переведите на английский</i>",
            "",
            f"<b>{escape(card.translation)}</b>",
        ]
    else:
        lines = [f"<b>{escape(card.term)}</b>"]
        if card.part_of_speech:
            lines.append(f"<i>{escape(card.part_of_speech)}</i>")
    lines.append("")
    lines.append(f"<i>{card.state.label} · повторений: {card.reps}</i>")
    if hint:
        lines.append("")
        lines.append(f"💡 <i>{escape(hint)}</i>")
    return "\n".join(lines)


def answer_accepted(
    praise: str,
    interval_text: str,
    *,
    streak: int = 0,
    notes: Sequence[str] = (),
    hint_used: bool = False,
) -> str:
    """The line after a graded card: praise first, then the schedule.

    Praise goes above the schedule on purpose — the encouraging sentence is what
    makes the tap feel answered, and the interval is reference material.
    """
    lines = [praise, ""]
    tail = f"Дальше через <b>{interval_text}</b>"
    if hint_used:
        tail += " · 💡 подсказка"
    if streak > 0:
        tail += f" · 🔥 <b>{streak}</b>"
    lines.append(f"<i>{tail}</i>")
    for note in notes:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def card_back(card: Card, *, example: str | None = None) -> str:
    lines = [f"<b>{escape(card.term)}</b>"]
    if card.transcription:
        lines.append(f"<code>{escape(card.transcription)}</code>")
    level = badge(card.cefr, card.frequency)
    if level:
        lines.append(f"<i>{level}</i>")
    lines.append("")
    lines.append(f"🇷🇺 <b>{escape(card.translation)}</b>")
    shown = example if example is not None else card.example
    if shown:
        lines.append("")
        lines.append(f"<i>{escape(shown)}</i>")
    if card.notes:
        lines.append("")
        lines.append(f"💡 {escape(card.notes)}")
    return "\n".join(lines)


def answer_result(
    card: Card,
    *,
    verdict: Verdict,
    typed: str | None,
    praise: str,
    interval_text: str,
    streak: int = 0,
    notes: Sequence[str] = (),
    hint_used: bool = False,
    example: str | None = None,
) -> str:
    """The revealed card: how close the answer came, then the answer itself.

    The verdict leads on purpose — the correction is what the learner is here
    for, and it must not be harder to find than the word. ``typed is None``
    means the «Не знаю» button, which gets its own first line instead.
    """
    lines = ["🤷 Не помню" if typed is None else f"{verdict.emoji} {verdict.label}"]
    if typed is not None:
        lines.append(f"<i>Ваш ответ:</i> <code>{escape(_display_answer(typed))}</code>")
    lines.append("")
    lines.append(card_back(card, example=example))
    lines.append("")
    lines.append(
        answer_accepted(praise, interval_text, streak=streak, notes=notes, hint_used=hint_used)
    )
    return "\n".join(lines)


def stale_button() -> str:
    return "Кнопка устарела — начните повторение заново."


def already_checking() -> str:
    return "Секунду — уже проверяю предыдущий ответ."


def card_unavailable() -> str:
    return "Карточка больше не доступна."


def hint_unavailable() -> str:
    return "Для этой карточки подсказки нет."


def leech_offer(card: Card) -> str:
    return f"🪤 <b>{escape(card.term)}</b> не даётся: {card.again_count}-й провал. Что сделаем?"


def leech_mnemonic_done(card: Card, mnemonic: str) -> str:
    return (
        f"🧠 Мнемоника для <b>{escape(card.term)}</b>:\n\n"
        f"<i>{escape(mnemonic)}</i>\n\n"
        "Сохранена в заметки к карточке."
    )


def leech_example_done(card: Card, example: str) -> str:
    return f"✍️ Свежий пример для <b>{escape(card.term)}</b>:\n\n<i>{escape(example)}</i>"


def leech_paused(card: Card) -> str:
    return (
        f"⏸ <b>{escape(card.term)}</b> на паузе — из повторений выпало. "
        "Вернуть можно командой /paused."
    )


def leech_left(card: Card) -> str:
    return f"Ок, оставляю <b>{escape(card.term)}</b> как есть."


def llm_required() -> str:
    return "Для этого нужна подключённая модель."


def llm_failed() -> str:
    return "Модель не ответила — попробуйте ещё раз позже."


def paused_list(cards: Sequence[Card], total: int) -> str:
    header = f"⏸ На паузе {total} {_plural(total, 'слово', 'слова', 'слов')}"
    if total > len(cards):
        header += f" (первые {len(cards)})"
    lines = [header, ""]
    lines.extend(f"• <b>{escape(card.term)}</b> — {escape(card.translation)}" for card in cards)
    lines.append("")
    lines.append("<i>Нажмите на слово, чтобы вернуть его в повторения.</i>")
    return "\n".join(lines)


def paused_empty() -> str:
    return "На паузе ничего нет."


CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")

FREQUENCY_LABELS = {"common": "частое", "uncommon": "обычное", "rare": "редкое"}


def badge(cefr: str | None, frequency: str | None) -> str | None:
    """The level/rarity line, when at least one known value is present.

    Only known values render: a junk string written past the validators must
    degrade to "no badge", never to markup or noise.
    """
    safe_cefr = cefr if cefr in CEFR_LEVELS else None
    safe_frequency = FREQUENCY_LABELS.get(frequency or "")
    parts = [escape(part) for part in (safe_cefr, safe_frequency) if part]
    return " · ".join(parts) or None


def level_menu(current: str | None) -> str:
    shown = escape(current) if current else "не указан"
    return (
        f"🎚 Ваш уровень: <b>{shown}</b>\n\n"
        "Выберите кнопкой или напишите, например <code>/level B1</code>."
    )


def level_set(level: str) -> str:
    return f"🎚 Уровень сохранён: <b>{escape(level)}</b>. Подбор слов будет держаться около него."


def level_reset() -> str:
    return "🎚 Уровень сброшен — подбираю слова как раньше."


def level_invalid(argument: str) -> str:
    return (
        f"Не знаю такого уровня: <code>{escape(argument)}</code>\n"
        "Напишите от A1 до C2, например <code>/level B1</code>."
    )


def practice_question(term: str) -> str:
    return f"<b>{escape(term)}</b>\n\nВыберите перевод:"


def practice_correct(translation: str) -> str:
    return f"✅ Верно: <b>{escape(translation)}</b>"


def practice_wrong(translation: str) -> str:
    return f"❌ Правильно: <b>{escape(translation)}</b>"


def practice_finished(correct: int, total: int) -> str:
    return f"Тренировка завершена: <b>{correct}</b> из <b>{total}</b> верно. 👍"


def practice_unavailable() -> str:
    return "Для тренировки нужно хотя бы 4 слова в повторениях."


def file_export_caption(total: int) -> str:
    word = _plural(total, "слово", "слова", "слов")
    return f"📄 В файле {total} {word}."


def file_import_prompt() -> str:
    return (
        "Пришлите CSV-файл со словами — разберу и покажу, что нашёл.\n\n"
        "Колонки: <code>term;translation;transcription;part_of_speech;example;notes;tags;deck</code>.\n"
        "Обязательна только <code>term</code>; переводы, если их нет, дополнит модель.\n"
        "Разделитель — <code>;</code>, <code>,</code> или табуляция; заголовок необязателен."
    )


def file_import_preview(
    rows: Sequence[CsvRow],
    *,
    duplicates: int,
    untranslated: int,
    skipped: int,
    filled: int,
) -> str:
    word = _plural(len(rows), "слово", "слова", "слов")
    lines = [f"📄 Нашёл {len(rows)} {word} для импорта.", ""]
    for row in list(rows)[:PREVIEW_ROWS]:
        lines.append(f"• <b>{escape(row.term)}</b> — {escape(row.translation or '')}")
    if len(rows) > PREVIEW_ROWS:
        lines.append(f"… и ещё {len(rows) - PREVIEW_ROWS}")

    notes: list[str] = []
    if duplicates:
        notes.append(f"уже было: {duplicates}")
    if untranslated:
        notes.append(f"без перевода пропущено: {untranslated}")
    if skipped:
        notes.append(f"нераспознанных строк: {skipped}")
    if filled:
        notes.append(f"переводов добавила модель: {filled}")
    if notes:
        lines.append("")
        lines.append("<i>(" + "; ".join(notes) + ")</i>")
    return "\n".join(lines)


def file_import_result(outcome: ImportOutcome) -> str:
    word = _plural(len(outcome.added), "слово", "слова", "слов")
    lines = [f"✅ Импортировано: {len(outcome.added)} {word}."]
    if outcome.total_skipped:
        lines.append(f"Пропущено дублей: {outcome.total_skipped}.")
    return "\n".join(lines)


def file_import_cancelled() -> str:
    return "Импорт отменён."


def file_import_empty() -> str:
    return "В файле не нашлось слов с переводом."


def file_import_too_big() -> str:
    return "Файл слишком большой — пришлите CSV до 1 МБ."


def file_import_unreadable() -> str:
    return "Не удалось прочитать файл — сохраните его в UTF-8."


def import_preview(
    drafts: list[CardDraft],
    engine: str,
    *,
    found: DiscoveryResult | None = None,
    note: str | None = None,
) -> str:
    """The batch waiting for confirmation.

    ``found`` adds the words the search produced for the same message — the
    learner asked for both at once, so they are offered as one batch with one
    save button. ``note`` carries what could not be done, so a partial result is
    never read as the whole answer.
    """
    lines = [f"Разобрал <b>{len(drafts)}</b> {_plural(len(drafts), 'слово', 'слова', 'слов')}:", ""]
    lines.extend(cards_listing(drafts))

    if found is not None and found.drafts:
        lines.append("")
        lines.append(_more_words_head(found))
        lines.append("")
        lines.extend(cards_listing(found.drafts))
        footnotes = _discovery_footnotes(found)
        if footnotes:
            lines.append("")
            lines.extend(footnotes)

    if note:
        lines.append("")
        lines.append(note)

    lines.append("")
    if engine != "rules":
        lines.append(f"<i>Разобрано моделью {escape(engine)}</i>")
    else:
        lines.append("<i>Разобрано без модели. Проверьте переводы.</i>")
    return "\n".join(lines)


def _more_words_head(found: DiscoveryResult) -> str:
    count = found.found
    words = _plural(count, "слово", "слова", "слов")
    if found.topic:
        return f"🔍 Ещё <b>{count}</b> {words} по теме «{escape(found.topic)}»:"
    return f"🔍 Ещё <b>{count}</b> {words}:"


def _discovery_footnotes(result: DiscoveryResult) -> list[str]:
    """What was dropped and where the count fell short, in the wording used
    everywhere a suggestion batch is shown."""
    dropped: list[str] = []
    if result.dropped_out_of_text:
        dropped.append(f"отброшено {result.dropped_out_of_text} — этих слов нет в тексте")
    if result.dropped_known:
        dropped.append(f"{result.dropped_known} уже есть в коллекции")
    if result.dropped_repeated:
        dropped.append(f"{result.dropped_repeated} повторились")

    lines: list[str] = []
    if dropped:
        lines.append(f"<i>Отсеяно: {'; '.join(dropped)}.</i>")
    if result.short_by:
        lines.append(
            f"<i>Просили {result.requested}, нашлось {result.found} — "
            f"честно больше не набралось.</i>"
        )
    return lines


def cards_listing(drafts: Sequence[CardDraft]) -> list[str]:
    """Render cards the same way everywhere they are previewed."""
    lines: list[str] = []
    for draft in drafts:
        head = f"• <b>{escape(draft.term)}</b>"
        if draft.transcription:
            head += f" <code>{escape(draft.transcription)}</code>"
        level = badge(draft.cefr, draft.frequency)
        if level:
            head += f" · <i>{level}</i>"
        lines.append(head)
        lines.append(f"   {escape(draft.translation)}")
        if draft.example:
            lines.append(f"   <i>{escape(draft.example)}</i>")
    return lines


def clarification(question: str) -> str:
    return f"❓ {escape(question)}"


def clarification_answered(answer: str) -> str:
    return f"❓ — {escape(answer)}\n\nИщу слова…"


def clarification_skipped() -> str:
    return "🤷 Хорошо, решу сам."


def looking_for_more() -> str:
    return "🔄 Ищу другие слова…"


def nothing_to_save() -> str:
    return "Уже нечего сохранять — пришлите слова заново."


def import_cancelled() -> str:
    return "Отменил, ничего не сохранял."


def unknown_command() -> str:
    return "Не знаю такой команды. Напишите слово — или посмотрите /help."


def unknown_timezone(argument: str) -> str:
    """The zone the user typed is not an IANA name.

    The argument is echoed back escaped: it is whatever the user typed, and a
    live ``<`` in it would otherwise be parsed as markup by Telegram.
    """
    return (
        f"Не знаю такой пояс: <code>{escape(argument)}</code>\n"
        "Нужен идентификатор IANA, например <code>Europe/Berlin</code>."
    )


def discovery_preview(
    result: DiscoveryResult,
    *,
    context_used: bool,
    deck_name: str,
) -> str:
    """The suggestion batch, with the accounting shown honestly.

    If words were dropped the user is told why: silently returning six words
    when ten were asked for looks like a failure, while "three were not in your
    text" reads as care.
    """
    if context_used:
        head = f"🔍 Нашёл в тексте <b>{result.found}</b>:"
    elif result.topic:
        head = (
            f"🔍 Подборка по теме «{escape(result.topic)}» — "
            f"<b>{result.found}</b> {_plural(result.found, 'слово', 'слова', 'слов')}:"
        )
    else:
        head = f"🔍 Подобрал <b>{result.found}</b> слов:"

    lines = [head, ""]
    lines.extend(cards_listing(result.drafts))

    footnotes = _discovery_footnotes(result)
    if footnotes:
        lines.append("")
        lines.extend(footnotes)
    lines.append("")
    lines.append(f"<i>Пойдёт в колоду «{escape(deck_name)}».</i>")
    return "\n".join(lines)


def discovery_empty(*, context_used: bool) -> str:
    if context_used:
        return (
            "🔍 Не нашёл в этом тексте слов, которых у вас ещё нет.\n\n"
            "Пришлите текст подлиннее — или назовите тему."
        )
    return (
        "🔍 Ничего нового подобрать не удалось.\n\n"
        "Попробуйте назвать тему точнее — например «лексика для переговоров»."
    )


def discovery_needs_context() -> str:
    """What to say when there is nothing to expand from.

    Asked instead of guessing: words generated with no topic and no text would be
    random, and random vocabulary is worse than a question.
    """
    return (
        "Чтобы подобрать слова, нужно понимать контекст. 🤔\n\n"
        "Назовите тему: <code>ещё слова про медицину</code>.\n"
        "Или пришлите абзац текста — найду слова в нём."
    )


def discovery_unavailable() -> str:
    return (
        "Для подбора слов нужна подключённая модель. 🔌\n\n"
        "Пока я умею только разбирать пары «слово — перевод». "
        "Ключ добавляется в <code>LLM__API_KEY</code>."
    )


def more_needs_topic() -> str:
    """The learner asked for extra words, but their domain could not be read.

    Shown under the words that did get through: the addition is not lost, and
    the missing half is asked for instead of guessed. A word on its own does not
    name a domain — "arrangement" is договорённость, расположение or аранжировка,
    and the words around the wrong sense would be useless.
    """
    return (
        "<i>Дополнительные слова не подобрал: не понял тему. Назовите её — "
        "например, «ещё слова про оценку» — и я подберу.</i>"
    )


def more_unavailable() -> str:
    return "<i>Дополнительные слова подобрать не удалось: нужна подключённая модель.</i>"


def more_quota_exceeded() -> str:
    return "<i>Дополнительные слова не подобрал: исчерпан лимит обращений к модели.</i>"


def more_found_nothing() -> str:
    return "<i>Дополнительных слов по теме не нашлось — эти можно сохранить.</i>"


def quota_exceeded() -> str:
    """The model is fine; this learner's own budget is spent.

    Worded apart from :func:`discovery_unavailable` on purpose: "connect a
    model" would be wrong advice here, and the manual path still works.
    """
    return (
        "⏳ Лимит обращений к модели исчерпан.\n\n"
        "Пришлите пары «слово — перевод», и я разберу их без модели. "
        "Лимит настраивается в <code>LLM__MAX_CALLS_PER_DAY</code> и "
        "<code>LLM__MAX_TOKENS_PER_DAY</code>."
    )


def praise_line(
    template: str,
    *,
    term: str,
    interval: str,
    streak: int = 0,
) -> str:
    """Fill a praise template, escaping the one value that comes from the user."""
    return render(
        template,
        term=escape(term),
        interval=escape(interval),
        streak=str(streak) if streak else "",
    )


def streak_notes(transition: StreakTransition | None) -> list[str]:
    """Render the streak events worth spelling out."""
    if transition is None:
        return []

    notes: list[str] = []
    used = transition.freezes_used
    if used:
        notes.append(
            f"🧊 Заморозка сохранила серию — пропущено дней: {used}. "
            f"Свободных заморозок: <b>{transition.state.freezes}</b>"
        )
    if transition.lost:
        notes.append("💔 Серия прервалась. Один ответ сегодня — и начнётся новая.")
    if transition.freeze_earned:
        notes.append(f"🧊 Новая заморозка. Теперь их <b>{transition.state.freezes}</b>")
    return notes


def queue_reminder(*, due_now: int, new_available: int, streak_days: int) -> str:
    """The morning ping: what is waiting, and where the streak stands.

    Sent only when there is something to do — "you have nothing to review" is a
    notification about the absence of a notification.
    """
    lines = ["☀️ <b>Доброе утро!</b>", ""]

    parts: list[str] = []
    if due_now:
        parts.append(f"к повторению — <b>{due_now}</b>")
    if new_available:
        parts.append(f"новых — <b>{new_available}</b>")
    lines.append("Ждут: " + ", ".join(parts) + ".")

    if streak_days > 1:
        lines.append(
            f"🔥 Серия {streak_days} {_plural(streak_days, 'день', 'дня', 'дней')} — не прерывайте."
        )
    lines += ["", "<i>Нажмите «" + BTN_REVIEW + "», когда будет минута.</i>"]
    return "\n".join(lines)


def heatmap_grid(days: Sequence[int]) -> str:
    """The 28-day heatmap as four rows of seven, oldest first."""
    cells = digest_heatmap(days)
    return "\n".join("".join(cells[index : index + 7]) for index in range(0, HEATMAP_DAYS, 7))


def weekly_digest(digest: WeeklyDigest, *, streak_days: int) -> str:
    """The Monday summary: the week's figures, the hard words, the heatmap."""
    lines = ["📅 <b>Итоги недели</b>", ""]
    lines.append(f"Отвечено: <b>{digest.total}</b> · верно: <b>{digest.correct}</b>")
    lines.append(f"Новых слов: <b>{digest.new_words}</b>")
    if streak_days > 1:
        lines.append(f"🔥 Серия: <b>{streak_days}</b>")
    if digest.weak:
        weak = ", ".join(f"<b>{escape(term)}</b> ({count})" for term, count in digest.weak)
        lines += ["", f"Трудные слова: {weak}"]
    lines += [
        "",
        f"К повторению сейчас: <b>{digest.due_now}</b> · "
        f"в ближайшую неделю: <b>{digest.due_week}</b>",
        "",
        heatmap_grid(digest.days),
        "<i>Ответы за последние 4 недели.</i>",
    ]
    return "\n".join(lines)


def evening_reminder(
    *,
    streak_days: int,
    due_now: int,
    new_available: int,
    streak_warning: bool,
) -> str:
    """The evening slot: the queue remainder and, when due, the streak warning.

    One message for the rest of the day — the warning and the remainder are the
    same ask, so splitting them would be two notifications for one action.
    """
    total = due_now + new_available
    if streak_warning:
        lines = [
            f"🔥 <b>Серия {streak_days} "
            f"{_plural(streak_days, 'день', 'дня', 'дней')} под угрозой.</b>",
            "",
            "Сегодня ещё ни одной карточки. Пара минут — и день засчитан.",
        ]
    else:
        lines = ["🌙 <b>Вечерняя проверка</b>"]

    if total:
        lines.append("")
        lines.append(
            f"<i>Ждут {total} {_plural(total, 'карточка', 'карточки', 'карточек')}. "
            f"Нажмите «{BTN_REVIEW}».</i>"
        )
    return "\n".join(lines)


def streak_summary(state: StreakState) -> str:
    if not state.is_alive:
        head = "🔥 Серия: <b>не начата</b>"
    else:
        head = f"🔥 Серия: <b>{state.current}</b> {_plural(state.current, 'день', 'дня', 'дней')}"
        if state.longest > state.current:
            head += f" · лучшая {state.longest}"

    parts = [head]
    if state.freezes:
        parts.append(f"🧊 Заморозки: <b>{state.freezes}</b>")
    if state.missed_days:
        parts.append(f"Пропущено дней: <b>{state.missed_days}</b>")
    return "\n".join(parts)


def import_result(outcome: ImportOutcome, deck_name: str) -> str:
    lines: list[str] = []
    if outcome.added:
        word = _plural(len(outcome.added), "слово", "слова", "слов")
        lines.append(f"✅ Добавил {len(outcome.added)} {word} в «{escape(deck_name)}».")
    else:
        lines.append("Новых слов не добавилось.")

    if outcome.skipped_existing:
        lines.append("")
        lines.append(f"Уже были в колоде: {_terms(outcome.skipped_existing)}")
    if outcome.skipped_in_batch:
        lines.append(f"Повторились в сообщении: {_terms(outcome.skipped_in_batch)}")
    if outcome.added:
        lines.append("")
        lines.append("Первое повторение — завтра: в день добавления слова не показываются.")
    return "\n".join(lines)


def deck_list(rows: list[tuple[str, int]], total: int) -> str:
    lines = [f"📚 <b>Колоды</b> — всего слов: {total}", ""]
    for name, count in rows:
        lines.append(f"• {escape(name)} — {count}")
    return "\n".join(lines)


def stats(
    user_stats: UserStats,
    timezone_name: str,
    retention: float,
    *,
    streak: StreakState | None = None,
    heatmap: Sequence[int] | None = None,
) -> str:
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"Всего слов: <b>{user_stats.total_cards}</b>",
        f"К повторению сейчас: <b>{user_stats.due_now}</b>",
        f"Новых в очереди: <b>{user_stats.new_cards}</b>",
        f"Повторений сегодня: <b>{user_stats.reviews_today}</b>",
        f"Новых слов сегодня: <b>{user_stats.new_today}</b>",
    ]
    if streak is not None:
        lines += ["", streak_summary(streak)]
    if heatmap is not None:
        lines += ["", heatmap_grid(heatmap), "<i>Ответы за последние 4 недели.</i>"]
    lines += [
        "",
        f"<i>Часовой пояс: {escape(timezone_name)} · целевая вероятность "
        f"вспоминания: {retention:.0%}</i>",
    ]
    return "\n".join(lines)


def explain(answer: str) -> str:
    return f"💬 {escape(answer)}\n\n<i>Чтобы сохранить слово, отправьте его перевод через тире.</i>"


def global_error() -> str:
    return "Что-то сломалось на моей стороне. Уже записал в лог — попробуйте ещё раз."


def _display_answer(typed: str) -> str:
    if len(typed) <= ANSWER_DISPLAY_LIMIT:
        return typed
    return typed[:ANSWER_DISPLAY_LIMIT].rstrip() + "…"


def _terms(terms: list[str]) -> str:
    shown = terms[:10]
    text = ", ".join(f"<code>{escape(term)}</code>" for term in shown)
    if len(terms) > len(shown):
        text += f" и ещё {len(terms) - len(shown)}"
    return text


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _seconds_until(moment: datetime) -> float:
    target = ensure_utc(moment) or utcnow()
    return (target - utcnow()).total_seconds()
