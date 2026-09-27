"""Deterministic source-linked reports, with a separate non-binding internal view."""
from datetime import datetime, timezone
from html import escape

from .domain import RECORDING_GAP, RecordingManifest, RecordingProvenance, RecordingTimeline, Report, Snapshot
from .packs import get_pack

_GAPS = {
    "workflow": "The current workflow has not been established from a concrete example.",
    "exceptions": "Exception and rework paths have not been verified.",
    "approvals": "Approval authority and fallback arrangements have not been verified.",
    "baseline": "A baseline of frequency, time and errors has not been established; benefits cannot be estimated.",
    "readback": "The client has not confirmed the final workflow readback.",
}


def build_report(snapshot: Snapshot, session_id: str, *, partial: bool = False) -> Report:
    snapshot = Snapshot.model_validate(snapshot)
    pack = get_pack(snapshot.pack_id)
    missing = [area for area in pack["required_coverage"] if area not in snapshot.coverage]
    unknowns = [*snapshot.unknowns, *[_GAPS[area] for area in missing]]
    sourced = [claim for claim in snapshot.claims if claim.status in {"observed", "reported", "inferred"}]
    if not snapshot.evidence:
        unknowns.append("No business evidence was captured; this is not a completed workflow review.")
    if not sourced or not snapshot.steps:
        unknowns.append("There is insufficient sourced workflow detail to make an implementation recommendation.")
    if sourced and not any(claim.status in {"observed", "reported"} for claim in sourced):
        unknowns.append("The findings are inference only; client confirmation is required.")
    if any(claim.status == "unknown" for claim in snapshot.claims):
        unknowns.append("Explicit unknown claims remain in the findings and require clarification.")
    correction_ids = {event.id for event in snapshot.evidence if event.kind == "correction"}
    replacement_sources = {ref for claim in sourced if claim.status in {"observed", "reported"}
                           for ref in claim.evidence_ids}
    if any(claim.status == "contradicted" and not (
        set(claim.evidence_ids) & correction_ids & replacement_sources
    ) for claim in snapshot.claims):
        unknowns.append("Unresolved contradictory findings require a source-backed correction and readback.")
    latest_correction = max((event.seq for event in snapshot.evidence if event.kind == "correction"), default=-1)
    if latest_correction >= 0 and not any(
        event.seq > latest_correction and event.kind == "transcript" and event.actor == "client"
        for event in snapshot.evidence
    ):
        unknowns.append("The latest correction still requires a fresh client readback.")
    unknowns = list(dict.fromkeys(unknowns))
    discovery_partial = partial or bool(unknowns)
    unknowns.append(RECORDING_GAP)
    is_partial = partial or bool(unknowns)
    summary = (f"This {'partial' if is_partial else 'completed'} review documents "
               f"{len(snapshot.steps)} workflow steps and {len(sourced)} current source-linked assertions. "
               "Reported statements and model-derived screen observations are not independent verification.")
    if sourced:
        summary += " Recorded findings: " + " ".join(claim.text[:600] for claim in sourced[:3])
    recommendations = ["Clarify before deciding on any change: " + gap[:3500]
                       + (" … See the complete gap in Unknowns." if len(gap) > 3500 else "")
                       for gap in unknowns]
    if not recommendations:
        recommendations = ["Retain this source-linked workflow as the current-state record. "
                           "No implementation project or financial benefit is established by the workflow map alone."]
    opportunity = {
        "status": "insufficient_evidence" if discovery_partial else "no_project",
        "non_binding": True,
        "rationale": ("Open evidence gaps prevent an implementation recommendation." if discovery_partial else
                      "No implementation project is established by these findings. A documented process does not by itself justify automation."),
        "candidate_claim_ids": [], "evidence_ids": [],
        "validation_questions": unknowns,
        "next_step": "Validate any proposed scope, baseline, access permissions and technical feasibility separately. No price or delivery commitment is made.",
    }
    return Report(
        session_id=session_id, revision=snapshot.revision, title=snapshot.title, summary=summary,
        claims=snapshot.claims, steps=snapshot.steps, edges=snapshot.edges, evidence=snapshot.evidence,
        unknowns=unknowns, recommendations=recommendations, status="partial" if is_partial else "complete",
        content_status="partial" if discovery_partial else "complete",
        internal_opportunity=opportunity, generated_at=datetime.now(timezone.utc).isoformat(),
    )


