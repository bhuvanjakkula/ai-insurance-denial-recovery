import os
import tempfile
import json
import pytest
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ['DEMO_API_KEY'] = 'test-key'
from fastapi.testclient import TestClient
from backend.main import app
from backend.denial_intel import sample_835, sample_eob, parse_835, parse_eob_text, score_denial
from backend.compliance import redact_phi, chain_hash, verify_chain, GENESIS
from backend.forecast import build_command_center

client = TestClient(app)
H = {'X-API-Key': 'test-key'}

def test_remittance_samples_endpoint():
    res = client.get('/api/remittance/samples', headers=H)
    assert res.status_code == 200
    data = res.json()
    assert 'sample_835' in data
    assert 'sample_eob' in data
    assert 'ISA*' in data['sample_835']
    assert 'EXPLANATION OF BENEFITS' in data['sample_eob']

def test_remittance_parse_835():
    s835 = sample_835()
    res = client.post('/api/remittance/parse', json={'text': s835, 'source_type': '835'}, headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data['format'] == '835'
    assert data['count'] >= 3
    first = data['lines'][0]
    assert 'intel' in first
    assert 'probability' in first['intel']
    assert 'path' in first['intel']

def test_remittance_parse_eob_and_import():
    seob = sample_eob()
    res = client.post('/api/remittance/parse', json={'text': seob, 'source_type': 'eob'}, headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data['format'] == 'text'
    assert data['count'] >= 2
    
    # Now import those parsed claims
    import_payload = {
        'claims': [
            {
                'claim_number': l['claim_number'],
                'clinic_name': l.get('clinic_name') or 'Metro Health Specialists',
                'payer': l['payer'],
                'service_date': l['service_date'],
                'denial_code': l['denial_code'],
                'denied_amount': l['denied_amount'],
                'appeal_deadline': l.get('appeal_deadline'),
                'notes': 'Imported from EOB test'
            }
            for l in data['lines']
        ]
    }
    imp_res = client.post('/api/remittance/import', json=import_payload, headers=H)
    assert imp_res.status_code == 201
    imp_data = imp_res.json()
    assert imp_data['imported'] >= 2

def test_claim_intel_endpoint():
    # Seed claims first
    client.post('/api/claims/seed-demo', headers=H)
    claims = client.get('/api/claims', headers=H).json()
    assert len(claims) > 0
    target_id = claims[0]['id']
    
    res = client.get(f'/api/claims/{target_id}/intel', headers=H)
    assert res.status_code == 200
    data = res.json()
    assert 'intel' in data
    assert 'path' in data['intel']
    assert 'expected_value' in data['intel']
    assert 'factors' in data['intel']

def test_command_center_analytics():
    res = client.get('/api/analytics/command-center', headers=H)
    assert res.status_code == 200
    data = res.json()
    assert 'kpis' in data
    assert 'open_claims' in data['kpis']
    assert 'expected_recoverable' in data['kpis']
    assert 'forecast' in data
    assert len(data['forecast']) == 12
    assert 'payers' in data
    assert 'heatmap' in data
    assert 'todays_plays' in data
    assert 'prevention' in data

def test_compliance_audit_and_verification():
    # Fetch audit events
    res = client.get('/api/compliance/audit', headers=H)
    assert res.status_code == 200
    events = res.json()
    assert len(events) > 0
    assert 'hash' in events[0]
    assert 'prev_hash' in events[0]
    
    # Verify cryptographic integrity
    v_res = client.post('/api/compliance/audit/verify', headers=H)
    assert v_res.status_code == 200
    v_data = v_res.json()
    assert v_data['intact'] is True
    assert v_data['chained'] > 0

def test_phi_redaction():
    text_with_phi = "Patient John Doe, SSN 123-45-6789, phone 555-123-4567, email john@example.com."
    res = client.post('/api/compliance/redact', json={'text': text_with_phi}, headers=H)
    assert res.status_code == 200
    data = res.json()
    assert '[SSN]' in data['redacted_text']
    assert '[PHONE]' in data['redacted_text']
    assert '[EMAIL]' in data['redacted_text']
    assert data['findings'].get('SSN') == 1

def test_llm_status_and_appeal_enhancement():
    # Status check
    st_res = client.get('/api/llm/status')
    assert st_res.status_code == 200
    st_data = st_res.json()
    assert 'configured' in st_data
    
    # Appeal enhancement fallback (deterministic when no key configured)
    claims = client.get('/api/claims', headers=H).json()
    target_id = claims[0]['id']
    
    enh_res = client.post(f'/api/claims/{target_id}/enhance-appeal', json={}, headers=H)
    assert enh_res.status_code == 200
    enh_data = enh_res.json()
    assert 'enhanced_body' in enh_data
    assert 'DRAFT — HUMAN REVIEW REQUIRED' in enh_data['enhanced_body']
