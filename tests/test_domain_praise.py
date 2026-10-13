"""The praise bank and the rule that keeps it from repeating."""

from __future__ import annotations

import itertools
import random
import re

import pytest

from eng_remember.domain.praise import (
    BANKS,
    PraiseCategory,
    category_for_rating,
    empty_history,
    render,
    select,
)

CATEGORIES = list(PraiseCategory)
NON_MILESTONE = [c for c in CATEGORIES if c is not PraiseCategory.MILESTONE]


class TestBank:
    def test_every_category_has_a_deep_bank(self) -> None:
        """Deep enough that a repeat stays out of sight for a long time."""
        assert len(BANKS[PraiseCategory.GOOD]) >= 40
        assert len(BANKS[PraiseCategory.EASY]) >= 25
        assert len(BANKS[PraiseCategory.HARD]) >= 25
        assert len(BANKS[PraiseCategory.AGAIN]) >= 25
        assert len(BANKS[PraiseCategory.MILESTONE]) >= 20

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_templates_are_unique(self, category: PraiseCategory) -> None:
        bank = BANKS[category]
        assert len(set(bank)) == len(bank), "duplicate template"

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_templates_are_one_or_two_sentences(self, category: PraiseCategory) -> None:
        for template in BANKS[category]:
            sentences = [part for part in re.split(r"[.!?]+\s*", template) if part.strip()]
            assert 1 <= len(sentences) <= 2, f"too long or too short: {template!r}"

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_templates_have_no_stray_braces(self, category: PraiseCategory) -> None:
        for template in BANKS[category]:
            for placeholder in re.findall(r"\{(\w+)\}", template):
                assert placeholder in {"term", "interval", "streak"}, template
            # Every brace must belong to a placeholder, not sit alone.
            assert template.count("{") == template.count("}"), template

    def test_milestone_templates_mention_the_streak(self) -> None:
        for template in BANKS[PraiseCategory.MILESTONE]:
            assert "{streak}" in template, template

    def test_regular_templates_do_not_mention_the_streak(self) -> None:
        """Streak numbers belong to the milestone bank; elsewhere they would
        have to be conditional and read wrong on day one."""
        for category in NON_MILESTONE:
            for template in BANKS[category]:
                assert "{streak}" not in template, template

    def test_no_template_gender_the_learner(self) -> None:
        """The bot does not know the user's gender, so it never assumes one."""
        # Masculine past-tense verbs and gendered compliments. Reflexive verbs
        # are matched by ending, the rest by stem, and plain nouns ending in -ал
        # ("интервал") are deliberately not caught — the point is to flag a
        # template that addresses a man, not to inflect Russian.
        forbidden = re.compile(
            # Whole-word forms only: "вспомнилось" is neuter and fine, while
            # a greedy stem would flag it.
            r"\b(?:"
            r"запомнил(?:а)?|вспомнил(?:а)?|достал(?:а)?|справил(?:ся|ась)|"
            r"сделал(?:а)?|сумел(?:а)?|смог(?:ла)?|успел(?:а)?|повторил(?:а)?|"
            r"выучил(?:а)?|победил(?:а)?|"
            r"молодец|умница|красав\w*|\w+л(?:ся|ась)"
            r")\b",
            re.IGNORECASE,
        )
        for bank in BANKS.values():
            for template in bank:
                match = forbidden.search(template.lower())
                assert match is None, f"{template!r} -> {match.group(0) if match else ''}"


