"""The language registry: the single source of truth for codes and labels."""

from __future__ import annotations

import pytest

from eng_remember.domain.languages import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    NATIVE_LANGUAGE,
    SUPPORTED_CODES,
    SUPPORTED_LANGUAGES,
    detect_script_language,
    language_badge,
    language_for_code,
    language_name_en,
    language_name_ru,
    language_target_phrase,
    resolve_language,
    resolve_target_language,
)

EXPECTED_CODES = [
    "eng",
    "cmn",
    "hin",
    "spa",
    "arb",
    "fra",
    "ben",
    "por",
    "ind",
    "deu",
    "jpn",
    "vie",
    "swh",
    "tur",
    "kor",
    "tha",
    "ita",
]


def test_supported_languages_have_the_agreed_order() -> None:
    assert [language.code for language in SUPPORTED_LANGUAGES] == EXPECTED_CODES
    assert set(SUPPORTED_CODES) == set(EXPECTED_CODES)
    assert frozenset(EXPECTED_CODES) == SUPPORTED_CODES
    assert DEFAULT_LANGUAGE == "eng"
    assert NATIVE_LANGUAGE == "rus"


def test_russian_is_in_the_registry_but_not_a_target() -> None:
    assert "rus" in LANGUAGES
    assert "rus" not in SUPPORTED_CODES
    assert LANGUAGES["rus"].name_ru == "Русский"
    assert language_name_en("rus") == "Russian"
    assert language_name_ru("rus") == "Русский"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en", "eng"),
        ("English", "eng"),
        ("zh", "cmn"),
        ("zh-Hant", "cmn"),
        ("zh_CN", "cmn"),
        ("fr", "fra"),
        ("fr-FR", "fra"),
        ("pt-BR", "por"),
        ("ar-EG", "arb"),
        ("French", "fra"),
        ("французский", "fra"),
        ("ru", "rus"),
        ("русский", "rus"),
        ("unknown", "eng"),
        (None, "eng"),
    ],
)
def test_resolve_language_accepts_aliases_and_regional_tags(
    value: str | None, expected: str
) -> None:
    assert resolve_language(value) == expected


def test_language_for_code_is_strict_about_unknown_codes() -> None:
    assert language_for_code("xx") is None
    assert language_for_code("en").code == "eng"  # type: ignore[union-attr]
    assert language_for_code("zh-Hant").code == "cmn"  # type: ignore[union-attr]
    assert language_for_code("French").code == "fra"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ru", "eng"),
        ("rus", "eng"),
        ("русский", "eng"),
        ("fra", "fra"),
        ("French", "fra"),
        ("unknown", "eng"),
        (None, "eng"),
    ],
)
def test_resolve_target_language_never_returns_russian(value: str | None, expected: str) -> None:
    assert resolve_target_language(value) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", "eng"),
        ("你好", "cmn"),
        ("東京", "cmn"),
        ("日本語", "cmn"),
        ("こんにちは", "jpn"),
        ("日本語です", "jpn"),
        ("한국어", "kor"),
        ("العربية", "arb"),
        ("हिन्दी", "hin"),
        ("বাংলা", "ben"),
        ("ไทย", "tha"),
        ("hello 世界", "eng"),
        ("Привет", None),
        ("Ελληνικά", None),
        ("hello Привет", None),
        ("", None),
        ("42!", None),
    ],
)
def test_detect_script_language(text: str, expected: str | None) -> None:
    assert detect_script_language(text) == expected


def test_labels_come_from_the_registry() -> None:
    assert language_badge("fra") == "🇫🇷 Французский"
    assert language_badge("unknown") == "🇬🇧 Английский"
    assert language_target_phrase("fra") == "на французском"
    assert language_target_phrase("cmn") == "на китайском"
    assert language_target_phrase("hin") == "на хинди"
    assert language_name_en("cmn") == "Chinese (Mandarin)"
    assert language_name_ru("cmn") == "Китайский"


def test_arabic_is_the_only_rtl_supported_language() -> None:
    assert [language.code for language in SUPPORTED_LANGUAGES if language.rtl] == ["arb"]
