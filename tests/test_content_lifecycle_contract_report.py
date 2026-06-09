"""Manifest checks for content lifecycle contracts."""
from support.content_lifecycle_manifest import CONTENT_CONTRACT_ENTRIES

def test_content_manifest_has_unique_rule_ids()->None:
 ids=[entry.rule_id for entry in CONTENT_CONTRACT_ENTRIES]
 assert len(ids)==len(set(ids))
 assert all(entry.node_prefixes for entry in CONTENT_CONTRACT_ENTRIES)