class TestSelection:
    def test_a_pick_comes_from_the_right_bank(self) -> None:
        pick = select(PraiseCategory.EASY, rng=random.Random(0))

        assert pick.text in BANKS[PraiseCategory.EASY]
        assert pick.category is PraiseCategory.EASY
        assert pick.key.startswith("easy:")

    def test_the_bank_is_exhausted_before_anything_repeats(self) -> None:
        """The whole point: no template twice until all of them have been used."""
        rng = random.Random(7)
        history = empty_history()
        seen: list[str] = []
        for _ in range(len(BANKS[PraiseCategory.GOOD])):
            pick = select(PraiseCategory.GOOD, history, rng=rng)
            history = pick.history
            seen.append(pick.text)

        assert len(set(seen)) == len(seen)

    def test_no_immediate_repeat_across_a_cycle_boundary(self) -> None:
        """The one place a repeat could slip through is the seam."""
        rng = random.Random(11)
        history = empty_history()
        bank_size = len(BANKS[PraiseCategory.HARD])
        picks: list[str] = []
        for _ in range(bank_size * 2):
            pick = select(PraiseCategory.HARD, history, rng=rng)
            history = pick.history
            picks.append(pick.text)

        for previous, current in itertools.pairwise(picks):
            assert previous != current

    def test_long_run_stays_varied(self) -> None:
        """300 draws must not collapse onto a handful of templates."""
        rng = random.Random(3)
        history = empty_history()
        picks: list[str] = []
        for _ in range(300):
            pick = select(PraiseCategory.GOOD, history, rng=rng)
            history = pick.history
            picks.append(pick.text)

        assert len(set(picks)) == len(BANKS[PraiseCategory.GOOD])

    def test_categories_keep_separate_histories(self) -> None:
        history = empty_history()
        history = select(PraiseCategory.GOOD, history, rng=random.Random(1)).history
        history = select(PraiseCategory.EASY, history, rng=random.Random(1)).history

        assert set(history) == {"good", "easy"}

    def test_history_survives_a_round_trip_through_json(self) -> None:
        """History is stored as JSON on the user row."""
        import json

        history = empty_history()
        for _ in range(5):
            history = select(PraiseCategory.AGAIN, history, rng=random.Random(2)).history

        restored = json.loads(json.dumps(history))

        assert restored == history
        pick = select(PraiseCategory.AGAIN, restored, rng=random.Random(2))
        assert pick.text in BANKS[PraiseCategory.AGAIN]

    def test_damaged_history_is_ignored_rather_than_fatal(self) -> None:
        """A shorter bank after an edit must not raise."""
        pick = select(PraiseCategory.EASY, {"easy": [0, 1, 99999, -3]}, rng=random.Random(0))

        assert pick.text in BANKS[PraiseCategory.EASY]

    def test_the_first_pick_never_repeats_the_previous_one(self) -> None:
        rng = random.Random(5)
        history = empty_history()
        used: set[str] = set()
        for _ in range(len(BANKS[PraiseCategory.MILESTONE])):
            pick = select(PraiseCategory.MILESTONE, history, rng=rng)
            history = pick.history
            used.add(pick.text)

        assert len(used) == len(BANKS[PraiseCategory.MILESTONE])


class TestCategoryForRating:
    @pytest.mark.parametrize(
        ("rating", "expected"),
        [
            ("good", PraiseCategory.GOOD),
            ("easy", PraiseCategory.EASY),
            ("hard", PraiseCategory.HARD),
            ("again", PraiseCategory.AGAIN),
        ],
    )
    def test_each_rating_maps_to_its_bank(self, rating: str, expected: PraiseCategory) -> None:
        assert category_for_rating(rating) is expected

    def test_an_unknown_rating_falls_back_to_good(self) -> None:
        assert category_for_rating("whatever") is PraiseCategory.GOOD

    def test_a_missed_card_is_never_congratulated(self) -> None:
        """``again`` means the word was forgotten: support, not praise."""
        assert category_for_rating("again") is not PraiseCategory.GOOD


class TestRender:
    def test_placeholders_are_filled(self) -> None:
        assert render("«{term}» через {interval}.", term="run", interval="3 дн") == (
            "«run» через 3 дн."
        )

    def test_unknown_placeholders_stay_visible(self) -> None:
        """A typo in one of ~140 templates must not break a review."""
        assert render("Привет, {unknown}!", term="run") == "Привет, {unknown}!"

    def test_every_template_renders(self) -> None:
        for bank in BANKS.values():
            for template in bank:
                text = render(template, term="run", interval="3 дн", streak="7")
                assert "{" not in text, template
