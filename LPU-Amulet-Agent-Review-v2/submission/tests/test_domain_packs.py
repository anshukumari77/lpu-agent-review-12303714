"""Synthetic interview-pack contract tests."""
import importlib.util

import pytest


def test_three_versioned_packs_are_configuration_not_shared_mutable_state():
    assert importlib.util.find_spec("beep_agent.packs"), "packs not implemented"
    from beep_agent.packs import get_pack, list_packs
    packs = list_packs()
    assert {p["id"] for p in packs} == {"general", "quotation", "recurring_reporting"}
    for pack in packs:
        assert pack["version"] == "1"
        assert {"exceptions", "approvals", "baseline", "readback"} <= set(pack["probes"])
        assert pack["required_coverage"]
        assert pack["probes"]["exceptions"] != pack["probes"]["baseline"]
    packs[0]["probes"]["baseline"] = "MUTATED"
    assert get_pack(packs[0]["id"])["probes"]["baseline"] != "MUTATED"
    with pytest.raises(ValueError, match="Unknown"):
        get_pack("not-a-pack")
    with pytest.raises(ValueError):
        get_pack(" general")
