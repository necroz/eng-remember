from eng_remember.domain.cloze import cloze


def test_replaces_the_word() -> None:
    assert cloze("The landlord raised the rent.", "landlord") == "The ____ raised the rent."


def test_matches_inflected_forms_by_stem() -> None:
    assert cloze("He runs every morning.", "run") == "He ____ every morning."


def test_matches_multiword_phrase_as_one_blank() -> None:
    assert cloze("He always gets away with it.", "get away with") == "He always ____ it."


def test_partial_phrase_match_leaves_example_untouched() -> None:
    assert cloze("They came up with a plan.", "come up with") == "They came up with a plan."


def test_case_insensitive_and_replaces_all() -> None:
    assert cloze("Landlord and landlord again.", "landlord") == "____ and ____ again."


def test_absent_word_leaves_example_untouched() -> None:
    assert cloze("No match here.", "ubiquitous") == "No match here."


def test_punctuation_is_preserved() -> None:
    assert cloze("A landlord, obviously!", "landlord") == "A ____, obviously!"


def test_chinese_term_is_found_inside_unspaced_text() -> None:
    assert cloze("我正在学习中文。", "中文", "cmn") == "我正在学习____。"


def test_japanese_term_is_found_inside_unspaced_text() -> None:
    assert cloze("私は日本語を勉強します。", "日本語", "jpn") == "私は____を勉強します。"


def test_arabic_diacritics_are_part_of_the_hidden_word() -> None:
    assert cloze("هذا كِتَابٌ جديد", "كتاب", "arb") == "هذا ____ جديد"


def test_french_apostrophe_is_a_token_separator() -> None:
    assert cloze("Il a réservé l’hôtel.", "hôtel", "fra") == "Il a réservé l’____."


def test_apostrophes_and_hyphens_are_blanked_with_their_terms() -> None:
    assert cloze("I don't know.", "don't", "eng") == "I ____ know."
    assert cloze("A well-known problem.", "well-known", "eng") == "A ____ problem."


def test_french_elision_is_blanked_with_the_term() -> None:
    assert cloze("Il a réservé l’hôtel.", "l’hôtel", "fra") == "Il a réservé ____."


def test_thai_term_is_blanked_by_substring() -> None:
    assert cloze("ฉันเรียนภาษาไทยทุกวัน", "ภาษาไทย", "tha") == "ฉันเรียน____ทุกวัน"


def test_overlapping_phrases_do_not_corrupt_the_blank() -> None:
    assert cloze("la la la", "la la", "eng") == "____"
