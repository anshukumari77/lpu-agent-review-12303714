"""Client reports from synthetic sources only."""
import importlib.util

from beep_agent.domain import Claim, Evidence, Snapshot, WorkflowEdge, WorkflowStep


def synthetic_snapshot(**changes):
    return Snapshot.model_validate({
        "title": "Synthetic quotation workflow", "pack_id": "quotation", "revision": 3,
        "last_event_seq": 2,
        "evidence": [Evidence(id="e1", kind="transcript", actor="client", seq=1, at_ms=1000, consent_epoch=0,
                              text="SYNTHETIC: Alex drafts and Rae approves the quote; ten quotes a week take 20 minutes each."),
                     Evidence(id="e2", kind="transcript", actor="client", seq=2, at_ms=2000, consent_epoch=0,
                              text="SYNTHETIC: Missing pricing goes back to Alex. The workflow readback is accurate.")],
        "claims": [Claim(id="c1", text="Ten quotes a week take 20 minutes each, as reported by Alex.",
                         status="reported", evidence_ids=["e1"])],
        "steps": [WorkflowStep(id="s1", title="Draft quotation", actor="Alex", system="Spreadsheet",
                               description="Draft the quote from enquiry inputs.", evidence_ids=["e1"]),
                  WorkflowStep(id="s2", title="Approve quotation", actor="Rae", system="Email",
                               description="Approve or return missing pricing.", evidence_ids=["e1", "e2"])],
        "edges": [WorkflowEdge(id="x1", source="s1", target="s2", kind="handoff", label="Approval"),
                  WorkflowEdge(id="x2", source="s2", target="s1", kind="rework", label="Missing pricing")],
        "coverage": ["workflow", "exceptions", "approvals", "baseline", "readback"],
        **changes,
    })


def test_report_assembly_is_source_linked_printable_and_separates_internal_draft():
    assert importlib.util.find_spec("beep_agent.reports"), "report module missing"
    from beep_agent.reports import build_report, render_report_html
    snapshot = synthetic_snapshot()
    report = build_report(snapshot, "synthetic-session")
    assert report.revision == 3 and report.status == "partial"
    assert report.recording.artifact_status == "unverified"
    assert report.recording.provenance == "missing_manifest"
    assert report.evidence == snapshot.evidence and report.steps == snapshot.steps
    assert report.internal_opportunity["non_binding"] is True
    html = render_report_html(report)
    for label in ("Workflow", "Evidence", "Unknowns", "Recommendations", "Baseline", "Approval", "rework"):
        assert label in html
    assert "@media print" in html
    assert 'href="#evidence-1"' in html and 'id="evidence-1"' in html
    assert "Internal opportunity" not in html
    assert "Internal opportunity" in render_report_html(report, internal=True)
    assert "synthetic-session" in html and "Revision 3" in html


def test_empty_snapshot_produces_honest_partial_not_canned_project():
    assert importlib.util.find_spec("beep_agent.reports"), "report module missing"
    from beep_agent.reports import build_report
    report = build_report(Snapshot(title="Synthetic empty"), "synthetic-empty")
    assert report.status == "partial"
    assert report.claims == [] and report.steps == [] and report.evidence == []
    assert any("evidence" in gap.lower() for gap in report.unknowns)
    assert report.internal_opportunity["status"] == "insufficient_evidence"
    assert report.internal_opportunity["candidate_claim_ids"] == []
    assert not any(term in " ".join(report.recommendations).lower() for term in ("automate", "savings", "$"))


def test_missing_baseline_or_only_inference_is_honestly_partial():
    from beep_agent.reports import build_report
    without_baseline = synthetic_snapshot(coverage=["workflow", "exceptions", "approvals", "readback"])
    report = build_report(without_baseline, "synthetic-no-baseline")
    assert report.status == "partial" and any("baseline" in gap.lower() for gap in report.unknowns)
    inferred = synthetic_snapshot(claims=[Claim(id="c1", text="The step probably takes 20 minutes.", status="inferred", evidence_ids=["e1"])])
    assert build_report(inferred, "synthetic-inference-only").status == "partial"


def test_no_project_is_valid_but_missing_recording_keeps_delivery_partial():
    from beep_agent.reports import build_report
    result = build_report(synthetic_snapshot(), "synthetic-no-project")
    assert result.status == "partial" and result.internal_opportunity["status"] == "no_project"
    assert result.recording.provenance == "missing_manifest"
    assert result.internal_opportunity["candidate_claim_ids"] == []
    assert build_report(synthetic_snapshot(), "synthetic-interrupted", partial=True).status == "partial"


