from mixxx_backend.library import BackendError
from mixxx_backend.models import Rules


def blocked_reason(track, rules: Rules):
    if not track["available"]:
        return "unavailable"
    if track["id"] in rules.blocked_tracks:
        return "blocked_track"
    if track["artist"].casefold() in {
        artist.casefold() for artist in rules.blocked_artists
    }:
        return "blocked_artist"
    if track["explicit"] is True and not rules.explicit_allowed:
        return "explicit_content"
    if track["explicit"] is None and not rules.allow_unrated_content:
        return "unrated_content"
    if rules.language_mix and not rules.language_mix.get(track["language"], 0):
        return "language"
    if track["bpm"] is None or not (
        rules.preferred_bpm_min <= track["bpm"] <= rules.preferred_bpm_max
    ):
        return "bpm"
    if track["duration_seconds"] <= rules.transition_overlap_seconds:
        return "too_short"
    return None


def validate_queue(tracks, rules):
    seen, artist_times = set(), {}
    elapsed, last_language, streak = 0.0, None, 0
    for track in tracks:
        reason = blocked_reason(track, rules)
        if reason:
            raise BackendError("POLICY_BLOCKED", f"{track['id']}: {reason}")
        if track["id"] in seen:
            raise BackendError("DUPLICATE_TRACK", "Queue repeats a recording")
        seen.add(track["id"])
        artist = track["artist"].casefold()
        if (
            elapsed - artist_times.get(artist, -1e10)
            < rules.artist_repeat_minutes * 60
        ):
            raise BackendError("ARTIST_COOLDOWN", "Artist repeats too soon")
        artist_times[artist] = elapsed
        language = track["language"]
        streak = streak + 1 if language == last_language else 1
        if streak > rules.max_same_language_in_row:
            raise BackendError(
                "LANGUAGE_STREAK", "Too many consecutive songs in one language"
            )
        last_language = language
        elapsed += track["duration_seconds"] - rules.transition_overlap_seconds
    if tracks:
        elapsed += rules.transition_overlap_seconds
    return elapsed


def language_balance(tracks, rules):
    counts = {language: 0 for language in rules.language_mix}
    for track in tracks:
        counts[track["language"]] = counts.get(track["language"], 0) + 1
    balanced = all(
        abs(counts[language] - weight * len(tracks)) <= 1
        for language, weight in rules.language_mix.items()
    )
    return {"language_counts": counts, "language_balance_ready": balanced}


def plan(tracks, rules):
    eligible = [t for t in tracks if not blocked_reason(t, rules)]
    selected, counts = [], {}
    elapsed = 0.0
    target = rules.duration_minutes * 60
    while eligible and (
        elapsed < target
        or not language_balance(selected, rules)["language_balance_ready"]
    ):

        def score(track):
            language = track["language"]
            deficit = rules.language_mix.get(language, 0) * (len(selected) + 1)
            deficit -= counts.get(language, 0)
            energy = abs(
                (track["energy"] if track["energy"] is not None else 5)
                - rules.target_energy
            )
            bpm = abs(track["bpm"] - selected[-1]["bpm"]) if selected else 0
            return (-deficit, energy, bpm, track["id"])

        chosen = None
        for candidate in sorted(eligible, key=score):
            try:
                coverage = validate_queue(selected + [candidate], rules)
            except BackendError:
                continue
            chosen, elapsed = candidate, coverage
            break
        if chosen is None:
            break
        selected.append(chosen)
        counts[chosen["language"]] = counts.get(chosen["language"], 0) + 1
        eligible.remove(chosen)
    balance = language_balance(selected, rules)
    complete = elapsed >= target and balance["language_balance_ready"]
    blockers = []
    if elapsed < target:
        blockers.append("INSUFFICIENT_COVERAGE")
    if not balance["language_balance_ready"]:
        blockers.append("LANGUAGE_BALANCE")
    return {
        "track_ids": [track["id"] for track in selected],
        "coverage_seconds": elapsed,
        "requested_seconds": target,
        "complete": complete,
        **balance,
        "language_mix_basis": "track_count",
        "blockers": blockers,
    }