def report_for_delivery(report: dict | Report) -> dict:
    """Read-time legacy-safe projection for JSON/report/export API owners."""
    return Report.model_validate(report).model_dump(mode="json")


def recording_provenance(session, evidence, reservations=(), manifests=()) -> RecordingProvenance:
    """Project private durable receipts; never infer media quality from file metadata."""
    epochs = sorted({e.consent_epoch for e in evidence})
    from pydantic import ValidationError
    by_reservation = {}
    invalid = False
    for value in manifests:
        try:
            manifest = RecordingManifest.model_validate(value)
        except (ValidationError, TypeError):
            invalid = True
            continue
        by_reservation.setdefault(manifest.reservation_id, []).append(manifest)
    segments = []
    for record in reservations:
        reservation = record["data"]
        observed = by_reservation.get(reservation["id"], [])
        matched = [m for m in observed if m.consent_epoch == reservation["consent_epoch"]
                   and m.room_name == reservation["room_name"]
                   and m.key == reservation["output_prefix"] + ".mp4"
                   and ("bucket" not in record or m.bucket == record["bucket"])
                   and ("egress_ids" not in record or m.egress_id in record["egress_ids"])]
        if len(matched) != len(observed) or len({m.egress_id for m in matched}) != len(matched):
            invalid = True
        for manifest in matched:
            segment = manifest.model_dump(include=set(RecordingTimeline.model_fields))
            if manifest.validation_receipt:
                receipt = manifest.validation_receipt
                segment.update(validation_failure=receipt.failure_code, validated_at=receipt.validated_at,
                    video_frames=receipt.video_frames, audio_samples=receipt.audio_samples)
            segments.append(RecordingTimeline.model_validate(segment))
        if not matched:
            segments.append(RecordingTimeline(consent_epoch=reservation["consent_epoch"]))
    metadata_epochs = {s.consent_epoch for s in segments if s.artifact_status == "metadata_verified"}
    # Every required segment matters, including reservations without text evidence
    # and multiple segments in one epoch. One good segment cannot mask a bad one.
    verified_epochs = {s.consent_epoch for s in segments if s.media_validation == "verified"}
    verified_epochs -= {s.consent_epoch for s in segments if s.media_validation != "verified"}
    missing_epochs = sorted(set(epochs) - verified_epochs)
    statuses = {s.artifact_status for s in segments}
    pending = any(not r["settled"] for r in reservations) or bool(session.get("recording_cleanup_pending"))
    if "metadata_verified" in statuses:
        outcome = "metadata_verified" if (statuses == {"metadata_verified"} and set(epochs) <= metadata_epochs
            and not pending and session.get("recording_status") != "failed") else "incomplete"
    elif session.get("recording_status") == "failed" or "unavailable" in statuses:
        outcome = "unavailable"
    else:
        outcome = "unverified"
    media_outcomes = {s.media_validation for s in segments}
    media_ready = (media_outcomes == {"verified"} and len(segments) == len(reservations)
        and bool(reservations) and not missing_epochs and not pending and not invalid
        and outcome == "metadata_verified")
    media_validation = ("verified" if media_ready else "failed" if "failed" in media_outcomes
        else "incomplete" if "verified" in media_outcomes or invalid else "not_performed")
    return RecordingProvenance(
        media_validation=media_validation, required_segments=len(reservations),
        settled_segments=sum(r["settled"] is True for r in reservations),
        artifact_status=outcome, provenance="persisted_manifest" if any(
            s.observed_at is not None for s in segments) else "missing_manifest",
        cleanup_status="pending" if pending else "confirmed" if reservations and all(
            r["settled"] for r in reservations) else "unknown",
        session_started_at=session.get("started_at"), evidence_epochs=epochs,
        unverified_evidence_epochs=missing_epochs, segments=segments)


