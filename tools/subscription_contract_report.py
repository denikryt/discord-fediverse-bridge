#!/usr/bin/env python3
"""Emit passive subscription lifecycle coverage."""
from __future__ import annotations
import json,sys
from pathlib import Path
import pytest
try: from tools.contract_report_support import PassiveCaseCollector,status_totals
except ModuleNotFoundError: from contract_report_support import PassiveCaseCollector,status_totals
def main()->int:
 root=Path(__file__).resolve().parents[1]
 for p in (root,root/'tests'):
  if str(p) not in sys.path: sys.path.insert(0,str(p))
 from support.subscription_contracts import SubscriptionCase,REQUIRED_SUBSCRIPTION_RULES
 c=PassiveCaseCollector(accepts=lambda x:isinstance(x,SubscriptionCase)); code=pytest.main(['-q','tests/operations/test_subscription_contract_cases.py'],plugins=[c]); rs=c.results(); ids={r.case.id for r in rs}; missing=[x.id for x in REQUIRED_SUBSCRIPTION_RULES if not ids.intersection(x.represented_by)]
 report={'domain':'subscription_lifecycle','summary':{'required_rules':len(REQUIRED_SUBSCRIPTION_RULES),'represented_rules':len(REQUIRED_SUBSCRIPTION_RULES)-len(missing),'missing_rules':len(missing),'statuses':status_totals(rs)},'missing_rule_ids':missing,'cases':[{'id':r.case.id,'nodeid':r.nodeid,'status':r.status,'action':r.case.action,'channel_state':r.case.channel_state,'follow_state':r.case.follow_state} for r in rs]}
 out=root/'.artifacts/test-assurance/subscription/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); return int(code)
if __name__=='__main__': raise SystemExit(main())
