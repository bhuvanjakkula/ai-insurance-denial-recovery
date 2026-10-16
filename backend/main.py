"""Denial Recovery demo API. Synthetic/de-identified data only."""
import csv
import io
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, ForeignKey, select, func
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from backend.engine import assess, draft_appeal

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get('DATABASE_URL', f'sqlite:///{ROOT / "denials.db"}')
engine = create_engine(DB_URL, connect_args={'check_same_thread': False} if DB_URL.startswith('sqlite') else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
class Base(DeclarativeBase): pass
class Claim(Base):
    __tablename__ = 'claims'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    clinic_name = Column(String(150), nullable=False)
    claim_number = Column(String(120), nullable=False)
    payer = Column(String(150), nullable=False)
    service_date = Column(String(10), nullable=False)
    denial_code = Column(String(30), nullable=False)
    denied_amount = Column(Float, nullable=False)
    appeal_deadline = Column(String(10), nullable=True)
    status = Column(String(30), default='new', nullable=False)
    notes = Column(Text, default='')
    created_at = Column(String(40), nullable=False)
class Appeal(Base):
    __tablename__ = 'appeals'
    id = Column(Integer, primary_key=True)
    claim_id = Column(Integer, ForeignKey('claims.id'), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    body = Column(Text, nullable=False)
    approved = Column(Integer, default=0, nullable=False)
    created_at = Column(String(40), nullable=False)
class Audit(Base):
    __tablename__ = 'audit_events'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    action = Column(String(80), nullable=False)
    entity_id = Column(Integer, nullable=True)
    timestamp = Column(String(40), nullable=False)

class ClaimInput(BaseModel):
    clinic_name: str = Field(min_length=1, max_length=150)
    claim_number: str = Field(min_length=1, max_length=120)
    payer: str = Field(min_length=1, max_length=150)
    service_date: str
    denial_code: str = Field(min_length=1, max_length=30)
    denied_amount: float = Field(gt=0, le=100000000)
    appeal_deadline: Optional[str] = None
    notes: str = Field(default='', max_length=2000)
class StatusInput(BaseModel):
    status: str
class AppealInput(BaseModel):
    approved: bool

def now(): return datetime.now(timezone.utc).isoformat()
def db_session():
    with SessionLocal() as session: yield session

def tenant_auth(x_api_key: str | None = Header(default=None)) -> str:
    configured = os.environ.get('DEMO_API_KEY', 'change-me-for-local-demo')
    if not x_api_key or not secrets.compare_digest(x_api_key, configured):
        raise HTTPException(401, 'Invalid API key')
    return os.environ.get('TENANT_ID', 'demo-clinic')

def log(db, tenant, action, entity_id=None):
    db.add(Audit(tenant_id=tenant, action=action, entity_id=entity_id, timestamp=now()))

def get_claim(db, tenant, claim_id):
    result = db.scalar(select(Claim).where(Claim.id == claim_id, Claim.tenant_id == tenant))
    if not result: raise HTTPException(404, 'Claim not found')
    return result

def serialize(c):
    return {key: getattr(c, key) for key in ['id', 'clinic_name', 'claim_number', 'payer', 'service_date', 'denial_code', 'denied_amount', 'appeal_deadline', 'status', 'notes', 'created_at']}

def validate_dates(item):
    from datetime import date
    try:
        date.fromisoformat(item.service_date)
        if item.appeal_deadline: date.fromisoformat(item.appeal_deadline)
    except (TypeError, ValueError): raise HTTPException(422, 'Dates must be ISO YYYY-MM-DD')

app = FastAPI(title='AI Insurance Denial Recovery — Demo API', version='0.1.0', description='Explainable rules-based prototype; synthetic data only.')
app.add_middleware(CORSMiddleware, allow_origins=[], allow_credentials=False, allow_methods=['GET','POST','PATCH'], allow_headers=['X-API-Key','Content-Type'])
Base.metadata.create_all(engine)

@app.get('/')
def index(): return FileResponse(ROOT / 'frontend' / 'index.html')
@app.get('/app.css')
def css(): return FileResponse(ROOT / 'frontend' / 'app.css', media_type='text/css')
@app.get('/app.js')
def javascript(): return FileResponse(ROOT / 'frontend' / 'app.js', media_type='application/javascript')
@app.get('/health')
def health(): return {'status': 'ok', 'mode':'demo', 'ai_mode': 'explainable-rules'}

@app.get('/api/claims')
def list_claims(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session), status: str | None = Query(default=None)):
    stmt = select(Claim).where(Claim.tenant_id == tenant)
    if status: stmt = stmt.where(Claim.status == status)
    return [serialize(c) for c in db.scalars(stmt.order_by(Claim.id.desc())).all()]

@app.post('/api/claims', status_code=201)
def create_claim(item: ClaimInput, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    validate_dates(item)
    if db.scalar(select(Claim.id).where(Claim.tenant_id == tenant, Claim.claim_number == item.claim_number)):
        raise HTTPException(409, 'Claim number already exists within clinic')
    c = Claim(**item.model_dump(), tenant_id=tenant, created_at=now())
    db.add(c); db.flush(); log(db, tenant, 'claim_created', c.id); db.commit(); db.refresh(c)
    return serialize(c)

@app.get('/api/claims/{claim_id}')
def claim_detail(claim_id: int, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    c = get_claim(db, tenant, claim_id)
    return {'claim': serialize(c), 'analysis': assess(c)}

@app.patch('/api/claims/{claim_id}/status')
def change_status(claim_id: int, payload: StatusInput, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    if payload.status not in {'new','in_review','appeal_drafted','submitted_manually','resolved','closed'}: raise HTTPException(422, 'Unsupported status')
    c = get_claim(db, tenant, claim_id); c.status = payload.status
    log(db, tenant, 'status_changed_' + payload.status, c.id); db.commit()
    return serialize(c)

@app.post('/api/claims/{claim_id}/appeals', status_code=201)
def create_appeal(claim_id: int, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    c = get_claim(db, tenant, claim_id)
    a = Appeal(claim_id=c.id, tenant_id=tenant, body=draft_appeal(c, assess(c)), created_at=now())
    db.add(a); c.status = 'appeal_drafted'; db.flush(); log(db, tenant, 'appeal_draft_created', a.id); db.commit(); db.refresh(a)
    return {'id':a.id, 'claim_id':c.id, 'body':a.body, 'approved':False}

@app.get('/api/appeals/{appeal_id}')
def read_appeal(appeal_id: int, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    a = db.scalar(select(Appeal).where(Appeal.id == appeal_id, Appeal.tenant_id == tenant))
    if not a: raise HTTPException(404, 'Appeal not found')
    return {'id':a.id, 'claim_id':a.claim_id, 'body':a.body, 'approved':bool(a.approved)}

@app.patch('/api/appeals/{appeal_id}/approval')
def approve_appeal(appeal_id: int, payload: AppealInput, tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    a = db.scalar(select(Appeal).where(Appeal.id == appeal_id, Appeal.tenant_id == tenant))
    if not a: raise HTTPException(404, 'Appeal not found')
    a.approved = int(payload.approved); log(db, tenant, 'appeal_approval_changed', a.id); db.commit()
    return {'id':a.id, 'approved':bool(a.approved), 'note':'Approval does not submit the appeal.'}

@app.get('/api/metrics')
def metrics(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    rows = db.scalars(select(Claim).where(Claim.tenant_id == tenant)).all()
    return {'claim_count':len(rows), 'denied_total':round(sum(c.denied_amount for c in rows),2), 'pending_count':sum(c.status not in ('resolved','closed') for c in rows), 'by_category': {k:sum(assess(c)['category']==k for c in rows) for k in sorted({assess(c)['category'] for c in rows})}}

@app.post('/api/import/csv')
async def import_csv(file: UploadFile = File(...), tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    data = await file.read(2_000_001)
    if len(data)>2_000_000: raise HTTPException(413, 'CSV limit 2 MB')
    try:
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
        if not reader.fieldnames: raise ValueError('Missing header')
        needed = {'clinic_name','claim_number','payer','service_date','denial_code','denied_amount'}
        if not needed.issubset(reader.fieldnames): raise ValueError('Missing columns: '+', '.join(sorted(needed-set(reader.fieldnames))))
        parsed = [ClaimInput(**{k:(row.get(k) or None) for k in needed}, appeal_deadline=row.get('appeal_deadline') or None, notes=row.get('notes') or '') for row in reader]
        if len(parsed)>1000: raise ValueError('Maximum 1000 rows')
        for p in parsed: validate_dates(p)
        numbers = [p.claim_number for p in parsed]
        if len(numbers)!=len(set(numbers)): raise ValueError('Duplicate claim numbers in CSV')
        existing = set(db.scalars(select(Claim.claim_number).where(Claim.tenant_id == tenant, Claim.claim_number.in_(numbers))).all())
        if existing: raise ValueError('Already imported: '+', '.join(sorted(existing)[:5]))
    except (UnicodeError, ValueError, TypeError) as e: raise HTTPException(422, str(e))
    for p in parsed: db.add(Claim(**p.model_dump(), tenant_id=tenant, created_at=now()))
    log(db, tenant, 'csv_import', len(parsed)); db.commit()
    return {'imported':len(parsed)}

@app.get('/api/audit')
def audit(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    rows = db.scalars(select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.desc()).limit(100)).all()
    return [{'id':x.id,'action':x.action,'entity_id':x.entity_id,'timestamp':x.timestamp} for x in rows]

@app.get('/api/export/claims.csv')
def export_claims(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    out=io.StringIO(); names=['id','clinic_name','claim_number','payer','service_date','denial_code','denied_amount','appeal_deadline','status','notes','created_at']
    w=csv.DictWriter(out, fieldnames=names); w.writeheader()
    for c in db.scalars(select(Claim).where(Claim.tenant_id==tenant).order_by(Claim.id)).all():
        d=serialize(c)
        # Prevent spreadsheet formula execution in exported user-controlled fields
        for k,v in d.items():
            if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')): d[k]="'"+v
        w.writerow(d)
    return PlainTextResponse(out.getvalue(), media_type='text/csv', headers={'Content-Disposition':'attachment; filename="claims.csv"'})
