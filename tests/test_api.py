import os
import tempfile
os.environ['DATABASE_URL']='sqlite:///'+tempfile.mktemp(suffix='.db')
os.environ['DEMO_API_KEY']='test-key'
from fastapi.testclient import TestClient
from backend.main import app
client=TestClient(app)
H={'X-API-Key':'test-key'}
DATA={'clinic_name':'Test Clinic','claim_number':'SYN-TEST-001','payer':'Aetna Health','service_date':'2026-09-01','denial_code':'CO-50','denied_amount':1200.0,'appeal_deadline':'2026-12-31'}

def test_health_and_auth():
    assert client.get('/health').status_code==200
    assert client.get('/api/claims').status_code==401

def test_workflow():
    r=client.post('/api/claims',json=DATA,headers=H);assert r.status_code==201,r.text
    cid=r.json()['id']
    assert client.post('/api/claims',json=DATA,headers=H).status_code==409
    detail=client.get(f'/api/claims/{cid}',headers=H).json()
    assert detail['analysis']['category']=='Medical necessity'
    assert 'aetna' in detail['analysis']['payer_dispatch']['payer_name'].lower()
    assert client.get('/api/metrics',headers=H).json()['denied_total']>=1200
    a=client.post(f'/api/claims/{cid}/appeals',headers=H);assert a.status_code==201
    aid=a.json()['id'];assert 'HUMAN REVIEW' in a.json()['body']
    approved=client.patch(f'/api/appeals/{aid}/approval',headers=H,json={'approved':True})
    assert approved.json()['approved'] is True
    assert client.patch(f'/api/claims/{cid}/status',headers=H,json={'status':'not_valid'}).status_code==422
    assert client.get('/api/export/claims.csv',headers=H).status_code==200
    assert client.get('/api/audit',headers=H).json()

def test_csv_import():
    csv_data='clinic_name,claim_number,payer,service_date,denial_code,denied_amount\nClinic,SYN-CSV-1,Example,2026-08-01,16,75.00\n'
    r=client.post('/api/import/csv',headers=H,files={'file':('claims.csv',csv_data,'text/csv')})
    assert r.status_code==200,r.text
    assert r.json()['imported']==1

def test_date_validation():
    bad={**DATA,'claim_number':'SYN-BAD','service_date':'tomorrow'}
    assert client.post('/api/claims',json=bad,headers=H).status_code==422

def test_outcome_and_batch_actions():
    # Create two claims
    c1 = client.post('/api/claims', json={**DATA, 'claim_number': 'SYN-BATCH-1', 'denied_amount': 800.0}, headers=H).json()
    c2 = client.post('/api/claims', json={**DATA, 'claim_number': 'SYN-BATCH-2', 'denied_amount': 950.0}, headers=H).json()
    
    # Batch generate appeals
    res_batch = client.post('/api/claims/batch-appeals', json={'claim_ids': [c1['id'], c2['id']], 'level': 1}, headers=H)
    assert res_batch.status_code == 200
    assert res_batch.json()['generated_count'] == 2

    # Batch status update
    res_stat = client.post('/api/claims/batch-status', json={'claim_ids': [c1['id'], c2['id']], 'status': 'submitted_manually'}, headers=H)
    assert res_stat.status_code == 200
    assert res_stat.json()['updated_count'] == 2

    # Record won outcome with recovery dollar amount
    res_outcome = client.post(f"/api/claims/{c1['id']}/outcome", json={'outcome_status': 'paid', 'recovered_amount': 800.0, 'notes': 'Overturned on Level 1 Appeal'}, headers=H)
    assert res_outcome.status_code == 200
    assert res_outcome.json()['recovered_amount'] == 800.0

    # Level 2 escalation appeal creation
    res_l2 = client.post(f"/api/claims/{c2['id']}/appeals?level=2", headers=H)
    assert res_l2.status_code == 201
    assert 'LEVEL 2 FORMAL ESCALATION' in res_l2.json()['body']
