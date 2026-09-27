"""Portable, bounded business records with explicit reference validation.

Evidence is append-only in discovery. Bounds fail closed; exceeding them must be
surfaced as partial by the caller, never implemented by dropping old sources.
"""
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_EVIDENCE = 4096
MAX_STATE_CHARS = 4_000_000


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value  # Opaque IDs and source text are NEVER normalised.


Identifier = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_nonblank)]
Text = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_nonblank)]
ShortText = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_nonblank)]
Refs = Annotated[list[Identifier], Field(max_length=128)]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class Evidence(DTO):
    id: Identifier
    kind: Literal["transcript", "screen_observation", "correction", "system"]
    text: Annotated[str, Field(min_length=1, max_length=12000), AfterValidator(_nonblank)]
    actor: Literal["client", "facilitator", "agent", "observer", "system"]
    at_ms: int = Field(ge=0, strict=True)
    consent_epoch: int = Field(ge=0, strict=True)
    seq: int = Field(default=0, ge=0, strict=True)
    source_ref: Annotated[str, Field(max_length=1000)] | None = None


class Claim(DTO):
    id: Identifier
    text: Text
    evidence_ids: Refs
    status: Literal["observed", "reported", "inferred", "unknown", "contradicted"]


class WorkflowStep(DTO):
    id: Identifier
    title: ShortText
    actor: ShortText
    system: ShortText
    description: Text
    evidence_ids: Refs


class WorkflowEdge(DTO):
    id: Identifier
    source: Identifier
    target: Identifier
    kind: Literal["next", "conditional", "rework", "handoff"]
    label: str = Field(max_length=500)


class Probe(DTO):
    id: Identifier
    text: ShortText
    reason: ShortText
    evidence_ids: Refs
    based_on_revision: int = Field(ge=0, strict=True)
    consent_epoch: int = Field(ge=0, strict=True)


def _unique_ids(items: list) -> set[str]:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate record id")
    return set(ids)


def validate_refs(refs: list[str], existing: set[str], *, required: bool = False) -> None:
    if required and not refs:
        raise ValueError("evidence references required")
    if len(refs) != len(set(refs)) or not set(refs).issubset(existing):
        raise ValueError("duplicate or missing evidence reference")


class _GraphRecords(DTO):
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    claims: list[Claim] = Field(default_factory=list, max_length=512)
    steps: list[WorkflowStep] = Field(default_factory=list, max_length=256)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=512)
    unknowns: list[Text] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        evidence_ids = _unique_ids(self.evidence)
        _unique_ids(self.claims)
        step_ids = _unique_ids(self.steps)
        _unique_ids(self.edges)
        by_id = {event.id: event for event in self.evidence}
        for claim in self.claims:
            validate_refs(claim.evidence_ids, evidence_ids, required=claim.status != "unknown")
            if claim.status != "unknown" and not any(
                by_id[ref].actor not in {"agent", "system"} and by_id[ref].kind != "system"
                for ref in claim.evidence_ids
            ):
                raise ValueError("business claims require client, facilitator or observer evidence")
            if claim.status == "observed" and any(
                by_id[ref].kind == "screen_observation" for ref in claim.evidence_ids
            ):
                raise ValueError("screen observations are inferred/reported, not exact observed facts")
        for step in self.steps:
            validate_refs(step.evidence_ids, evidence_ids, required=True)
            if not any(by_id[ref].actor not in {"agent", "system"}
                       and by_id[ref].kind != "system" for ref in step.evidence_ids):
                raise ValueError("business workflow steps require client, facilitator or observer evidence")
        for edge in self.edges:
            if edge.source not in step_ids or edge.target not in step_ids:
                raise ValueError("edge endpoint does not exist")
        if len(self.model_dump_json()) > MAX_STATE_CHARS:
            raise ValueError("state capacity exceeded; preserve evidence and finish partial")
        return self


class Snapshot(_GraphRecords):
    schema_version: Literal["1"] = "1"
    title: ShortText
    pack_id: Literal["general", "quotation", "recurring_reporting"] = "general"
    revision: int = Field(default=0, ge=0, strict=True)
    consent_epoch: int = Field(default=0, ge=0, strict=True)
    last_event_seq: int = Field(default=0, ge=0, strict=True)
    coverage: list[ShortText] = Field(default_factory=list, max_length=32)
    probe: Probe | None = None

    @model_validator(mode="after")
    def validate_fences(self) -> Self:
        if any(e.consent_epoch > self.consent_epoch for e in self.evidence):
            raise ValueError("evidence from future consent epoch")
        if any(e.seq > self.last_event_seq for e in self.evidence):
            raise ValueError("evidence sequence exceeds snapshot cursor")
        positive_seqs = [e.seq for e in self.evidence if e.seq > 0]
        if positive_seqs != sorted(set(positive_seqs)):
            raise ValueError("evidence sequences must be unique and ordered")
        if self.probe:
            if (self.probe.based_on_revision != self.revision
                    or self.probe.consent_epoch != self.consent_epoch):
                raise ValueError("stale probe revision or consent epoch")
            validate_refs(self.probe.evidence_ids, {e.id for e in self.evidence})
        return self


