from eng_remember.domain.answers import (
    Verdict,
    matches,
    normalize,
    rating_for,
    translation_variants,
)
from eng_remember.domain.enums import ReviewRating


def test_normalize_ignores_case_yo_punctuation_and_hyphens() -> None:
    assert normalize("  Вездесущий, Ёлка! По-русски…  ") == "вездесущий елка по русски"


def test_translation_variants_split_on_commas_semicolons_and_slashes() -> None:
    assert translation_variants("ужасный, страшный; жуткий/кошмарный") == [
        "ужасный страшный жуткий кошмарный",
        "ужасный",
        "страшный",
        "жуткий",
        "кошмарный",
    ]


def test_matches_accepts_any_variant_and_user_separators() -> None:
    assert matches("Кошмарный!", "ужасный, кошмарный")
    assert matches("страшный, кошмарный", "ужасный, кошмарный")
    assert not matches("вездесущий", "ужасный, кошмарный")


def test_rating_for_uses_speed_only_for_correct_answers() -> None:
    assert (
        rating_for(Verdict.CORRECT, duration_seconds=4.9, easy_threshold_seconds=5.0)
        is ReviewRating.EASY
    )
    assert (
        rating_for(Verdict.CORRECT, duration_seconds=5.0, easy_threshold_seconds=5.0)
        is ReviewRating.GOOD
    )
    assert (
        rating_for(Verdict.CORRECT, duration_seconds=None, easy_threshold_seconds=5.0)
        is ReviewRating.GOOD
    )
    assert (
        rating_for(Verdict.ALMOST, duration_seconds=1.0, easy_threshold_seconds=5.0)
        is ReviewRating.HARD
    )
    assert (
        rating_for(Verdict.WRONG, duration_seconds=1.0, easy_threshold_seconds=5.0)
        is ReviewRating.AGAIN
    )


def test_hint_caps_fast_correct_at_good() -> None:
    assert (
        rating_for(
            Verdict.CORRECT,
            duration_seconds=1.0,
            easy_threshold_seconds=5.0,
            hint_used=True,
        )
        is ReviewRating.GOOD
    )


def test_hint_does_not_touch_other_verdicts() -> None:
    assert (
        rating_for(
            Verdict.ALMOST,
            duration_seconds=1.0,
            easy_threshold_seconds=5.0,
            hint_used=True,
        )
        is ReviewRating.HARD
    )
    assert (
        rating_for(
            Verdict.WRONG,
            duration_seconds=1.0,
            easy_threshold_seconds=5.0,
            hint_used=True,
        )
        is ReviewRating.AGAIN
    )
