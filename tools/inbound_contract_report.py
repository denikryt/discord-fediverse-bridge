#!/usr/bin/env python3
"""Emit passive inbound ActivityPub contract coverage."""
from __future__ import annotations
import json,sys
from pathlib import Path
import pytest
try:
    from tools.assurance_reporting import OwnerPrefixCollector as Collector, build_owner_report
except ModuleNotFoundError:
    from assurance_reporting import OwnerPrefixCollector as Collector, build_owner_report

def main()->int:
 root=Path(__file__).resolve().parents[1]
 for p in (root,root/'tests'):
  if str(p) not in sys.path:sys.path.insert(0,str(p))
 from support.inbound_contract_manifest import INBOUND_CONTRACT_ENTRIES
 prefixes={p for e in INBOUND_CONTRACT_ENTRIES for p in e.node_prefixes}; c=Collector(prefixes); files=sorted({p.split('::',1)[0] for p in prefixes}); code=pytest.main(['-q',*files],plugins=[c]); report=build_owner_report(domain='inbound_activitypub',entries=INBOUND_CONTRACT_ENTRIES,status=c.status); out=root/'.artifacts/test-assurance/inbound-activitypub/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); return int(code)
if __name__=='__main__':raise SystemExit(main())
