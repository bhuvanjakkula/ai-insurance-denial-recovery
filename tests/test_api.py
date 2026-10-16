import os
import tempfile
os.environ['DATABASE_URL']='sqlite:///'+tempfile.mktemp(suffix='.db')
os.environ['DEMO_API_KEY']='test-key'
from fastapi.testclient import TestClient
from backend.main import app
client=TestClient(app)
H={'X-API-Key':'test-key'}
DATA={'clinic_name':'Test Clinic','claim_number':'SYN-TEST-001','payer':'Example','service_date':'2026-09-01','denial_code':'CO-50','denied_amount':1200.0,'appeal_deadline':'2026-12-31'}

def test_health_and_auth():
    assert client.get('/health').status_code==200
    assert client.get('/api/claims').status_code==401

def test_workflow():
    r=client.post('/api/claims',json=DATA,headers=H);assert r.status_code==201,r.text
    cid=r.json()['id']
    assert client.post('/api/claims',json=DATA,headers=H).status_code==409
    detail=client.get(f'/api/claims/{cid}',headers=H).json()
    assert detail['analysis']['category']=='Medical necessity'
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
