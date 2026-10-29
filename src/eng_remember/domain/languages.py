"""The languages the bot can study, and the text rules around them.

Russian is the one translation language and is deliberately not a target.  A
card's language is therefore a property of the card, not of the user: one
collection may mix English, French and Chinese cards.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "NATIVE_LANGUAGE",
    "SUPPORTED_CODES",
    "SUPPORTED_LANGUAGES",
    "Language",
    "detect_script_language",
    "language_badge",
    "language_for_code",
    "language_name_en",
    "language_name_ru",
    "language_target_phrase",
    "resolve_language",
    "resolve_target_language",
]

DEFAULT_LANGUAGE = "eng"
NATIVE_LANGUAGE = "rus"


@dataclass(frozen=True, slots=True)
class Language:
    """Everything the UI and prompts need to know about one language."""

    code: str
    name_ru: str
    name_en: str
    flag: str
    script: str
    rtl: bool = False
    name_ru_in: str = ""


SUPPORTED_LANGUAGES: tuple[Language, ...] = (
    Language("eng", "Английский", "English", "🇬🇧", "latin", name_ru_in="на английском"),
    Language(
        "cmn",
        "Китайский",
        "Chinese (Mandarin)",
        "🇨🇳",
        "han",
        name_ru_in="на китайском",
    ),
    Language("hin", "Хинди", "Hindi", "🇮🇳", "devanagari", name_ru_in="на хинди"),
    Language("spa", "Испанский", "Spanish", "🇪🇸", "latin", name_ru_in="на испанском"),
    Language(
        "arb",
        "Арабский",
        "Arabic",
        "🇸🇦",
        "arabic",
        rtl=True,
        name_ru_in="на арабском",
    ),
    Language("fra", "Французский", "French", "🇫🇷", "latin", name_ru_in="на французском"),
    Language("ben", "Бенгальский", "Bengali", "🇧🇩", "bengali", name_ru_in="на бенгальском"),
    Language(
        "por",
        "Португальский",
        "Portuguese",
        "🇵🇹",
        "latin",
        name_ru_in="на португальском",
    ),
    Language(
        "ind",
        "Индонезийский",
        "Indonesian",
        "🇮🇩",
        "latin",
        name_ru_in="на индонезийском",
    ),
    Language("deu", "Немецкий", "German", "🇩🇪", "latin", name_ru_in="на немецком"),
    Language("jpn", "Японский", "Japanese", "🇯🇵", "japanese", name_ru_in="на японском"),
    Language("vie", "Вьетнамский", "Vietnamese", "🇻🇳", "latin", name_ru_in="на вьетнамском"),
    Language("swh", "Суахили", "Swahili", "🇰🇪", "latin", name_ru_in="на суахили"),
    Language("tur", "Турецкий", "Turkish", "🇹🇷", "latin", name_ru_in="на турецком"),
    Language("kor", "Корейский", "Korean", "🇰🇷", "hangul", name_ru_in="на корейском"),
    Language("tha", "Тайский", "Thai", "🇹🇭", "thai", name_ru_in="на тайском"),
    Language("ita", "Итальянский", "Italian", "🇮🇹", "latin", name_ru_in="на итальянском"),
)

RUSSIAN = Language("rus", "Русский", "Russian", "🇷🇺", "cyrillic", name_ru_in="на русском")

SUPPORTED_CODES: frozenset[str] = frozenset(language.code for language in SUPPORTED_LANGUAGES)
LANGUAGES: dict[str, Language] = {language.code: language for language in SUPPORTED_LANGUAGES}
LANGUAGES[RUSSIAN.code] = RUSSIAN

# ISO 639-1 codes, names and the spellings users actually type.  Regional tags
# are handled by falling back to their base tag (fr-FR -> fr, zh-Hant -> zh).
_ALIASES: dict[str, str] = {}


def _add_aliases(code: str, *aliases: str) -> None:
    for alias in (code, *aliases):
        _ALIASES[alias.strip().casefold().replace("_", "-")] = code


for _language in (*SUPPORTED_LANGUAGES, RUSSIAN):
    _add_aliases(
        _language.code,
        _language.name_en,
        _language.name_ru,
        _language.name_en.casefold(),
        _language.name_ru.casefold(),
    )

_add_aliases("eng", "en", "англ", "en-us", "en-gb")
_add_aliases(
    "cmn",
    "zh",
    "chinese",
    "mandarin",
    "китайский",
    "мандарин",
    "中文",
    "zh-cn",
    "zh-hans",
    "zh-hant",
)
_add_aliases("hin", "hi", "hindi", "хинди", "हिन्दी", "हिंदी")
_add_aliases("spa", "es", "spanish", "español", "испанский", "es-mx")
_add_aliases(
    "arb",
    "ar",
    "arabic",
    "msa",
    "modern standard arabic",
    "standard arabic",
    "арабский",
    "العربية",
    "ar-sa",
    "ar-eg",
)
_add_aliases("fra", "fr", "french", "français", "francais", "французский", "fr-fr")
_add_aliases("ben", "bn", "bengali", "bangla", "бенгальский", "বাংলা")
_add_aliases("por", "pt", "portuguese", "português", "portugues", "португальский", "pt-br")
_add_aliases("ind", "id", "indonesian", "bahasa indonesia", "bahasa", "индонезийский")
_add_aliases("deu", "de", "german", "deutsch", "немецкий", "de-at")
_add_aliases("jpn", "ja", "jp", "japanese", "日本語", "японский")
_add_aliases("vie", "vi", "vietnamese", "tiếng việt", "tieng viet", "вьетнамский")
_add_aliases("swh", "sw", "swahili", "kiswahili", "суахили")
_add_aliases("tur", "tr", "turkish", "türkçe", "turkce", "турецкий")
_add_aliases("kor", "ko", "kr", "korean", "한국어", "корейский")
_add_aliases("tha", "th", "thai", "ไทย", "тайский")
_add_aliases("ita", "it", "italian", "italiano", "итальянский")
_add_aliases("rus", "ru", "russian", "русский", "рус")


def _normalise_token(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def language_for_code(value: str) -> Language | None:
    """Return a language for a code, name or regional tag; ``None`` if unknown."""
    token = _normalise_token(value)
    if not token:
        return None
    code = _ALIASES.get(token)
    if code is None and "-" in token:
        code = _ALIASES.get(token.split("-", 1)[0])
    return LANGUAGES.get(code) if code is not None else None


def resolve_language(value: str | None) -> str:
    """Canonical code, including ``rus`` for the translation language."""
    language = language_for_code(value) if value else None
    return language.code if language is not None else DEFAULT_LANGUAGE


def resolve_target_language(value: str | None) -> str:
    """Canonical target code; Russian and unknown values degrade to English."""
    code = resolve_language(value)
    return code if code in SUPPORTED_CODES else DEFAULT_LANGUAGE


def language_name_en(code: str) -> str:
    language = language_for_code(code) or LANGUAGES[DEFAULT_LANGUAGE]
    return language.name_en


def language_name_ru(code: str) -> str:
    language = language_for_code(code) or LANGUAGES[DEFAULT_LANGUAGE]
    return language.name_ru


def language_badge(code: str) -> str:
    language = language_for_code(code) or LANGUAGES[DEFAULT_LANGUAGE]
    return f"{language.flag} {language.name_ru}"


def language_target_phrase(code: str) -> str:
    language = language_for_code(code) or LANGUAGES[DEFAULT_LANGUAGE]
    return language.name_ru_in


# The ranges are intentionally broad: they only have to answer "which script",
# and every letter outside them is treated as unsupported rather than guessed.
_LATIN_RANGES = (
    (0x0041, 0x005A),
    (0x0061, 0x007A),
    (0x00C0, 0x024F),
    (0x1E00, 0x1EFF),
    (0x2C60, 0x2C7F),
    (0xA720, 0xA7FF),
    (0xAB30, 0xAB6F),
    (0xFF21, 0xFF3A),
    (0xFF41, 0xFF5A),
)
_CYRILLIC_RANGES = (
    (0x0400, 0x052F),
    (0x2DE0, 0x2DFF),
    (0xA640, 0xA69F),
)
_HAN_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)
_HIRAGANA_RANGES = ((0x3040, 0x309F),)
_KATAKANA_RANGES = (
    (0x30A0, 0x30FF),
    (0x31F0, 0x31FF),
    (0xFF66, 0xFF9D),
)
_HANGUL_RANGES = (
    (0x1100, 0x11FF),
    (0x3130, 0x318F),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),
)
_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
_DEVANAGARI_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))
_BENGALI_RANGES = ((0x0980, 0x09FF),)
_THAI_RANGES = ((0x0E00, 0x0E7F),)


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= code <= end for start, end in ranges)


def _script_of(char: str) -> str:
    code = ord(char)
    if _in_ranges(code, _LATIN_RANGES):
        return "latin"
    if _in_ranges(code, _CYRILLIC_RANGES):
        return "cyrillic"
    if _in_ranges(code, _HAN_RANGES):
        return "han"
    if _in_ranges(code, _HIRAGANA_RANGES):
        return "hiragana"
    if _in_ranges(code, _KATAKANA_RANGES):
        return "katakana"
    if _in_ranges(code, _HANGUL_RANGES):
        return "hangul"
    if _in_ranges(code, _ARABIC_RANGES):
        return "arabic"
    if _in_ranges(code, _DEVANAGARI_RANGES):
        return "devanagari"
    if _in_ranges(code, _BENGALI_RANGES):
        return "bengali"
    if _in_ranges(code, _THAI_RANGES):
        return "thai"
    return "unsupported"


_SCRIPT_LANGUAGE = {
    "latin": "eng",
    "han": "cmn",
    "devanagari": "hin",
    "arabic": "arb",
    "bengali": "ben",
    "thai": "tha",
}


def detect_script_language(text: str) -> str | None:
    """Pick the dominant supported script, or ``None`` for unsupported ones.

    Kana beats Han because a mixed Japanese word is Japanese, not Chinese;
    Hangul is Korean.  Any Cyrillic or unsupported letter rejects the whole
    string, which is what keeps Russian-left/English-right mixups out.
    """
    counts: Counter[str] = Counter()
    has_kana = False
    has_hangul = False

    for char in text:
        if not char.isalpha():
            continue
        script = _script_of(char)
        if script in {"cyrillic", "unsupported"}:
            return None
        counts[script] += 1
        if script in {"hiragana", "katakana"}:
            has_kana = True
        elif script == "hangul":
            has_hangul = True

    if not counts:
        return None
    if has_kana:
        return "jpn"
    if has_hangul:
        return "kor"

    dominant = max(counts, key=lambda script: counts[script])
    return _SCRIPT_LANGUAGE.get(dominant)
