from eng_remember.domain.examples import merge_examples, pick_example


def test_rotation_cycles_through_examples() -> None:
    assert pick_example(["a", "b", "c"], None, 0) == "a"
    assert pick_example(["a", "b", "c"], None, 3) == "a"
    assert pick_example(["a", "b", "c"], None, 5) == "c"


def test_fallback_when_list_empty() -> None:
    assert pick_example([], "legacy", 2) == "legacy"
    assert pick_example([], None, 2) is None


def test_merge_puts_example_first_and_dedupes() -> None:
    assert merge_examples("One.", ["two.", "ONE.", "three."]) == ["One.", "two.", "three."]


def test_merge_limit_truncates_after_dedupe() -> None:
    assert merge_examples("One.", ["two.", "three."], limit=2) == ["One.", "two."]


def test_merge_skips_blank_entries() -> None:
    assert merge_examples(None, ["", "  ", "Real."]) == ["Real."]
