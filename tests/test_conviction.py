from polywhale.conviction import ConvictionProfile, from_clips


def test_empty_clips_zero_score():
    p = from_clips([])
    assert p.median_clip_usd == 0.0
    assert p.conviction_score == 0.0


def test_large_consistent_clips_high_conviction():
    # Repeated $50 clips with tiny variation -> high conviction.
    p = from_clips([50.0] * 30 + [48.0, 52.0, 49.5, 50.5])
    assert p.median_clip_usd == 50.0
    assert p.repeat_size_ratio > 0.95
    assert p.conviction_score > 0.85


def test_tiny_sprayed_clips_low_conviction():
    # $0.20 median, no repetition pattern.
    p = from_clips([0.1, 0.5, 0.2, 0.7, 0.3, 0.8, 0.15, 0.4, 0.9, 0.25])
    assert p.median_clip_usd < 1.0
    assert p.conviction_score < 0.30


def test_large_but_random_sizes_moderate_score():
    # Big clips but no repeat pattern -> credit only for size.
    p = from_clips([10, 25, 100, 7, 45, 200, 80, 3, 150, 60])
    assert p.median_clip_usd >= 25.0
    assert p.conviction_score >= 0.30
    assert p.conviction_score < 0.95


def test_p90_and_max_track_distribution():
    # n=11; sorted, the 90th percentile by nearest-rank lands at index 9.
    p = from_clips([1.0] * 10 + [100.0])
    assert p.median_clip_usd == 1.0
    assert p.max_clip_usd == 100.0
    # With 10 small + 1 large, nearest-rank P90 still sits in the cluster of 1s.
    assert p.p90_clip_usd == 1.0


def test_p90_picks_top_decile_with_enough_outliers():
    # n=11, P90 index = int(0.9 * 10) = 9 -> the 10th element (sorted).
    p = from_clips([1.0] * 9 + [50.0, 100.0])
    assert p.p90_clip_usd == 50.0
    assert p.max_clip_usd == 100.0


def test_score_clamped_to_unit_interval():
    p = from_clips([10000.0] * 100)
    assert 0.0 <= p.conviction_score <= 1.0
