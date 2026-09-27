"""Regression slices from the independent core review; no model network."""
import pytest
from pydantic import ValidationError
from beep_agent.domain import Evidence, Snapshot, Report
from beep_agent.discovery import DiscoveryEngine, DiscoveryError, Extraction


def evidence(actor="agent", kind="transcript"):
    return Evidence(id="prior", actor=actor, kind=kind, text="Synthetic question", at_ms=0,
                    consent_epoch=0, seq=1)


def step():
    return dict(id="s", title="Approve", actor="Clerk", system="ERP",
                description="Synthetic unsupported step", evidence_ids=["prior"])


@pytest.mark.parametrize("actor,kind", [("agent", "transcript"), ("system", "system"), ("client", "system")])
@pytest.mark.parametrize("record", ["snapshot", "report", "delta"])
async def test_steps_require_business_evidence(actor, kind, record):
    event = evidence(actor, kind)
    if record == "snapshot":
        with pytest.raises(ValidationError, match="business.*evidence"):
            Snapshot(title="Synthetic", last_event_seq=1, evidence=[event], steps=[step()])
    elif record == "report":
        with pytest.raises(ValidationError, match="business.*evidence"):
            Report(session_id="s", title="Synthetic", revision=0, summary="Synthetic",
                   status="partial", recommendations=[], internal_opportunity={},
                   generated_at="2026-09-11T00:00:00Z", evidence=[event], steps=[step()])
    else:
        class Provider:
            async def extract(self, snapshot, event):
                return Extraction(steps=[step()])
        with pytest.raises(DiscoveryError):
            await DiscoveryEngine(Provider()).process(
                Snapshot(title="Synthetic", evidence=[event], last_event_seq=1),
                Evidence(id="new", actor="client", kind="transcript", text="Next", at_ms=1,
                         consent_epoch=0, seq=2), thread_id="synthetic-review")
