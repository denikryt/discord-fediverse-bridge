#!/usr/bin/env python3
"""Emit passive identity/discovery contract coverage."""
from __future__ import annotations
import json, sys
from pathlib import Path
import pytest
try:
    from tools.assurance_reporting import PassiveCaseCollector, build_case_report
except ModuleNotFoundError:
    from assurance_reporting import PassiveCaseCollector, build_case_report

def main() -> int:
    root=Path(__file__).resolve().parents[1]
    for p in (root, root/'tests'):
        if str(p) not in sys.path: sys.path.insert(0,str(p))
    from support.identity_discovery_contracts import IdentityDiscoveryCase, REQUIRED_IDENTITY_DISCOVERY_RULES
    collector=PassiveCaseCollector(accepts=lambda c:isinstance(c,IdentityDiscoveryCase))
    code=pytest.main(['-q','tests/test_identity_discovery_contract_cases.py'],plugins=[collector])
    results=collector.results()
    report=build_case_report(domain='identity_discovery',results=results,required_rules=REQUIRED_IDENTITY_DISCOVERY_RULES,serialize_case=lambda case:{'action':case.action})
    out=root/'.artifacts/test-assurance/identity-discovery/report.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    return int(code)
if __name__=='__main__': raise SystemExit(main())
