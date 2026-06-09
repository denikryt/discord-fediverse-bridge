#!/usr/bin/env python3
"""Run outbound fanout scenario owners and emit a deterministic report."""
from __future__ import annotations
import json,sys
from collections import Counter
from pathlib import Path
from typing import Any
import pytest

try:
 from tools.assurance_reporting import OwnerPrefixCollector, build_owner_report
except ModuleNotFoundError:
 from assurance_reporting import OwnerPrefixCollector, build_owner_report

Collector = OwnerPrefixCollector

def build_report(entries:tuple[Any,...],status:dict[str,str])->dict[str,Any]:
 return build_owner_report(domain='outbound_fanout',entries=entries,status=status)


def main()->int:
 root=Path(__file__).resolve().parents[1]
 for p in (root,root/'tests'):
  if str(p) not in sys.path:sys.path.insert(0,str(p))
 from support.fanout_contract_manifest import FANOUT_CONTRACT_ENTRIES
 prefixes={p for e in FANOUT_CONTRACT_ENTRIES for p in e.node_prefixes}; c=Collector(prefixes)
 files=sorted({p.split('::',1)[0] for p in prefixes}); code=pytest.main(['-q',*files],plugins=[c]); report=build_report(FANOUT_CONTRACT_ENTRIES,c.status)
 out=root/'.artifacts/test-assurance/outbound-fanout/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); return int(code)
if __name__=='__main__':raise SystemExit(main())
