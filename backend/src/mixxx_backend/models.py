from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Unit = Annotated[float, Field(ge=0, le=1)]
Language = Literal[
    "hindi", "punjabi", "english", "mixed", "instrumental", "unknown"
]
Operation = Literal[
    "load_track",
    "set_playing",
    "stop",
    "seek",
    "set_bpm",
    "set_sync",
    "set_volume",
    "set_crossfader",
    "set_eq",
    "set_effect",
    "transition",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class TrackRegister(Model):
    path: str = Field(min_length=1, max_length=4096)


class TrackMetadata(Model):
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(min_length=1, max_length=300)
    language: Language = "unknown"
    explicit: bool | None = None
    bpm: float | None = Field(default=None, ge=20, le=300)
    camelot_key: str | None = Field(
        default=None, pattern=r"^(?:[1-9]|1[0-2])[AB]$"
    )
    energy: float | None = Field(default=None, ge=0, le=10)


class Rules(Model):
    duration_minutes: int = Field(default=60, ge=1, le=720)
    language_mix: dict[Language, Unit] = Field(default_factory=dict)
    explicit_allowed: bool = False
    allow_unrated_content: bool = False
    artist_repeat_minutes: int = Field(default=30, ge=0, le=360)
    max_same_language_in_row: int = Field(default=4, ge=1, le=20)
    blocked_tracks: list[str] = Field(default_factory=list, max_length=1000)
    blocked_artists: list[str] = Field(default_factory=list, max_length=1000)
    preferred_bpm_min: float = Field(default=60, ge=20, le=300)
    preferred_bpm_max: float = Field(default=180, ge=20, le=300)
    target_energy: float = Field(default=5, ge=0, le=10)
    transition_overlap_seconds: float = Field(default=8, ge=1, le=30)

    @model_validator(mode="after")
    def coherent(self):
        if (
            self.language_mix
            and abs(sum(self.language_mix.values()) - 1) > 0.0001
        ):
            raise ValueError("Language weights must sum to 1")
        if self.preferred_bpm_min > self.preferred_bpm_max:
            raise ValueError("Minimum BPM exceeds maximum")
        return self


class SessionCreate(Model):
    name: str = Field(default="Party", min_length=1, max_length=100)
    settings: Rules = Field(default_factory=Rules)


class QueueEdit(Model):
    revision: int = Field(ge=0)
    track_ids: list[str] = Field(min_length=1, max_length=1000)


class Revision(Model):
    revision: int = Field(ge=0)


class CommandInput(Model):
    command_type: Operation
    deck: Annotated[int, Field(strict=True, ge=1, le=2)] | None = None
    track_id: str | None = Field(default=None, max_length=64)
    playing: bool | None = None
    enabled: bool | None = None
    value: float | None = None
    band: Literal["low", "mid", "high"] | None = None
    effect_unit: Annotated[int, Field(strict=True, ge=1, le=4)] | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=30000)

    @model_validator(mode="after")
    def coherent(self):
        operation = self.command_type
        fields = {
            "load_track": {"deck", "track_id"},
            "set_playing": {"deck", "playing"},
            "stop": set(),
            "seek": {"deck", "value"},
            "set_bpm": {"deck", "value"},
            "set_sync": {"deck", "enabled"},
            "set_volume": {"deck", "value"},
            "set_crossfader": {"value"},
            "set_eq": {"deck", "band", "value"},
            "set_effect": {"effect_unit", "enabled"},
            "transition": {"deck", "duration_ms"},
        }[operation]
        provided = {
            k
            for k, v in self.model_dump().items()
            if k != "command_type" and v is not None
        }
        if provided != fields:
            raise ValueError(f"{operation} requires exactly {sorted(fields)}")
        if self.value is not None:
            limits = {
                "seek": (0, 1),
                "set_bpm": (20, 300),
                "set_volume": (0, 1),
                "set_crossfader": (-1, 1),
                "set_eq": (0, 2),
            }[operation]
            if not limits[0] <= self.value <= limits[1]:
                raise ValueError(f"value must be within {limits}")
        return self


class DeckState(Model):
    track_id: str | None = Field(default=None, max_length=64)
    playing: bool
    position: float
    bpm: float = Field(ge=0)
    volume: Unit
    sync_enabled: bool
    eq: dict[Literal["low", "mid", "high"], float]

    @model_validator(mode="after")
    def complete_eq(self):
        if set(self.eq) != {"low", "mid", "high"}:
            raise ValueError("All three EQ bands are required")
        return self


class PlaybackState(Model):
    sequence: int = Field(ge=0)
    decks: dict[Literal["1", "2"], DeckState]
    crossfader: float = Field(ge=-1, le=1)
    effects: dict[Literal["1", "2", "3", "4"], bool] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def complete_decks(self):
        if set(self.decks) != {"1", "2"}:
            raise ValueError("Both deck states are required")
        return self


class BridgeRegister(Model):
    protocol_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    name: str = Field(min_length=1, max_length=100)
    capabilities: list[Operation] = Field(min_length=1, max_length=11)


class BridgeState(Model):
    instance_id: str = Field(min_length=1, max_length=64)
    state: PlaybackState


class BridgeClaim(Model):
    instance_id: str = Field(min_length=1, max_length=64)


class Acknowledge(BridgeState):
    success: bool
    error: str | None = Field(default=None, max_length=300)


class Reconcile(Model):
    outcome: Literal["confirmed", "not_executed"]
