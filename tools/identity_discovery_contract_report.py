#!/usr/bin/env python3
"""Emit passive identity/discovery contract coverage."""
from __future__ import annotations
import json, sys
from pathlib import Path
import pytest
try:
    from tools.contract_report_support import PassiveCaseCollector, status_totals
except ModuleNotFoundError:
    from contract_report_support import PassiveCaseCollector, status_totals

def main() -> int:
    root=Path(__file__).resolve().parents[1]
    for p in (root, root/'tests'):
        if str(p) not in sys.path: sys.path.insert(0,str(p))
    from support.identity_discovery_contracts import IdentityDiscoveryCase, REQUIRED_IDENTITY_DISCOVERY_RULES
    collector=PassiveCaseCollector(accepts=lambda c:isinstance(c,IdentityDiscoveryCase))
    code=pytest.main(['-q','tests/test_identity_discovery_contract_cases.py'],plugins=[collector])
    results=collector.results(); ids={r.case.id for r in results}
    missing=[rule.id for rule in REQUIRED_IDENTITY_DISCOVERY_RULES if not ids.intersection(rule.represented_by)]
    report={'domain':'identity_discovery','summary':{'required_rules':len(REQUIRED_IDENTITY_DISCOVERY_RULES),'represented_rules':len(REQUIRED_IDENTITY_DISCOVERY_RULES)-len(missing),'missing_rules':len(missing),'statuses':status_totals(results)},'missing_rule_ids':missing,'cases':[{'id':r.case.id,'nodeid':r.nodeid,'status':r.status,'action':r.case.action} for r in results]}
    out=root/'.artifacts/test-assurance/identity-discovery/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    return int(code)
if __name__=='__main__': raise SystemExit(main())
