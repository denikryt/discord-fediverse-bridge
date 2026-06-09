"""Report checks for inbound ActivityPub rule ownership."""

from support.inbound_contract_manifest import INBOUND_CONTRACT_ENTRIES


def test_inbound_manifest_has_unique_rule_ids_and_nodes() -> None:
    ids = [entry.rule_id for entry in INBOUND_CONTRACT_ENTRIES]
    assert len(ids) == len(set(ids))
    assert all(entry.node_prefixes for entry in INBOUND_CONTRACT_ENTRIES)
