import pytest
from datetime import date, timedelta
from backend.engine import assess, draft_appeal, lookup_payer_dispatch

class MockClaim:
    def __init__(self, denial_code, denied_amount, service_date="2026-09-01", appeal_deadline=None, claim_number="MOCK-01", clinic_name="Metro Clinic", payer="Aetna"):
        self.denial_code = denial_code
        self.denied_amount = denied_amount
        self.service_date = service_date
        self.appeal_deadline = appeal_deadline
        self.claim_number = claim_number
        self.clinic_name = clinic_name
        self.payer = payer

def test_medical_necessity_carc50():
    c = MockClaim(denial_code="CO-50", denied_amount=2500.0)
    analysis = assess(c)
    assert analysis['category'] == 'Medical necessity'
    assert analysis['priority_score'] > 0
    assert 'clinical' in analysis['recommended_action'].lower()
    assert 'LCD/NCD' in analysis['statutory_citation'] or 'CMS' in analysis['statutory_citation']
    assert len(analysis['required_documents']) >= 3

def test_prior_auth_carc197():
    c = MockClaim(denial_code="CO-197", denied_amount=4000.0)
    analysis = assess(c)
    assert analysis['category'] == 'Authorization missing'
    assert 'authorization' in analysis['recommended_action'].lower()

def test_timely_filing_carc29():
    c = MockClaim(denial_code="29", denied_amount=600.0)
    analysis = assess(c)
    assert analysis['category'] == 'Timely filing'
    assert 'clearinghouse' in analysis['recommended_action'].lower()

def test_urgent_deadline_calculation():
    urgent_deadline = (date.today() + timedelta(days=4)).isoformat()
    c = MockClaim(denial_code="CO-50", denied_amount=5000.0, appeal_deadline=urgent_deadline)
    analysis = assess(c)
    assert analysis['urgency_tier'] == 'CRITICAL'
    assert analysis['days_to_deadline'] == 4
    assert analysis['priority_score'] >= 75
    assert analysis['priority_label'] == 'CRITICAL'

def test_expired_deadline_warning():
    past_deadline = (date.today() - timedelta(days=5)).isoformat()
    c = MockClaim(denial_code="CO-29", denied_amount=300.0, appeal_deadline=past_deadline)
    analysis = assess(c)
    assert analysis['urgency_tier'] == 'EXPIRED'
    assert 'has passed' in analysis['recommended_action']

def test_draft_appeal_generation():
    c = MockClaim(denial_code="CO-16", denied_amount=1500.0, claim_number="SYN-999")
    analysis = assess(c)
    draft = draft_appeal(c, analysis, level=1)
    assert 'FORMAL MEDICAL CLAIM APPEAL' in draft
    assert 'SYN-999' in draft
    assert '$1,500.00' in draft
    assert 'HUMAN REVIEW REQUIRED' in draft
    assert 'MANDATORY HUMAN REVIEW CHECKLIST' in draft

def test_level2_escalation_draft():
    c = MockClaim(denial_code="CO-50", denied_amount=3200.0, claim_number="SYN-ESC-01")
    analysis = assess(c)
    draft_l2 = draft_appeal(c, analysis, level=2)
    assert 'LEVEL 2 FORMAL ESCALATION APPEAL' in draft_l2
    assert '29 U.S.C. § 1133' in draft_l2
    assert 'State Department of Insurance' in draft_l2

def test_payer_dispatch_lookup():
    info = lookup_payer_dispatch("Aetna Commercial Health Plan")
    assert 'availity' in info['appeals_portal'].lower()
    assert info['appeals_fax'] == '1-859-455-8650'

    uhc_info = lookup_payer_dispatch("UnitedHealthcare Choice Plus")
    assert 'uhcprovider' in uhc_info['appeals_portal'].lower()
    assert '801' in uhc_info['appeals_fax']