class _InternalOpportunity(DTO):
    """Non-binding diligence only: no price, ROI, scope commitment, or commercial offer."""
    status: Literal["insufficient_evidence", "no_project", "discovery_only"] = "insufficient_evidence"
    non_binding: Literal[True] = True
    rationale: Text = "Insufficient evidence to propose an implementation."
    candidate_claim_ids: Refs = Field(default_factory=list)
    evidence_ids: Refs = Field(default_factory=list)
    validation_questions: list[Text] = Field(default_factory=list, max_length=160)
    next_step: Text = "Validate the evidence with the client before considering any project."


class DecoderProcessReceipt(DTO):
    """Bounded actual subprocess output evidence; no paths or raw media/logs."""
    returncode: int
    outcome: Literal["complete", "timeout", "cancelled", "output_limit"]
    stdout_bytes: int = Field(ge=0, strict=True)
    stderr_bytes: int = Field(ge=0, strict=True)
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


MediaFailure = Literal["identity_unbound", "object_changed", "download_failed", "size_limit",
    "size_mismatch", "decoder_unavailable", "probe_failed", "stream_policy", "decode_failed",
    "silent_audio", "timeout", "cancelled", "output_limit", "validation_failed"]


class MediaValidationReceipt(DTO):
    """Physical artifact validation only, never semantic/offset/endurance proof."""
    validator: Literal["ffmpeg-full-av-v1"] = "ffmpeg-full-av-v1"
    outcome: Literal["verified", "failed"]
    failure_code: MediaFailure | None = None
    validated_at: datetime
    bytes_downloaded: int = Field(default=0, ge=0, strict=True)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    object_etag: str | None = Field(default=None, max_length=500)
    object_version_id: str | None = Field(default=None, max_length=1000)
    ffprobe_version: str | None = Field(default=None, max_length=500)
    ffmpeg_version: str | None = Field(default=None, max_length=500)
    probe: DecoderProcessReceipt | None = None
    decode: DecoderProcessReceipt | None = None
    video_codec: Literal["h264"] | None = None
    audio_codec: Literal["aac"] | None = None
    duration_seconds: float | None = Field(default=None, gt=0, le=6000, allow_inf_nan=False)
    video_frames: int | None = Field(default=None, gt=0, strict=True)
    audio_samples: int | None = Field(default=None, gt=0, strict=True)
    audio_rms: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    audio_peak: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    audio_measurement: Literal["decoded_mono_s16le_16000hz"] = "decoded_mono_s16le_16000hz"
    offset_correspondence: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.validated_at.tzinfo is None:
            raise ValueError("Media validation requires a timezone")
        if self.outcome == "verified":
            if (self.failure_code is not None or not self.bytes_downloaded or not self.sha256
                    or not (self.object_etag or (self.object_version_id and self.object_version_id != "null"))
                    or not (self.ffprobe_version or "").startswith("ffprobe version ")
                    or not (self.ffmpeg_version or "").startswith("ffmpeg version ")
                    or not self.video_frames or not self.audio_samples
                    or not self.duration_seconds or not self.video_codec or not self.audio_codec
                    or not self.audio_rms or not self.audio_peak
                    or any(p is None or p.outcome != "complete" or p.returncode != 0
                           for p in (self.probe, self.decode))):
                raise ValueError("Full decode requires exact object and actual decoder evidence")
            if (not self.probe.stdout_bytes or self.probe.stderr_bytes
                    or self.decode.stdout_bytes != self.audio_samples * 2
                    or not self.decode.stderr_bytes or self.audio_peak < self.audio_rms):
                raise ValueError("Full decode counts must match measured process outputs")
        elif self.failure_code is None:
            raise ValueError("Failed validation requires a safe failure code")
        return self


