"""Synthetic contract fixtures only; no client data or inference."""
import importlib.util

import pytest
from pydantic import ValidationError

from beep_agent.domain import Claim, Evidence, Report, Snapshot


def evidence(**changes):
    return Evidence.model_validate({"id": "e1", "kind": "transcript", "text": "SYNTHETIC source",
                                   "actor": "client", "at_ms": 1, "consent_epoch": 0, "seq": 1,
                                   **changes})


def sourced_snapshot(**changes):
    return Snapshot.model_validate({"title": "Synthetic", "last_event_seq": 1,
                                    "evidence": [evidence().model_dump()], **changes})


@pytest.mark.parametrize("changes", [
    {"mystery": True}, {"revision": -1}, {"schema_version": "2"}, {"pack_id": "unknown"},
    {"claims": [{"id": "c", "text": "Unsupported", "evidence_ids": ["missing"], "status": "reported"}]},
    {"claims": [{"id": "c", "text": "Unsupported", "evidence_ids": [], "status": "reported"}]},
    {"steps": [{"id": "s", "title": "S", "actor": "A", "system": "X", "description": "D", "evidence_ids": ["missing"]}]},
    {"edges": [{"id": "x", "source": "missing", "target": "missing", "kind": "next", "label": ""}]},
    {"evidence": [evidence(), evidence()]},
    {"evidence": [evidence(consent_epoch=2)]},
    {"last_event_seq": 0},
    {"probe": {"id": "p", "text": "Q", "reason": "R", "evidence_ids": ["e1"], "based_on_revision": 2, "consent_epoch": 0}},
    {"probe": {"id": "p", "text": "Q", "reason": "R", "evidence_ids": ["missing"], "based_on_revision": 0, "consent_epoch": 0}},
    {"coverage": ["baseline"] * 1000},
])
def test_snapshot_rejects_invalid_or_unbounded_graph(changes):
    with pytest.raises(ValidationError):
        sourced_snapshot(**changes)


@pytest.mark.parametrize("changes", [
    {"id": ""}, {"id": " "}, {"text": ""}, {"at_ms": True}, {"at_ms": -1},
    {"seq": "1"}, {"text": "x" * 12001}, {"source_ref": "x" * 1001},
    {"unexpected": "field"},
])
def test_evidence_rejects_malformed_fields(changes):
    with pytest.raises(ValidationError):
        evidence(**changes)


def test_inferred_screen_text_cannot_be_promoted_to_observed():
    with pytest.raises(ValidationError, match="screen"):
        sourced_snapshot(evidence=[evidence(kind="screen_observation", actor="observer")],
                         claims=[Claim(id="c1", text="Text exact", evidence_ids=["e1"], status="observed")])


def test_revalidation_catches_mutated_model_instances():
    snapshot = sourced_snapshot()
    snapshot.evidence[0].at_ms = -1
    with pytest.raises(ValidationError):
        Snapshot.model_validate(snapshot)


def test_opaque_identifiers_are_preserved_not_normalized():
    assert evidence(id=' <opaque/&"id> ').id == ' <opaque/&"id> '


@pytest.mark.parametrize("actor", ["agent", "system"])
def test_business_claim_cannot_be_supported_only_by_agent_or_system(actor):
    with pytest.raises(ValidationError):
        sourced_snapshot(evidence=[evidence(actor=actor)], claims=[Claim(
            id="c1", text="Reported business claim", evidence_ids=["e1"], status="reported")])


def test_report_rejects_dangling_refs_and_binding_price():
    data = dict(session_id="synthetic", revision=1, title="Synthetic", summary="Incomplete",
                claims=[], steps=[], edges=[], unknowns=[], recommendations=[], evidence=[],
                status="partial", internal_opportunity={"binding_price": 9000},
                generated_at="2026-09-11T00:00:00+00:00")
    with pytest.raises(ValidationError):
        Report.model_validate(data)
    data["internal_opportunity"] = {}
    data["claims"] = [dict(id="c", text="Missing", evidence_ids=["missing"], status="reported")]
    with pytest.raises(ValidationError):
        Report.model_validate(data)


def test_public_snapshot_roundtrip_retains_source_graph_and_probe():
    assert importlib.util.find_spec("beep_agent.domain"), "domain contract is not implemented"
    from beep_agent.domain import Snapshot

    payload = {
        "title": "Synthetic quotation review",
        "revision": 1,
        "consent_epoch": 2,
        "last_event_seq": 1,
        "evidence": [{"id": "e1", "kind": "transcript", "text": "SYNTHETIC: Sam drafts a quote.",
                      "actor": "client", "at_ms": 2000, "consent_epoch": 2, "seq": 1}],
        "claims": [{"id": "c1", "text": "Sam drafts a quote.", "evidence_ids": ["e1"], "status": "reported"}],
        "steps": [{"id": "s1", "title": "Draft", "actor": "Sam", "system": "Spreadsheet",
                   "description": "Draft a quotation", "evidence_ids": ["e1"]}],
        "probe": {"id": "p1", "text": "Who approves exceptions?", "reason": "Approval gap",
                  "evidence_ids": ["e1"], "based_on_revision": 1, "consent_epoch": 2},
    }
    snapshot = Snapshot.model_validate(payload)
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert snapshot.schema_version == "1"
    assert snapshot.pack_id == "general"
    assert snapshot.evidence[0].source_ref is None
    assert snapshot.claims[0].evidence_ids == ["e1"]
