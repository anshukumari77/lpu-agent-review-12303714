"""Synthetic discovery scenarios; no network/model fixtures outside tests."""
import importlib.util

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from beep_agent.domain import Evidence, Snapshot


class SyntheticProvider:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def extract(self, snapshot, event):
        self.calls.append((snapshot, event))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def source(n=1, **changes):
    return Evidence.model_validate({"id": f"e{n}", "kind": "transcript", "actor": "client", "at_ms": n * 1000,
                                    "consent_epoch": 0, "seq": n, "text": "SYNTHETIC: Alex drafts a quotation.",
                                    **changes})


def extraction(**changes):
    return {"claims": [{"id": "c1", "text": "Alex drafts a quotation.", "status": "reported", "evidence_ids": ["e1"]}],
            "steps": [{"id": "s1", "title": "Draft quotation", "actor": "Alex", "system": "Spreadsheet",
                       "description": "Draft from an enquiry", "evidence_ids": ["e1"]}],
            "coverage": [{"area": "workflow", "evidence_ids": ["e1"]}], **changes}


@pytest.mark.asyncio
async def test_real_compiled_graph_persists_sourced_delta_and_targeted_exception_probe():
    assert importlib.util.find_spec("beep_agent.discovery"), "discovery engine missing"
    from beep_agent.discovery import DiscoveryEngine
    saver = InMemorySaver()
    provider = SyntheticProvider(extraction())
    engine = DiscoveryEngine(provider, checkpointer=saver)
    assert isinstance(engine.graph, CompiledStateGraph)
    assert engine.graph.checkpointer is saver
    before = Snapshot(title="Synthetic quotation", pack_id="quotation")
    after = await engine.process(before, source(), thread_id="synthetic-tenant/session-a")
    assert after.revision == 1 and after.last_event_seq == 1
    assert after.evidence == [source()]
    assert after.claims[0].evidence_ids == ["e1"]
    assert after.probe.based_on_revision == after.revision
    assert after.probe.consent_epoch == after.consent_epoch
    assert "exception" in after.probe.reason.lower()
    assert "Draft quotation" in after.probe.text
    assert not before.evidence
    checkpoint = await engine.graph.aget_state({"configurable": {"thread_id": "synthetic-tenant/session-a"}})
    assert checkpoint.values["result"]["revision"] == 1


@pytest.mark.asyncio
async def test_subsequent_empty_delta_never_erases_old_facts_or_sources():
    from beep_agent.discovery import DiscoveryEngine
    engine = DiscoveryEngine(SyntheticProvider(extraction(), {}), InMemorySaver())
    first = await engine.process(Snapshot(title="Synthetic"), source(), thread_id="synthetic-preserve")
    second = await engine.process(first, source(2, text="SYNTHETIC: Still demonstrating."), thread_id="synthetic-preserve")
    assert second.evidence[:1] == first.evidence
    assert second.claims == first.claims and second.steps == first.steps
    assert second.revision == 2 and second.last_event_seq == 2


@pytest.mark.asyncio
async def test_correction_keeps_history_and_revises_graph_with_correction_provenance():
    from beep_agent.discovery import DiscoveryEngine
    corrected = extraction(
        claims=[{"id": "c2", "text": "Rae drafts the quotation, not Alex.", "status": "reported", "evidence_ids": ["e2"]}],
        steps=[{"id": "s1", "title": "Draft quotation", "actor": "Rae", "system": "Spreadsheet",
                "description": "Rae drafts from the enquiry", "evidence_ids": ["e2"]}],
        superseded_claim_ids=["c1"], coverage=[],
    )
    engine = DiscoveryEngine(SyntheticProvider(extraction(), corrected), InMemorySaver())
    first = await engine.process(Snapshot(title="Synthetic"), source(), thread_id="synthetic-correction")
    second = await engine.process(first, source(2, kind="correction", text="SYNTHETIC correction: It is Rae, not Alex."), thread_id="synthetic-correction")
    assert second.evidence[0] == first.evidence[0]
    assert second.evidence[1].kind == "correction"
    assert second.claims[0].text == first.claims[0].text
    assert second.claims[0].status == "contradicted"
    assert second.claims[0].evidence_ids == ["e1", "e2"]
    assert second.claims[1].id == "c2"
    assert second.steps[0].actor == "Rae" and second.steps[0].evidence_ids == ["e1", "e2"]
    assert first.steps[0].actor == "Alex" and first.claims[0].status == "reported"
    assert "readback" not in second.coverage
    assert "correct" in second.probe.reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_delta", [
    {"claims": [{"id": "fake", "text": "Unsupported", "status": "reported", "evidence_ids": ["invented"]}]},
    {"coverage": [{"area": "baseline", "evidence_ids": ["invented"]}]},
    {"coverage": [{"area": "exceptions", "evidence_ids": []}]},
    {"unexpected": "ignored"},
    {"superseded_claim_ids": ["absent"]},
    {"edges": [{"id": "edge", "source": "missing", "target": "missing", "kind": "next", "label": ""}]},
])
async def test_model_output_is_explicitly_validated_before_publication(bad_delta):
    from beep_agent.discovery import DiscoveryEngine
    before = Snapshot(title="Synthetic")
    engine = DiscoveryEngine(SyntheticProvider(bad_delta))
    with pytest.raises(ValueError):
        await engine.process(before, source(), thread_id="synthetic-bad-output")
    assert not before.evidence and before.revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("event,thread", [(source(seq=0), "t"), (source(consent_epoch=1), "t"),
                                         (source(), ""), (source(), " " )])
