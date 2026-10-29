from eng_remember.domain.term_match import contains_term, term_spans


def test_chinese_term_is_a_substring() -> None:
    assert contains_term("我正在学习中文。", "中文", "cmn")
    assert term_spans("我正在学习中文。", "中文", "cmn") == [(5, 7)]


def test_japanese_term_is_a_substring() -> None:
    assert contains_term("私は日本語を勉強します。", "日本語", "jpn")


def test_arabic_diacritics_do_not_hide_the_word() -> None:
    assert contains_term("هذا كِتَابٌ جديد", "كتاب", "arb")


def test_latin_forms_match_by_stem() -> None:
    assert contains_term("They are negotiating the price.", "negotiate", "eng")
    assert contains_term("runs", "run", "eng")


def test_latin_diacritics_are_significant() -> None:
    assert not contains_term("The hotel is open.", "hôtel", "fra")
    assert not contains_term("cafe", "café", "fra")


def test_empty_inputs_never_match() -> None:
    assert not contains_term("text", "")
    assert not contains_term("", "word")


def test_consecutive_phrase_parts_become_one_span() -> None:
    text = "He always gets away with it."

    assert term_spans(text, "get away with", "eng") == [(10, 24)]


def test_non_consecutive_phrase_parts_are_hidden_separately() -> None:
    text = "With it, get away."

    spans = term_spans(text, "get away with", "eng")

    assert [text[start:end] for start, end in spans] == ["With", "get", "away"]