class RecordingManifest(DTO):
    """Private exact-object receipt. Metadata is NOT a media/playback validation."""
    reservation_id: Identifier
    consent_epoch: int = Field(ge=0, strict=True)
    room_name: Identifier
    bucket: Identifier
    key: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_nonblank)]
    egress_id: Identifier
    terminal_status: Literal["complete", "failed", "aborted", "limit_reached"]
    artifact_status: Literal["metadata_verified", "unavailable"]
    media_validation: Literal["not_performed", "verified", "failed"] = "not_performed"
    validation_receipt: MediaValidationReceipt | None = None
    observed_at: datetime
    started_at_ns: int | None = Field(default=None, gt=0, strict=True)
    ended_at_ns: int | None = Field(default=None, gt=0, strict=True)
    file_started_at_ns: int | None = Field(default=None, gt=0, strict=True)
    file_ended_at_ns: int | None = Field(default=None, gt=0, strict=True)
    duration_ns: int | None = Field(default=None, gt=0, strict=True)
    size_bytes: int | None = Field(default=None, gt=0, strict=True)
    head_size_bytes: int | None = Field(default=None, gt=0, strict=True)
    content_type: str | None = Field(default=None, max_length=200)
    etag: str | None = Field(default=None, max_length=500)
    version_id: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.observed_at.tzinfo is None:
            raise ValueError("Recording observation requires a timezone")
        if self.artifact_status == "metadata_verified":
            if (self.terminal_status != "complete" or self.size_bytes is None
                    or self.head_size_bytes != self.size_bytes):
                raise ValueError("Artifact metadata requires COMPLETE and exact positive HEAD size")
        elif self.terminal_status == "complete" or any(value is not None for value in (
                self.size_bytes, self.head_size_bytes, self.content_type, self.etag, self.version_id)):
            raise ValueError("Failed cleanup cannot claim an artifact")
        receipt = self.validation_receipt
        if self.media_validation == "not_performed":
            if receipt is not None:
                raise ValueError("Unperformed media validation cannot have a receipt")
        elif (self.artifact_status != "metadata_verified" or receipt is None
                or receipt.outcome != self.media_validation):
            raise ValueError("Media outcome must match its reserved metadata receipt")
        if receipt and (receipt.object_etag != self.etag or receipt.object_version_id != self.version_id
                or (receipt.outcome == "verified" and receipt.bytes_downloaded != self.size_bytes)):
            raise ValueError("Media receipt does not match exact object metadata")
        return self


RECORDING_GAP = ("Required recording is not playback-verified. This is a recording-incomplete partial report; "
                 "retained text evidence remains available, but media replay and offset correspondence require validation.")


class RecordingTimeline(DTO):
    """Client-safe observation, deliberately without room, Egress or storage IDs."""
    consent_epoch: int = Field(ge=0, strict=True)
    terminal_status: Literal["complete", "failed", "aborted", "limit_reached", "unconfirmed"] = "unconfirmed"
    artifact_status: Literal["metadata_verified", "unavailable", "unverified"] = "unverified"
    media_validation: Literal["not_performed", "verified", "failed"] = "not_performed"
    validation_failure: MediaFailure | None = None
    validated_at: datetime | None = None
    video_frames: int | None = Field(default=None, gt=0, strict=True)
    audio_samples: int | None = Field(default=None, gt=0, strict=True)
    observed_at: datetime | None = None
    started_at_ns: int | None = Field(default=None, gt=0, strict=True)
    ended_at_ns: int | None = Field(default=None, gt=0, strict=True)
    file_started_at_ns: int | None = Field(default=None, gt=0, strict=True)
    file_ended_at_ns: int | None = Field(default=None, gt=0, strict=True)
    duration_ns: int | None = Field(default=None, gt=0, strict=True)


class RecordingProvenance(DTO):
    required: Literal[True] = True
    artifact_status: Literal["metadata_verified", "unavailable", "incomplete", "unverified"] = "unverified"
    provenance: Literal["persisted_manifest", "missing_manifest"] = "missing_manifest"
    cleanup_status: Literal["confirmed", "pending", "unknown"] = "unknown"
    media_validation: Literal["not_performed", "verified", "failed", "incomplete"] = "not_performed"
    required_segments: int = Field(default=0, ge=0, strict=True)
    settled_segments: int = Field(default=0, ge=0, strict=True)
    session_started_at: str | None = None
    evidence_epochs: list[int] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    unverified_evidence_epochs: list[int] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    segments: list[RecordingTimeline] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    timeline_basis: Literal["provider_unix_ns; evidence_session_ms; correspondence_unverified"] = (
        "provider_unix_ns; evidence_session_ms; correspondence_unverified")

    @property
    def decode_verified(self) -> bool:
        return (self.media_validation == "verified" and self.provenance == "persisted_manifest"
            and self.cleanup_status == "confirmed" and self.artifact_status == "metadata_verified"
            and self.required_segments > 0
            and self.required_segments == self.settled_segments == len(self.segments)
            and not self.unverified_evidence_epochs
            and set(self.evidence_epochs).issubset({s.consent_epoch for s in self.segments})
            and all(s.media_validation == "verified" and s.terminal_status == "complete"
                and s.artifact_status == "metadata_verified" and s.validated_at is not None
                and s.video_frames and s.audio_samples and s.validation_failure is None
                for s in self.segments))