def test_unresolved_contradiction_or_unacknowledged_correction_blocks_complete():
    from beep_agent.reports import build_report
    snapshot = synthetic_snapshot(claims=[Claim(id="c1", text="The process takes twenty minutes.", status="contradicted", evidence_ids=["e1"])])
    assert build_report(snapshot, "synthetic-contradiction").status == "partial"
    data = synthetic_snapshot().model_dump()
    data["evidence"].append(Evidence(id="correction", kind="correction", actor="client", text="SYNTHETIC: Actually the approver is Kim.", seq=3, at_ms=3000, consent_epoch=0))
    data["last_event_seq"] = 3
    assert build_report(Snapshot.model_validate(data), "synthetic-correction").status == "partial"


def test_hostile_refs_html_and_export_are_inert_and_preserve_portable_schema():
    import json
    from html.parser import HTMLParser
    from beep_agent.reports import build_report, render_report_html
    hostile = '<script>alert("synthetic")</script>&'
    event_id = 'e/" onclick="synthetic <&>'
    step_id = 'step"<script>'
    snapshot = Snapshot(title=hostile, last_event_seq=1,
                        evidence=[Evidence(id=event_id, kind="screen_observation", actor="observer", seq=1, at_ms=0, consent_epoch=0,
                                           text=hostile, source_ref="javascript:alert('synthetic')")],
                        claims=[Claim(id="claim", text=hostile, status="inferred", evidence_ids=[event_id])],
                        steps=[WorkflowStep(id=step_id, title=hostile, actor=hostile, system=hostile, description=hostile, evidence_ids=[event_id])])
    report = build_report(snapshot, 'session/"<&>')
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags, self.attrs = [], []
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.attrs.extend(attrs)
    html = render_report_html(report)
    parsed = Parser()
    parsed.feed(html)
    assert "script" not in parsed.tags
    assert not any(name.startswith("on") for name, value in parsed.attrs)
    hrefs = [value for name, value in parsed.attrs if name == "href"]
    assert hrefs and all(value.startswith("#") for value in hrefs)
    assert "&lt;script&gt;" in html
    assert 'id="evidence-1"' in html
    client = json.loads(report.model_dump_json(exclude={"internal_opportunity"}))
    assert client["schema_version"] == "1" and "internal_opportunity" not in client
    assert client["evidence"][0]["id"] == event_id and client["steps"][0]["id"] == step_id
    assert "checkpointer" not in client and "messages" not in client


def test_client_html_does_not_leak_internal_rationale_or_questions():
    from beep_agent.reports import build_report, render_report_html
    report = build_report(synthetic_snapshot(), "synthetic-internal")
    report.internal_opportunity["rationale"] = "SYNTHETIC INTERNAL ONLY SENTINEL"
    report.internal_opportunity["validation_questions"] = ["SYNTHETIC COMMERCIAL ONLY QUESTION"]
    assert "SYNTHETIC INTERNAL ONLY" not in render_report_html(report)
    assert "SYNTHETIC COMMERCIAL ONLY" not in render_report_html(report)
    assert "SYNTHETIC INTERNAL ONLY" in render_report_html(report, internal=True)


def test_explicit_unknown_claim_and_unresolved_contradiction_keep_report_partial():
    from beep_agent.reports import build_report
    snapshot = synthetic_snapshot()
    data = snapshot.model_dump()
    data["claims"].append(Claim(id="unknown", text="The fallback approver is unknown.", evidence_ids=[], status="unknown"))
    assert build_report(Snapshot.model_validate(data), "synthetic-explicit-unknown").status == "partial"
    data["claims"][-1] = Claim(id="contradiction", text="Conflicting process duration.", evidence_ids=["e1", "e2"], status="contradicted")
    result = build_report(Snapshot.model_validate(data), "synthetic-conflict")
    assert result.status == "partial" and any("contradict" in gap.lower() for gap in result.unknowns)


def test_maximum_valid_unknowns_are_preserved_in_partial_report():
    from beep_agent.reports import build_report
    unknowns = [f"SYNTHETIC unresolved item {n}: " + "x" * 3900 for n in range(128)]
    snapshot = Snapshot(title="Synthetic maximum gaps", unknowns=unknowns)
    result = build_report(snapshot, "synthetic-bounded-report")
    assert result.status == "partial" and result.unknowns[:128] == unknowns
    assert result.internal_opportunity["validation_questions"] == result.unknowns


def test_internal_candidate_requires_its_actual_supporting_sources():
    import pytest
    from pydantic import ValidationError
    from beep_agent.domain import Report
    from beep_agent.reports import build_report
    data = build_report(synthetic_snapshot(), "synthetic-opportunity-integrity").model_dump()
    data["internal_opportunity"].update(status="discovery_only", candidate_claim_ids=["c1"], evidence_ids=["e2"])
    with pytest.raises(ValidationError):
        Report.model_validate(data)
    data["internal_opportunity"]["evidence_ids"] = ["e1"]
    assert Report.model_validate(data).internal_opportunity["candidate_claim_ids"] == ["c1"]
