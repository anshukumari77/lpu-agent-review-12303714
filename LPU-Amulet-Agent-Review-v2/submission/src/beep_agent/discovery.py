"""Explicit LangGraph discovery controller; no external actions or hidden retry loop."""
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from .domain import (
    Claim, DTO, Evidence, Probe, Refs, Snapshot, Text, WorkflowEdge, WorkflowStep, validate_refs,
)
from .packs import get_pack


class CoverageItem(DTO):
    area: Literal["workflow", "exceptions", "approvals", "baseline", "readback"]
    evidence_ids: Refs


class UnknownResolution(DTO):
    text: Text
    evidence_ids: Refs


class Extraction(DTO):
    """Provider delta, not a replacement snapshot. References use supplied IDs only."""
    claims: list[Claim] = Field(default_factory=list, max_length=32)
    steps: list[WorkflowStep] = Field(default_factory=list, max_length=24)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=32)
    coverage: list[CoverageItem] = Field(default_factory=list, max_length=5)
    unknowns: list[Text] = Field(default_factory=list, max_length=16)
    superseded_claim_ids: Refs = Field(default_factory=list)
    resolved_unknowns: list[UnknownResolution] = Field(default_factory=list, max_length=16)


class DiscoveryError(ValueError):
    """Safe public failure; never a fabricated partial snapshot."""


class _State(TypedDict):
    snapshot: dict
    event: dict
    delta: dict
    result: dict


