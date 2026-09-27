import pytest
from beep_agent.domain import Evidence, Snapshot
from beep_agent.discovery import DiscoveryEngine, Extraction
from beep_agent.providers import compact_context, _encode, MAX_CONTEXT_CHARS


async def test_visible_long_unknown_can_be_resolved_by_exact_protocol():
    unknown = "Synthetic unknown: " + "long baseline question " * 18
    snapshot = Snapshot(title="Synthetic", unknowns=[unknown])
    class VisibleProvider:
        async def extract(self, snapshot, event):
            visible = compact_context(snapshot, event)["untrusted_data"]["snapshot"]["unknowns"]
            return Extraction(claims=[dict(id="c", text="Answered", status="reported", evidence_ids=[event.id])],
                              resolved_unknowns=[dict(text=visible[0], evidence_ids=[event.id])])
    result = await DiscoveryEngine(VisibleProvider()).process(snapshot, Evidence(
        id="e", kind="transcript", actor="client", text="Answer", at_ms=0, seq=1, consent_epoch=0),
        thread_id="synthetic-unknown")
    assert result.unknowns == []
    assert snapshot.unknowns == [unknown]


@pytest.mark.parametrize("text", ["x" * 4000, "界" * 4000], ids=["ascii", "unicode"])
def test_unknowns_are_complete_or_explicitly_omitted_within_wire_bound(text):
    snapshot = Snapshot(title="Synthetic", unknowns=[str(n) + text[:3995] for n in range(12)])
    context = compact_context(snapshot)
    visible = context["untrusted_data"]["snapshot"]["unknowns"]
    assert visible and all(item in snapshot.unknowns for item in visible)
    assert context["omitted"]["unknowns"] == len(snapshot.unknowns) - len(visible)
    assert len(_encode(context)) <= MAX_CONTEXT_CHARS
