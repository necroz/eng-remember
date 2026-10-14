from eng_remember.domain.leech import next_level, should_offer


def test_offers_at_threshold_multiples() -> None:
    assert should_offer(3, 0, threshold=4) is False
    assert should_offer(4, 0, threshold=4) is True
    assert should_offer(4, 1, threshold=4) is False
    assert should_offer(8, 1, threshold=4) is True


def test_next_level_uses_division_not_increment() -> None:
    assert next_level(4, threshold=4) == 1
    assert next_level(7, threshold=4) == 1
    assert next_level(8, threshold=4) == 2
