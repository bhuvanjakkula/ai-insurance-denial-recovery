import os
import tempfile
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ['DEMO_API_KEY'] = 'test-key'
from fastapi.testclient import TestClient
from backend.main import app
from backend.radar import analyze_evidence, phi_findings, sample_note

client = TestClient(app)
H = {'X-API-Key': 'test-key'}


class Claim:
    denied_amount = 2000.0
    payer = 'Aetna'


def status_of(result, cid):
    return next(c['status'] for c in result['criteria'] if c['id'] == cid)


def test_sample_note_reveals_realistic_gaps():
    r = analyze_evidence(Claim(), 'Medical necessity', sample_note('Medical necessity'))
    assert status_of(r, 'diagnosis') == 'MET'
    assert status_of(r, 'exam') == 'MET'
    assert status_of(r, 'conservative') == 'PARTIAL'  # 4 weeks < 6
    assert status_of(r, 'attestation') == 'MISSING'
    assert r['verdict'] in ('STRENGTHEN', 'AT RISK')
    assert any(b['id'] == 'attestation' for b in r['blockers'])
    # critical + missing gap is ranked first
    assert r['gap_plan'][0]['critical'] and r['gap_plan'][0]['status'] == 'MISSING'
    assert r['win_likelihood']['potential'] > r['win_likelihood']['current']


def test_duration_threshold_is_configurable():
    note = ('Assessment: lumbar strain (M54.50). Completed 4 weeks of physical therapy with no improvement. '
            'Exam shows tenderness and 4/5 strength.')
    strict = analyze_evidence(Claim(), 'Medical necessity', note, conservative_weeks=6)
    lenient = analyze_evidence(Claim(), 'Medical necessity', note, conservative_weeks=4)
    assert status_of(strict, 'conservative') == 'PARTIAL'
    assert status_of(lenient, 'conservative') == 'MET'
    assert lenient['criteria'][3]['metrics']['documented_weeks'] == 4.0


def test_negation_is_flagged_as_contradiction():
    note = 'Patient has not tried physical therapy. Assessment: back pain (M54.50). Pain limits walking.'
    r = analyze_evidence(Claim(), 'Medical necessity', note)
    item = next(c for c in r['criteria'] if c['id'] == 'conservative')
    assert item['status'] == 'MISSING'
    assert item['contradiction'] is not None


def test_complete_chart_is_ready():
    note = (
        'Patient reports 10 weeks of progressive low back pain rated 8/10. Assessment: lumbar radiculopathy (M54.16). '
        'Completed 8 weeks of physical therapy and NSAIDs with no improvement. '
        'Exam: positive straight leg raise, 4/5 left dorsiflexion strength, diminished reflexes. '
        'X-ray shows disc space narrowing at L5-S1. Pain limits walking, sleep and work. '
        'MRI will help guide surgical planning. Per ACR appropriateness criteria, MRI is indicated. '
        'Electronically signed by Dr. Lee, NPI: 1234567890.'
    )
    r = analyze_evidence(Claim(), 'Medical necessity', note)
    assert r['verdict'] == 'READY', r['counts']
    assert r['blockers'] == []
    assert r['coverage'] >= 85


def test_phi_guard_and_short_text():
    assert set(phi_findings('SSN 123-45-6789, DOB: 1/2/1980')) >= {'SSN', 'date of birth'}
    assert phi_findings('Patient has 7 weeks of low back pain.') == []
    try:
        analyze_evidence(Claim(), 'Medical necessity', 'too short')
        assert False, 'expected ValueError'
    except ValueError:
        pass


def test_radar_api_and_appeal_integration():
    claim = {
        'clinic_name': 'Radar Clinic', 'claim_number': 'SYN-RADAR-001', 'payer': 'UnitedHealthcare',
        'service_date': '2026-09-01', 'denial_code': 'CO-50', 'denied_amount': 3000.0,
        'appeal_deadline': '2026-12-31',
    }
    cid = client.post('/api/claims', json=claim, headers=H).json()['id']

    assert client.get(f'/api/claims/{cid}/evidence-radar', headers=H).json() == {'scan': None}
    assert client.get(f'/api/claims/{cid}/evidence-radar').status_code == 401

    sample = client.get(f'/api/claims/{cid}/evidence-radar/sample', headers=H).json()
    assert sample['category'] == 'Medical necessity'

    bad = client.post(f'/api/claims/{cid}/evidence-radar', headers=H,
                      json={'clinical_text': sample['clinical_text'] + ' SSN 123-45-6789 on file.'})
    assert bad.status_code == 422 and 'SSN' in bad.text

    r = client.post(f'/api/claims/{cid}/evidence-radar', headers=H,
                    json={'clinical_text': sample['clinical_text']})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body['category'] == 'Medical necessity'
    assert body['axes'] and body['gap_plan'] and len(body['note_sha256']) == 64
    assert client.get(f'/api/claims/{cid}/evidence-radar', headers=H).json()['scan']['scan_id'] == body['scan_id']

    appeal = client.post(f'/api/claims/{cid}/appeals', headers=H).json()
    assert 'EVIDENCE MAP' in appeal['body']
    assert 'Close evidence gap before sending' in appeal['body']

    other = {**claim, 'claim_number': 'SYN-RADAR-002'}
    cid2 = client.post('/api/claims', json=other, headers=H).json()['id']
    plain = client.post(f'/api/claims/{cid2}/appeals', headers=H).json()
    assert 'EVIDENCE MAP' not in plain['body']
    assert client.post('/api/claims/999999/evidence-radar', headers=H,
                       json={'clinical_text': sample['clinical_text']}).status_code == 404