class DiscoveryEngine:
    def __init__(self, provider, checkpointer=None):
        self.provider = provider
        graph = StateGraph(_State)
        graph.add_node("ingest", self._ingest)
        graph.add_node("extract", self._extract)
        graph.add_node("reconcile", self._reconcile)
        graph.add_node("choose_probe", self._choose_probe)
        graph.add_node("validate", self._validate)
        graph.add_edge(START, "ingest")
        graph.add_edge("ingest", "extract")
        graph.add_edge("extract", "reconcile")
        graph.add_edge("reconcile", "choose_probe")
        graph.add_edge("choose_probe", "validate")
        graph.add_edge("validate", END)
        self.graph = graph.compile(checkpointer=checkpointer, name="beep-discovery-v1")

    async def process(self, snapshot: Snapshot, event: Evidence, *, thread_id: str) -> Snapshot:
        snapshot = Snapshot.model_validate(snapshot)
        event = Evidence.model_validate(event)
        if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 400:
            raise DiscoveryError("A bounded tenant/session thread_id is required")
        self._check_event(snapshot, event)
        try:
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 12}
            if self.graph.checkpointer is not None:
                checkpoint = await self.graph.aget_state(config)
                previous = checkpoint.values
                if not checkpoint.next and previous.get("result"):
                    completed = Snapshot.model_validate(previous["result"])
                    if (previous.get("snapshot") == snapshot.model_dump(mode="json")
                            and previous.get("event") == event.model_dump(mode="json")):
                        return completed
                    if completed.revision > snapshot.revision:
                        raise DiscoveryError("Caller snapshot is older than the completed checkpoint")
                    if completed.revision == snapshot.revision and (
                        completed.model_dump(exclude={"consent_epoch", "probe"})
                        != snapshot.model_dump(exclude={"consent_epoch", "probe"})
                    ):
                        raise DiscoveryError("Caller snapshot conflicts with the completed checkpoint")
            output = await self.graph.ainvoke(
                {"snapshot": snapshot.model_dump(mode="json"), "event": event.model_dump(mode="json"),
                 "delta": {}, "result": {}},
                config=config,
            )
            return Snapshot.model_validate(output["result"])
        except Exception:
            raise DiscoveryError("Discovery failed; no snapshot published") from None

    @staticmethod
    def _check_event(snapshot: Snapshot, event: Evidence) -> None:
        if event.consent_epoch != snapshot.consent_epoch:
            raise DiscoveryError("Event consent epoch is not current")
        if event.seq <= snapshot.last_event_seq or any(e.id == event.id for e in snapshot.evidence):
            raise DiscoveryError("Event is stale or already applied")

    def _ingest(self, state: _State) -> dict:
        self._check_event(Snapshot.model_validate(state["snapshot"]),
                          Evidence.model_validate(state["event"]))
        return {}

    async def _extract(self, state: _State) -> dict:
        snapshot = Snapshot.model_validate(state["snapshot"])
        event = Evidence.model_validate(state["event"])
        try:
            delta = Extraction.model_validate(await self.provider.extract(snapshot, event))
        except Exception:
            # Sanitise BEFORE LangGraph persists a task error into the checkpointer.
            raise DiscoveryError("Discovery failed during structured extraction") from None
        return {"delta": delta.model_dump(mode="json")}

    def _reconcile(self, state: _State) -> dict:
        snapshot = Snapshot.model_validate(state["snapshot"])
        event = Evidence.model_validate(state["event"])
        delta = Extraction.model_validate(state["delta"])
        data = snapshot.model_dump(mode="json")
        data.update(revision=snapshot.revision + 1, last_event_seq=event.seq, probe=None)
        data["evidence"].append(event.model_dump(mode="json"))
        business_ids = {item["id"] for item in data["evidence"]
                        if item["actor"] not in {"agent", "system"} and item["kind"] != "system"}
        for item in delta.coverage:
            validate_refs(item.evidence_ids, business_ids, required=True)
        resolved = set()
        for item in delta.resolved_unknowns:
            validate_refs(item.evidence_ids, business_ids, required=True)
            if item.text not in snapshot.unknowns or event.id not in item.evidence_ids:
                raise ValueError("Unknown resolution must address an existing gap using this event")
            if not any(event.id in claim.evidence_ids for claim in delta.claims):
                raise ValueError("Unknown resolution requires a new sourced finding")
            resolved.add(item.text)
        if delta.superseded_claim_ids:
            if event.kind != "correction":
                raise ValueError("Superseding claims requires a correction event")
            validate_refs(delta.superseded_claim_ids, {c.id for c in snapshot.claims}, required=True)
            if not any(event.id in c.evidence_ids for c in delta.claims):
                raise ValueError("A correction must provide a sourced replacement claim")
        for claim in data["claims"]:
            if claim["id"] in delta.superseded_claim_ids:
                claim["status"] = "contradicted"
                claim["evidence_ids"] = list(dict.fromkeys([*claim["evidence_ids"], event.id]))
        for field in ("claims", "steps", "edges"):
            existing = {item["id"]: item for item in data[field]}
            for record in getattr(delta, field):
                item = record.model_dump(mode="json")
                prior = existing.get(item["id"])
                if prior and prior != item:
                    if field == "claims" or event.kind != "correction":
                        raise ValueError("Existing facts cannot be overwritten; append a correction")
                    if field == "steps":
                        if event.id not in item["evidence_ids"]:
                            raise ValueError("Changed workflow step requires correction provenance")
                        item["evidence_ids"] = list(dict.fromkeys([
                            *prior["evidence_ids"], *item["evidence_ids"],
                        ]))
                existing[item["id"]] = item
            data[field] = list(existing.values())
        prior_coverage = [] if event.kind == "correction" else snapshot.coverage
        data["coverage"] = list(dict.fromkeys([*prior_coverage, *[c.area for c in delta.coverage]]))
        data["unknowns"] = list(dict.fromkeys([
            *[unknown for unknown in snapshot.unknowns if unknown not in resolved], *delta.unknowns,
        ]))
        return {"result": Snapshot.model_validate(data).model_dump(mode="json")}

    def _choose_probe(self, state: _State) -> dict:
        snapshot = Snapshot.model_validate(state["result"])
        event = Evidence.model_validate(state["event"])
        pack = get_pack(snapshot.pack_id)
        missing = [area for area in pack["required_coverage"] if area not in snapshot.coverage]
        area = "readback" if event.kind == "correction" else missing[0] if missing else "readback"
        prefix = f"For ‘{snapshot.steps[-1].title[:100]}’: " if snapshot.steps else ""
        probe = Probe(id=f"probe-{snapshot.revision}", text=prefix + pack["probes"][area],
                      reason=("Confirm the correction before continuing discovery." if event.kind == "correction"
                              else f"Unverified {area}; use the current example, not an assumed happy path."),
                      evidence_ids=[event.id], based_on_revision=snapshot.revision,
                      consent_epoch=snapshot.consent_epoch)
        data = snapshot.model_dump(mode="json")
        data["probe"] = probe.model_dump(mode="json")
        return {"result": Snapshot.model_validate(data).model_dump(mode="json")}

    def _validate(self, state: _State) -> dict:
        return {"result": Snapshot.model_validate(state["result"]).model_dump(mode="json")}
