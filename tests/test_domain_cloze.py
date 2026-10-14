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