async def test_stale_events_or_missing_thread_rejected_before_provider(event, thread):
    from beep_agent.discovery import DiscoveryEngine
    provider = SyntheticProvider({})
    with pytest.raises(ValueError):
        await DiscoveryEngine(provider).process(Snapshot(title="Synthetic"), event, thread_id=thread)
    assert not provider.calls


@pytest.mark.asyncio
async def test_hidden_provider_exception_is_not_partial_success_or_secret_leak():
    from beep_agent.discovery import DiscoveryEngine
    provider = SyntheticProvider(RuntimeError("secret=synthetic-sensitive-value"))
    with pytest.raises(ValueError, match="Discovery failed") as error:
        await DiscoveryEngine(provider).process(Snapshot(title="Synthetic"), source(), thread_id="synthetic-error")
    assert "synthetic-sensitive-value" not in str(error.value)


@pytest.mark.asyncio
async def test_client_transcript_cannot_silently_overwrite_prior_claim():
    from beep_agent.discovery import DiscoveryEngine
    engine = DiscoveryEngine(SyntheticProvider(extraction(), extraction(claims=[{
        "id": "c1", "text": "Different assertion", "evidence_ids": ["e2"], "status": "reported"}], steps=[], coverage=[])))
    first = await engine.process(Snapshot(title="Synthetic"), source(), thread_id="synthetic-immutability")
    with pytest.raises(ValueError):
        await engine.process(first, source(2), thread_id="synthetic-immutability")
    assert first.claims[0].text == "Alex drafts a quotation."


@pytest.mark.asyncio
async def test_completed_checkpoint_recovers_exact_replay_without_second_inference():
    from beep_agent.discovery import DiscoveryEngine
    saver = InMemorySaver()
    provider = SyntheticProvider(extraction())
    before = Snapshot(title="Synthetic replay")
    first = await DiscoveryEngine(provider, saver).process(before, source(), thread_id="synthetic-replay")
    restarted = DiscoveryEngine(provider, saver)
    recovered = await restarted.process(before, source(), thread_id="synthetic-replay")
    assert recovered == first and len(provider.calls) == 1
    with pytest.raises(ValueError):
        await restarted.process(before, source(2), thread_id="synthetic-replay")
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_unknown_resolution_requires_new_source_and_preserves_prior_evidence():
    from beep_agent.discovery import DiscoveryEngine
    provider = SyntheticProvider(extraction(unknowns=["Who approves unusual pricing?"]), {
        "claims": [{"id": "c2", "text": "Rae approves unusual pricing.", "status": "reported", "evidence_ids": ["e2"]}],
        "resolved_unknowns": [{"text": "Who approves unusual pricing?", "evidence_ids": ["e2"]}],
        "coverage": [{"area": "approvals", "evidence_ids": ["e2"]}],
    })
    engine = DiscoveryEngine(provider)
    before = await engine.process(Snapshot(title="Synthetic resolution"), source(), thread_id="synthetic-resolution")
    result = await engine.process(before, source(2, text="SYNTHETIC: Rae approves unusual pricing."), thread_id="synthetic-resolution")
    assert result.unknowns == [] and before.unknowns == ["Who approves unusual pricing?"]
    assert result.evidence[0] == before.evidence[0] and len(result.claims) == 2


@pytest.mark.asyncio
async def test_duplicate_delta_ids_are_rejected_not_last_write_wins():
    from beep_agent.discovery import DiscoveryEngine
    duplicate = extraction()
    duplicate["claims"].append({**duplicate["claims"][0], "text": "SYNTHETIC conflicting duplicate"})
    with pytest.raises(ValueError):
        await DiscoveryEngine(SyntheticProvider(duplicate)).process(Snapshot(title="Synthetic"), source(), thread_id="synthetic-duplicate")


@pytest.mark.asyncio
async def test_system_or_agent_statements_cannot_close_business_coverage():
    from beep_agent.discovery import DiscoveryEngine
    with pytest.raises(ValueError):
        await DiscoveryEngine(SyntheticProvider({"coverage": [{"area": "baseline", "evidence_ids": ["e1"]}]})).process(
            Snapshot(title="Synthetic"), source(actor="agent", text="SYNTHETIC: What is the baseline?"), thread_id="synthetic-question-not-proof")
