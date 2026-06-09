#!/usr/bin/env python3
"""Run outbound fanout scenario owners and emit a deterministic report."""
from __future__ import annotations
import json,sys
from collections import Counter
from pathlib import Path
from typing import Any
import pytest

class Collector:
 def __init__(self,prefixes:set[str]): self.prefixes=prefixes; self.status={}
 def _matches(self,nodeid:str)->bool: return any(nodeid.startswith(p) for p in self.prefixes)
 def pytest_collection_modifyitems(self,items):
  for item in items:
   if self._matches(item.nodeid): self.status[item.nodeid]='collected'
 def pytest_runtest_logreport(self,report):
  if report.nodeid not in self.status:return
  if report.failed:self.status[report.nodeid]='failed'
  elif report.skipped:self.status[report.nodeid]='xfailed' if hasattr(report,'wasxfail') else 'skipped'
  elif report.when=='call' and report.passed:self.status[report.nodeid]='passed'

def build_report(entries:tuple[Any,...],status:dict[str,str])->dict[str,Any]:
 rows=[]; missing=[]
 for entry in entries:
  nodes=sorted(n for n in status if any(n.startswith(p) for p in entry.node_prefixes)); represented=bool(nodes)
  if not represented:missing.append(entry.rule_id)
  rows.append({'rule_id':entry.rule_id,'family':entry.family,'classification':entry.classification,'represented':represented,'nodes':[{'nodeid':n,'status':status[n]} for n in nodes]})
 totals=Counter(status.values())
 return {'domain':'outbound_fanout','summary':{'required_rules':len(entries),'represented_rules':len(entries)-len(missing),'missing_rules':len(missing),'statuses':dict(sorted(totals.items()))},'missing_rule_ids':missing,'rules':rows}

def main()->int:
 root=Path(__file__).resolve().parents[1]
 for p in (root,root/'tests'):
  if str(p) not in sys.path:sys.path.insert(0,str(p))
 from support.fanout_contract_manifest import FANOUT_CONTRACT_ENTRIES
 prefixes={p for e in FANOUT_CONTRACT_ENTRIES for p in e.node_prefixes}; c=Collector(prefixes)
 files=sorted({p.split('::',1)[0] for p in prefixes}); code=pytest.main(['-q',*files],plugins=[c]); report=build_report(FANOUT_CONTRACT_ENTRIES,c.status)
 out=root/'.artifacts/test-assurance/outbound-fanout/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); return int(code)
if __name__=='__main__':raise SystemExit(main())