def render_report_html(report: Report, *, internal: bool = False) -> str:
    report = Report.model_validate(report)
    def e(value) -> str:
        return escape(str(value), quote=True)
    # Numeric document-local anchors cannot interpret hostile opaque IDs as URLs/HTML.
    evidence_anchors = {item.id: f"evidence-{index}" for index, item in enumerate(report.evidence, 1)}
    step_anchors = {item.id: f"step-{index}" for index, item in enumerate(report.steps, 1)}
    step_titles = {item.id: item.title for item in report.steps}

    def sources(refs: list[str]) -> str:
        return " ".join(f'<a href="#{evidence_anchors[ref]}">[{e(ref)}]</a>' for ref in refs)

    def bullets(items: list[str], empty: str) -> str:
        return "<ul>" + "".join(f"<li>{e(item)}</li>" for item in items) + "</ul>" if items else f"<p>{e(empty)}</p>"

    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             f"<title>{e(report.title)} — BEEP review</title>",
             """<style>
:root{font-family:system-ui,sans-serif;color:#17262f;background:#eef1f3}
body{max-width:980px;margin:32px auto;background:white;padding:40px;line-height:1.55}
h1{line-height:1.15}h2{margin-top:36px;border-bottom:1px solid #ccd5da;padding-bottom:8px}
h3{margin-bottom:8px}p,li,td{overflow-wrap:anywhere}a{color:#14516f}small,.meta{color:#526570}
table{border-collapse:collapse;width:100%;font-size:.94em}th,td{border:1px solid #c9d2d8;padding:10px;text-align:left;vertical-align:top}
.workflow{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;padding:0;list-style:none}
.workflow li{border:1px solid #9fb4bf;padding:16px}.source{border-top:1px solid #d7dfe4;padding:14px 0;break-inside:avoid}
.source p{white-space:pre-wrap}.status{display:inline-block;background:#e9eef2;padding:4px 10px}
.internal{border:2px solid #865b29;padding:20px}.historical{color:#664d43}
@media print{body{margin:0;max-width:none;padding:0;background:white;font-size:10pt}:root{background:white}h2,h3{break-after:avoid}tr,.workflow li{break-inside:avoid}a{color:inherit;text-decoration:underline}@page{margin:16mm}}
</style></head><body>""",
             '<header><p>BEEP · Workflow review</p>', f"<h1>{e(report.title)}</h1>",
             f'<p class="meta">Session {e(report.session_id)} · Revision {report.revision} · Schema {e(report.schema_version)}<br>Generated {e(report.generated_at)}</p>',
             f'<span class="status">{e(report.status.upper())}</span></header>',
             f"<section><h2>Summary</h2><p>{e(report.summary)}</p></section>",
             '<section><h2>Recording outcome and provenance</h2>',
             f'<p><strong>Required recording:</strong> {e(report.recording.artifact_status)} · '
             f'Provenance: {e(report.recording.provenance)} · Cleanup: {e(report.recording.cleanup_status)}</p>',
             ('<p>Full audio/video decode verified for every required reserved segment. '
              'This validates the physical files only; evidence-to-file offset correspondence remains unverified. '
              'No semantic accuracy, uninterrupted capture, lost-frame absence or endurance is certified.</p>'
              if report.recording.decode_verified else
              '<p>Playback has not been validated for all required segments. A COMPLETE provider status and matching private-object HEAD '
              'verify metadata only, not playable audio/video or evidence-to-file offset correspondence. '
              'This recording-incomplete partial report preserves the text findings.</p>'),
             '<p>No public permanent media URL is provided.</p>',
             f'<p>Evidence consent epochs: {e(report.recording.evidence_epochs)}. '
             f'Epochs without full decode verification: {e(report.recording.unverified_evidence_epochs)}.</p>',
             '<p>Timeline: provider Unix nanoseconds where actually observed; evidence offsets are session-relative milliseconds. '
             'No continuous recording or synchronised playback mapping is asserted across pauses/resumes.</p>']
    for index, segment in enumerate(report.recording.segments, 1):
        parts.append(f'<p>Recording segment {index} · consent epoch {segment.consent_epoch} · '
                     f'{e(segment.terminal_status)} · {e(segment.artifact_status)} · decode: {e(segment.media_validation)}')
        for field in ("validation_failure", "validated_at", "video_frames", "audio_samples",
                      "observed_at", "started_at_ns", "ended_at_ns", "file_started_at_ns", "file_ended_at_ns", "duration_ns"):
            value = getattr(segment, field)
            if value is not None:
                parts.append(f'<br>{e(field)}: {e(value)}')
        parts.append('</p>')
    parts.append('</section><section><h2>Workflow map · Current state</h2><ol class="workflow">')
    for step in report.steps:
        parts.append(f'<li id="{step_anchors[step.id]}"><h3>{e(step.title)}</h3>'
                     f'<p><strong>Owner:</strong> {e(step.actor)}<br><strong>System:</strong> {e(step.system)}</p>'
                     f'<p>{e(step.description)}</p><p>{sources(step.evidence_ids)}</p></li>')
    parts.append("</ol>")
    if not report.steps:
        parts.append("<p>No sourced workflow map is available.</p>")
    parts.append("<h3>Transitions, approvals and exception paths</h3>")
    if report.edges:
        parts.append("<table><thead><tr><th>From</th><th>To</th><th>Path</th><th>Condition / handoff</th></tr></thead><tbody>")
        for edge in report.edges:
            parts.append(f'<tr><td><a href="#{step_anchors[edge.source]}">{e(step_titles[edge.source])}</a></td>'
                         f'<td><a href="#{step_anchors[edge.target]}">{e(step_titles[edge.target])}</a></td>'
                         f'<td>{e(edge.kind)}</td><td>{e(edge.label)}</td></tr>')
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No transitions have been established; this is not evidence that exceptions or approvals are absent.</p>")
    parts.append("</section><section><h2>Findings and provenance</h2><ul>")
    for claim in report.claims:
        parts.append(f'<li><strong>{e(claim.status)}</strong> · {e(claim.text)} {sources(claim.evidence_ids)}</li>')
    parts.append("</ul><h3>Baseline and benefit confidence</h3><p>Only source-linked measurements stated in the findings are available. "
                 "No savings, implementation feasibility or return on investment is asserted. Reported estimates require validation.</p></section>")
    parts.append("<section><h2>Unknowns and decision limits</h2>" + bullets(report.unknowns, "No recorded open discovery gaps. This is not a technical feasibility guarantee.") + "</section>")
    parts.append("<section><h2>Recommendations and next steps</h2>" + bullets(report.recommendations, "No implementation recommendation is supported.") + "</section>")
    parts.append("<section><h2>Corrections and revision history</h2><p>Corrections remain in the evidence register; contradicted claims retain their earlier wording and sources. "
                 "This report applies only to the displayed revision. Request a correction rather than editing source evidence.</p>")
    corrections = [item for item in report.evidence if item.kind == "correction"]
    parts.append("<p>" + " ".join(sources([item.id]) for item in corrections) + "</p>" if corrections else "<p>No explicit corrections are recorded in this revision.</p>")
    parts.append("</section><section><h2>Evidence register</h2><p>Source offsets are relative to the session. Screen observations are model-derived, not verified verbatim pixel transcripts. "
                 "Source references below are inert labels, not external links.</p>")
    for item in report.evidence:
        parts.append(f'<article class="source" id="{evidence_anchors[item.id]}"><h3>{e(item.id)}</h3>'
                     f'<p class="meta">{e(item.kind)} · {e(item.actor)} · {item.at_ms} ms · sequence {item.seq} · consent epoch {item.consent_epoch}</p>'
                     f'<p>{e(item.text)}</p>')
        if item.source_ref is not None:
            parts.append(f"<p>Source reference: <code>{e(item.source_ref)}</code></p>")
        parts.append("</article>")
    parts.append("</section>")
    if internal:
        opportunity = report.internal_opportunity
        parts.append('<section class="internal"><h2>Internal opportunity · Non-binding diligence draft</h2>'
                     f'<p><strong>{e(opportunity["status"])}</strong></p><p>{e(opportunity["rationale"])}</p>'
                     f'<p>{sources(opportunity["evidence_ids"])}</p>'
                     + bullets(opportunity["validation_questions"], "No additional diligence questions recorded.")
                     + f'<p>{e(opportunity["next_step"])}</p></section>')
    parts.append("<footer><p>This is a workflow discovery record, not an offer, approved scope, binding price or authority to act on client systems.</p></footer></body></html>")
    return "".join(parts)
