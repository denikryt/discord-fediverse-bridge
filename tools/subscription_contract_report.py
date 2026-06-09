#!/usr/bin/env python3
"""Emit passive subscription lifecycle coverage."""
from __future__ import annotations
import json,sys
from pathlib import Path
import pytest
try: from tools.assurance_reporting import PassiveCaseCollector,build_case_report
except ModuleNotFoundError: from assurance_reporting import PassiveCaseCollector,build_case_report
def main()->int:
 root=Path(__file__).resolve().parents[1]
 for p in (root,root/'tests'):
  if str(p) not in sys.path: sys.path.insert(0,str(p))
 from support.subscription_contracts import SubscriptionCase,REQUIRED_SUBSCRIPTION_RULES
 c=PassiveCaseCollector(accepts=lambda x:isinstance(x,SubscriptionCase)); code=pytest.main(['-q','tests/operations/test_subscription_contract_cases.py'],plugins=[c]); rs=c.results()
 report=build_case_report(domain='subscription_lifecycle',results=rs,required_rules=REQUIRED_SUBSCRIPTION_RULES,serialize_case=lambda case:{'action':case.action,'channel_state':case.channel_state,'follow_state':case.follow_state})
 out=root/'.artifacts/test-assurance/subscription/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); return int(code)
if __name__=='__main__': raise SystemExit(main())
