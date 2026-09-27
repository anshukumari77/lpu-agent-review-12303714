"""Versioned interview method configuration; paid and sponsored use the same packs."""
from copy import deepcopy

_REQUIRED = ["workflow", "exceptions", "approvals", "baseline", "readback"]
_COMMON = {
    "workflow": "Walk me through a recent real example, from its trigger to the final outcome.",
    "exceptions": "Show me a case that did not follow the normal path. What failed, and who fixed it?",
    "approvals": "Who can approve this, and what happens when they are unavailable or disagree?",
    "baseline": "How often does this happen, how long does it take, and what records support those estimates?",
    "readback": "Is this workflow accurate, including exceptions and approvals? What have I missed or misunderstood?",
}
_PACKS = {
    "general": {
        "id": "general", "version": "1", "title": "Workflow review",
        "description": "Evidence-led review of one current business workflow.",
        "vocabulary": ["trigger", "handoff", "owner", "system", "outcome"],
        "probes": _COMMON,
    },
    "quotation": {
        "id": "quotation", "version": "1", "title": "Quotation workflow",
        "description": "Follow an enquiry through pricing, approval, delivery and revision.",
        "vocabulary": ["enquiry", "quotation", "pricing", "margin", "approval", "revision"],
        "probes": {
            **_COMMON,
            "workflow": "Show a recent enquiry and how it became a sent quotation. Where did the inputs come from?",
            "exceptions": "What happens when pricing is missing, scope changes, or a quote needs rework?",
            "approvals": "Who approves unusual discounts or terms, and how is that approval recorded?",
            "baseline": "How many quotes are produced, how much hands-on time and waiting do they take, and how is rework measured?",
        },
    },
    "recurring_reporting": {
        "id": "recurring_reporting", "version": "1", "title": "Recurring reporting",
        "description": "Follow one reporting cycle from source data through checks and distribution.",
        "vocabulary": ["source data", "reconciliation", "reporting cycle", "sign-off", "distribution"],
        "probes": {
            **_COMMON,
            "workflow": "Show the last reporting cycle, from source data to the report people received.",
            "exceptions": "What happens when a source arrives late, totals disagree, or a report must be reissued?",
            "approvals": "Who checks and signs off the report, and who handles disputed figures?",
            "baseline": "How frequent is the cycle, how much preparation and checking time does it take, and what evidence supports that?",
        },
    },
}
for _pack in _PACKS.values():
    _pack["required_coverage"] = list(_REQUIRED)


def get_pack(pack_id: str) -> dict:
    if pack_id not in _PACKS:
        raise ValueError("Unknown interview pack")
    return deepcopy(_PACKS[pack_id])


def list_packs() -> list[dict]:
    return [get_pack(pack_id) for pack_id in _PACKS]
