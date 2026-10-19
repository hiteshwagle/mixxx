import pytest
from pydantic import ValidationError

from mixxx_backend.library import BackendError
from mixxx_backend.models import Rules
from mixxx_backend.planner import blocked_reason, plan, validate_queue


def track(identifier, **values):
    return {
        "id": str(identifier),
        "artist": f"Artist {identifier}",
        "language": "english",
        "explicit": False,
        "available": True,
        "bpm": 100,
        "energy": 5,
        "duration_seconds": 180,
        **values,
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"available": False}, "unavailable"),
        ({"explicit": True}, "explicit_content"),
        ({"explicit": None}, "unrated_content"),
        ({"bpm": None}, "bpm"),
        ({"bpm": 220}, "bpm"),
        ({"duration_seconds": 5}, "too_short"),
    ],
)
def test_fail_closed_policy(changes, reason):
    assert blocked_reason(track(1, **changes), Rules()) == reason


def test_blocklists_and_content_overrides():
    assert (
        blocked_reason(track(1), Rules(blocked_tracks=["1"]))
        == "blocked_track"
    )
    assert (
        blocked_reason(track(1), Rules(blocked_artists=["ARTIST 1"]))
        == "blocked_artist"
    )
    assert (
        blocked_reason(
            track(1, explicit=None), Rules(allow_unrated_content=True)
        )
        is None
    )
    assert (
        blocked_reason(track(1, explicit=True), Rules(explicit_allowed=True))
        is None
    )


def test_duplicates_artist_cooldown_and_language_streak():
    with pytest.raises(BackendError, match="repeats a recording"):
        validate_queue([track(1), track(1)], Rules())
    with pytest.raises(BackendError, match="Artist repeats"):
        validate_queue([track(1), track(2, artist="ARTIST 1")], Rules())
    with pytest.raises(BackendError, match="consecutive songs"):
        validate_queue([track(1), track(2)], Rules(max_same_language_in_row=1))


def test_full_runtime_coverage_includes_overlap():
    result = plan([track(1), track(2)], Rules(duration_minutes=6))
    assert result["coverage_seconds"] == 352
    assert result["complete"] is False
    result = plan([track(1), track(2), track(3)], Rules(duration_minutes=6))
    assert result["complete"] is True
    assert len(result["track_ids"]) == 3


def test_language_mix_cannot_be_claimed_when_only_one_language_exists():
    rules = Rules(
        duration_minutes=10, language_mix={"english": 0.5, "hindi": 0.5}
    )
    result = plan([track(i) for i in range(4)], rules)
    assert result["complete"] is False
    assert result["language_balance_ready"] is False
    assert "LANGUAGE_BALANCE" in result["blockers"]


def test_language_weights_and_energy_preference():
    rules = Rules(
        duration_minutes=5, language_mix={"english": 0.5, "hindi": 0.5}
    )
    result = plan(
        [
            track(1),
            track(2, language="hindi"),
            track(3, explicit=True),
        ],
        rules,
    )
    assert result["complete"] is True
    assert result["language_counts"] == {"english": 1, "hindi": 1}
    quiet = plan(
        [track(1, energy=0), track(2, energy=5)],
        Rules(duration_minutes=1, target_energy=0),
    )
    assert quiet["track_ids"] == ["1"]


@pytest.mark.parametrize(
    "data",
    [
        {"language_mix": {"english": 0.4}},
        {"language_mix": {"english": -1, "hindi": 2}},
        {"language_mix": {"not_a_language": 1}},
        {"preferred_bpm_min": 150, "preferred_bpm_max": 100},
        {"transition_overlap_seconds": 31},
        {"explicit_allowed": "false"},
        {"target_energy": float("nan")},
    ],
)
def test_invalid_rules(data):
    with pytest.raises(ValidationError):
        Rules(**data)
