"""End-to-end smoke test: every API call a UI button makes, in realistic order."""
import os
import tempfile

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('DEMO_API_KEY', 'e2e-key')

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402

c = TestClient(app)
H = {'X-API-Key': os.environ['DEMO_API_KEY']}


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), f'{r.request.method} {r.request.url.path} -> {r.status_code} {r.text[:200]}'
    return r.json() if 'json' in r.headers.get('content-type', '') else r.text


def test_static_pages():
    for p in ('/', '/app.css', '/app.js', '/health'):
        assert c.get(p).status_code == 200, p
    html = c.get('/').text
    assert 'Action Center' in html


def test_full_ui_workflow():
    # Seed button
    ok(c.post('/api/claims/seed-demo', headers=H))
    claims = ok(c.get('/api/claims', headers=H))
    assert len(claims) >= 10
    cid = claims[0]['id']

    # Dashboard metrics
    m = ok(c.get('/api/metrics', headers=H))
    assert m['claim_count'] >= 10

    # Action Center
    cc = ok(c.get('/api/analytics/command-center', headers=H))
    assert cc['todays_plays'] and cc['forecast']

    # Open studio
    detail = ok(c.get(f'/api/claims/{cid}', headers=H))
    assert 'analysis' in detail and 'claim' in detail
    ok(c.get(f'/api/claims/{cid}/intel', headers=H))

    # Generate appeal L1 + L2, edit, approve
    ap = ok(c.post(f'/api/claims/{cid}/appeals?level=1', headers=H))
    ok(c.post(f'/api/claims/{cid}/appeals?level=2', headers=H))
    ok(c.get(f'/api/appeals/{ap["id"]}', headers=H))
    ok(c.put(f'/api/appeals/{ap["id"]}', headers=H, json={'body': ap['body'] + '\nEdited.'}))
    ok(c.patch(f'/api/appeals/{ap["id"]}/approval', headers=H, json={'approved': True, 'reviewer': 'QA'}))

    # Status + outcome
    ok(c.patch(f'/api/claims/{cid}/status', headers=H, json={'status': 'submitted_manually'}))
    ok(c.post(f'/api/claims/{cid}/outcome', headers=H, json={'outcome_status': 'paid', 'recovered_amount': 100.0}))

    # Evidence radar
    s = ok(c.get(f'/api/claims/{cid}/evidence-radar/sample', headers=H))
    note = s.get('clinical_text') or s.get('text') or s.get('note')
    assert note
    ok(c.post(f'/api/claims/{cid}/evidence-radar', headers=H, json={'clinical_text': note}))
    ok(c.get(f'/api/claims/{cid}/evidence-radar', headers=H))

    # Batch ops
    ids = [x['id'] for x in claims[1:4]]
    ok(c.post('/api/claims/batch-appeals', headers=H, json={'claim_ids': ids, 'level': 1}))
    ok(c.post('/api/claims/batch-status', headers=H, json={'claim_ids': ids, 'status': 'in_review'}))

    # Intake + CSV
    ok(c.post('/api/claims', headers=H, json={
        'clinic_name': 'QA Clinic', 'claim_number': 'QA-1', 'payer': 'Aetna', 'denial_code': 'CO-50',
        'denied_amount': 500, 'service_date': '2026-01-01', 'appeal_deadline': '2026-12-01', 'notes': 'x'}))
    csv = ('claim_number,payer,denial_code,denied_amount,service_date,appeal_deadline,clinic_name,notes\n'
           'QA-CSV-1,Cigna,CO-197,250,2026-02-01,2026-12-01,QA Clinic,n\n')
    ok(c.post('/api/import/csv', headers=H, files={'file': ('c.csv', csv, 'text/csv')}))

    # Export, audit, compliance
    assert 'claim_number' in ok(c.get('/api/export/claims.csv', headers=H))
    assert ok(c.get('/api/audit', headers=H))
    assert ok(c.post('/api/compliance/audit/verify', headers=H))['intact'] is True
    ok(c.post('/api/compliance/redact', headers=H, json={'text': 'SSN 123-45-6789'}))
    ok(c.get('/api/llm/status'))


def test_remittance_ui_flow():
    s = ok(c.get('/api/remittance/samples', headers=H))
    for key, kind in (('sample_835', '835'), ('sample_eob', 'eob')):
        res = ok(c.post('/api/remittance/parse', headers=H, json={'text': s[key], 'source_type': kind}))
        assert res['count'] > 0
        claims = [{
            'claim_number': 'UI-' + kind + '-' + l['claim_number'], 'payer': l['payer'],
            'service_date': l.get('service_date') or '2026-01-01', 'denial_code': l['denial_code'],
            'denied_amount': l['denied_amount'], 'appeal_deadline': l.get('appeal_deadline'),
            'notes': l.get('carc_description') or ''} for l in res['lines']]
        r = ok(c.post('/api/remittance/import', headers=H, json={'claims': claims}))
        assert r['imported'] == len(claims)
        # re-import is idempotent
        assert ok(c.post('/api/remittance/import', headers=H, json={'claims': claims}))['imported'] == 0
