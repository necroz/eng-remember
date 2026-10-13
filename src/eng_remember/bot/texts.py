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

from eng_remember.domain.gamification import StreakState, StreakTransition
from eng_remember.domain.praise import render
from eng_remember.domain.srs import SchedulePreview
from eng_remember.models.card import Card
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.cards import ImportOutcome, UserStats
from eng_remember.services.discovery import DiscoveryResult
from eng_remember.utils.time import ensure_utc, humanize_interval, utcnow

__all__ = [
    "BTN_ADD",
    "BTN_DECKS",
    "BTN_HELP",
    "BTN_REVIEW",
    "BTN_STATS",
    "MAIN_MENU_BUTTONS",
    "answer_accepted",
    "cannot_parse",
    "card_back",
    "card_front",
    "clarification",
    "clarification_answered",
    "clarification_skipped",
    "deck_list",
    "discovery_empty",
    "discovery_needs_context",
    "discovery_preview",
    "discovery_unavailable",
    "explain",
    "global_error",
    "import_cancelled",
    "import_preview",
    "import_result",
    "looking_for_more",
    "no_cards_yet",
    "nothing_due",
    "nothing_to_save",
    "praise_line",
    "queue_reminder",
    "rating_buttons_hint",
    "review_finished",
    "stats",
    "streak_notes",
    "streak_reminder",
    "streak_summary",
    "unknown_command",
    "welcome_back",
    "welcome_new",
]

BTN_ADD = "➕ Добавить слова"
BTN_REVIEW = "🎯 Повторять"
BTN_DECKS = "📚 Колоды"
BTN_STATS = "📊 Статистика"
BTN_HELP = "❓ Помощь"

MAIN_MENU_BUTTONS = (BTN_ADD, BTN_REVIEW, BTN_DECKS, BTN_STATS, BTN_HELP)


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
        f"Дальше — «{BTN_REVIEW}», и я буду показывать то, что пора повторить."
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


def card_front(card: Card) -> str:
    lines = [f"<b>{escape(card.term)}</b>"]
    if card.part_of_speech:
        lines.append(f"<i>{escape(card.part_of_speech)}</i>")
    lines.append("")
    lines.append(f"<i>{card.state.label} · повторений: {card.reps}</i>")
    return "\n".join(lines)


def answer_accepted(
    praise: str,
    interval_text: str,
    *,
    streak: int = 0,
    notes: Sequence[str] = (),
) -> str:
    """The line after a graded card: praise first, then the schedule.

    Praise goes above the schedule on purpose — the encouraging sentence is what
    makes the tap feel answered, and the interval is reference material.
    """
    lines = [praise, ""]
    tail = f"Дальше через <b>{interval_text}</b>"
    if streak > 0:
        tail += f" · 🔥 <b>{streak}</b>"
    lines.append(f"<i>{tail}</i>")
    for note in notes:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def card_back(card: Card, previews: list[SchedulePreview]) -> str:
    lines = [f"<b>{escape(card.term)}</b>"]
    if card.transcription:
        lines.append(f"<code>{escape(card.transcription)}</code>")
    lines.append("")
    lines.append(f"🇷🇺 <b>{escape(card.translation)}</b>")
    if card.example:
        lines.append("")
        lines.append(f"<i>{escape(card.example)}</i>")
    if card.notes:
        lines.append("")
        lines.append(f"💡 {escape(card.notes)}")
    if previews:
        lines.append("")
        lines.append("<i>Насколько помните?</i>")
    return "\n".join(lines)


def rating_buttons_hint() -> str:
    return "Выберите вариант — покажу, через сколько слово вернётся."


def import_preview(drafts: list[CardDraft], engine: str) -> str:
    lines = [f"Разобрал <b>{len(drafts)}</b> {_plural(len(drafts), 'слово', 'слова', 'слов')}:", ""]
    lines.extend(cards_listing(drafts))
    lines.append("")
    if engine != "rules":
        lines.append(f"<i>Разобрано моделью {escape(engine)}</i>")
    else:
        lines.append("<i>Разобрано без модели. Проверьте переводы.</i>")
    return "\n".join(lines)


def cards_listing(drafts: Sequence[CardDraft]) -> list[str]:
    """Render cards the same way everywhere they are previewed."""
    lines: list[str] = []
    for draft in drafts:
        head = f"• <b>{escape(draft.term)}</b>"
        if draft.transcription:
            head += f" <code>{escape(draft.transcription)}</code>"
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

    footnotes: list[str] = []
    if result.dropped_out_of_text:
        footnotes.append(f"отброшено {result.dropped_out_of_text} — этих слов нет в тексте")
    if result.dropped_known:
        footnotes.append(f"{result.dropped_known} уже есть в коллекции")
    if result.dropped_repeated:
        footnotes.append(f"{result.dropped_repeated} повторились")
    if footnotes:
        lines.append("")
        lines.append(f"<i>Отсеяно: {'; '.join(footnotes)}.</i>")
    if result.short_by:
        lines.append(
            f"<i>Просили {result.requested}, нашлось {result.found} — "
            f"честно больше не набралось.</i>"
        )
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


def quota_exceeded() -> str:
    """The model is fine; this learner's own budget is spent.

    Worded apart from :func:`discovery_unavailable` on purpose: "connect a
    model" would be wrong advice here, and the manual path still works.
    """
    return (
        "⏳ Лимит обращений к модели исчерпан.\n\n"
        "Пришлите пары «слово — перевод», и я разберу их без модели. "
        "Лимит настраивается в <code>LLM__MAX_CALLS_PER_HOUR</code> и "
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


def streak_reminder(*, streak_days: int, due_now: int, new_available: int) -> str:
    """The evening warning, sent only while the streak can still be saved."""
    total = due_now + new_available
    lines = [
        f"🔥 <b>Серия {streak_days} {_plural(streak_days, 'день', 'дня', 'дней')} под угрозой.</b>",
        "",
        "Сегодня ещё ни одной карточки. Пара минут — и день засчитан.",
    ]
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
        lines.append(f"Слова уже ждут в «{BTN_REVIEW}».")
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