class Report(_GraphRecords):
    schema_version: Literal["1"] = "1"
    session_id: Identifier
    revision: int = Field(ge=0, strict=True)
    title: ShortText
    summary: Text
    unknowns: list[Text] = Field(default_factory=list, max_length=160)
    recommendations: list[Text] = Field(max_length=160)
    status: Literal["complete", "partial"]
    # Legacy rows cannot recover content eligibility from an already-downgraded
    # delivery status. Only newly evaluated coverage explicitly supplies it.
    content_status: Literal["complete", "partial", "unknown"] = "unknown"
    internal_opportunity: dict
    generated_at: str
    recording: RecordingProvenance = Field(default_factory=RecordingProvenance)

    @model_validator(mode="before")
    @classmethod
    def legacy_recording_projection(cls, value):
        from pydantic import ValidationError
        if isinstance(value, dict):
            try:
                RecordingProvenance.model_validate(value.get("recording", {}))
            except ValidationError:
                # Corrupt historical media data cannot certify a report or prevent
                # delivery of valid retained text evidence. Do not mutate the row.
                return {**value, "recording": {}}
        return value

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        timestamp = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("generated_at must be an ISO UTC timestamp")
        opportunity = _InternalOpportunity.model_validate(self.internal_opportunity)
        validate_refs(opportunity.evidence_ids, {e.id for e in self.evidence})
        validate_refs(opportunity.candidate_claim_ids, {c.id for c in self.claims})
        if opportunity.candidate_claim_ids:
            candidates = [claim for claim in self.claims if claim.id in opportunity.candidate_claim_ids]
            if any(claim.status in {"unknown", "contradicted"} for claim in candidates):
                raise ValueError("Opportunity candidates must be current sourced claims")
            required_sources = {ref for claim in candidates for ref in claim.evidence_ids}
            if set(opportunity.evidence_ids) != required_sources:
                raise ValueError("Opportunity references must match its candidate claims' sources")
        self.internal_opportunity = opportunity.model_dump(mode="json")
        self.recording.evidence_epochs = sorted({event.consent_epoch for event in self.evidence})
        verified_epochs = {s.consent_epoch for s in self.recording.segments if s.media_validation == "verified"}
        verified_epochs -= {s.consent_epoch for s in self.recording.segments if s.media_validation != "verified"}
        if self.recording.provenance == "missing_manifest":
            verified_epochs = set()
        self.recording.unverified_evidence_epochs = sorted(set(self.recording.evidence_epochs) - verified_epochs)
        media_ready = self.recording.decode_verified
        if not media_ready and self.recording.media_validation == "verified":
            self.recording.media_validation = "incomplete"
        # Content eligibility is independently evaluated from coverage by build_report
        # and rechecked against the persisted snapshot at publication. Never infer it
        # from a legacy delivery status, a no_project rationale, or playable video.
        if media_ready:
            self.unknowns = [gap for gap in self.unknowns if gap != RECORDING_GAP]
            self.recommendations = [r for r in self.recommendations
                if r != "Clarify before deciding on any change: " + RECORDING_GAP]
            self.internal_opportunity["validation_questions"] = [q for q in
                self.internal_opportunity["validation_questions"] if q != RECORDING_GAP]
        elif RECORDING_GAP not in self.unknowns:
            if len(self.unknowns) >= 160:
                raise ValueError("Report capacity exceeded; recording limitation must not be omitted")
            self.unknowns.append(RECORDING_GAP)
        complete = (media_ready and self.content_status == "complete" and not self.unknowns
            and bool(self.evidence) and bool(self.steps)
            and any(c.status in {"reported", "observed"} for c in self.claims))
        self.status = "complete" if complete else "partial"
        if complete:
            self.summary = self.summary.replace("This partial review documents", "This completed review documents")
        else:
            self.summary = self.summary.replace("This completed review documents", "This partial review documents")
        return self
