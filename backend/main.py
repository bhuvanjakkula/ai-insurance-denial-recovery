"""
AI Insurance Denial Recovery — High-Performance Clinic RCM API.
Synthetic/de-identified data only.
"""
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Query, Body, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, ForeignKey, select, func, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from backend.engine import assess, draft_appeal, lookup_payer_dispatch, BENCHMARK_REVERSAL_RATES
from backend.radar import analyze_evidence, phi_findings, sample_note
from backend.denial_intel import (
    parse_remittance, parse_835, parse_eob_text, score_denial, sample_835, sample_eob,
    CARC_DESCRIPTIONS, RARC_CATALOG, days_until, category_for
)
from backend.forecast import build_command_center, payer_history
from backend.compliance import (
    chain_hash, verify_chain, redact_phi, resolve_key, has_permission, GENESIS
)
from backend.llm import enhance_appeal, status as llm_status
from backend import security, accounts

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get('DATABASE_URL', f'sqlite:///{ROOT / "denials.db"}')
engine = create_engine(
    DB_URL,
    connect_args={'check_same_thread': False} if DB_URL.startswith('sqlite') else {},
    pool_pre_ping=True
)
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

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
    recovered_amount = Column(Float, default=0.0, nullable=True)
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
    level = Column(Integer, default=1, nullable=False)
    approved = Column(Integer, default=0, nullable=False)
    created_at = Column(String(40), nullable=False)

class Audit(Base):
    __tablename__ = 'audit_events'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    action = Column(String(80), nullable=False)
    entity_id = Column(Integer, nullable=True)
    timestamp = Column(String(40), nullable=False)
    actor = Column(String(100), nullable=True, default='system')
    role = Column(String(50), nullable=True, default='admin')
    prev_hash = Column(String(64), nullable=True, default='GENESIS')
    hash = Column(String(64), nullable=True)

class EvidenceScan(Base):
    __tablename__ = 'evidence_scans'
    id = Column(Integer, primary_key=True)
    claim_id = Column(Integer, ForeignKey('claims.id'), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    coverage = Column(Integer, nullable=False)
    verdict = Column(String(20), nullable=False)
    note_sha256 = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)

class RadarInput(BaseModel):
    clinical_text: str = Field(min_length=40, max_length=20000)
    conservative_weeks: int = Field(default=6, ge=1, le=52)

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
    recovered_amount: Optional[float] = None
    notes: Optional[str] = None

class BatchStatusInput(BaseModel):
    claim_ids: List[int]
    status: str

class BatchAppealInput(BaseModel):
    claim_ids: List[int]
    level: int = 1

class AppealInput(BaseModel):
    approved: bool

class AppealUpdateInput(BaseModel):
    body: str

class OutcomeInput(BaseModel):
    outcome_status: str  # 'paid', 'partially_paid', 'denied_again', 'written_off'
    recovered_amount: float = 0.0
    notes: Optional[str] = None

class RemittanceInput(BaseModel):
    text: str = Field(min_length=10, max_length=500000)
    source_type: str = Field(default='auto')  # 'auto', '835', 'eob'

class RemittanceImportItem(BaseModel):
    claim_number: str
    clinic_name: str = 'Metro Health Specialists'
    payer: str
    service_date: str
    denial_code: str
    denied_amount: float
    appeal_deadline: Optional[str] = None
    notes: Optional[str] = ''

class RemittanceImportInput(BaseModel):
    claims: List[RemittanceImportItem]

class RedactInput(BaseModel):
    text: str = Field(min_length=1, max_length=100000)

class LLMEnhanceInput(BaseModel):
    body: Optional[str] = None

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def db_session():
    with SessionLocal() as session:
        yield session

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'

def get_auth(request: Request, x_api_key: str | None = Header(default=None)) -> Dict[str, str]:
    ip = _client_ip(request)
    default_tenant = os.environ.get('TENANT_ID', 'demo-clinic')

    # Passwordless owner session (post-quantum signed login -> HttpOnly cookie).
    sid = request.cookies.get(security.SESSION_COOKIE)
    usid = request.cookies.get(accounts.SESSION_COOKIE)

    def csrf_guard():
        # CSRF defence in depth: SameSite=Strict cookie PLUS a custom header on writes.
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-owner-session') != '1':
            raise HTTPException(403, 'Missing X-Owner-Session header')

    if not x_api_key and sid and security.session_valid(engine, sid):
        csrf_guard()
        return {'tenant': default_tenant, 'role': 'admin', 'actor': security.OWNER_EMAIL}

    # Customer account session: private tenant per customer, 3-month trial enforced server-side.
    if not x_api_key and usid:
        u = accounts.user_for_session(engine, usid)
        if not u:
            raise HTTPException(401, 'Session expired. Please sign in again.')
        csrf_guard()
        if accounts.account_view(u)['status'] == 'expired':
            raise HTTPException(402, 'Your free trial has ended. Choose a plan to continue.')
        return {'tenant': u['tenant_id'], 'role': 'admin', 'actor': u['email']}

    if not x_api_key and sid:
        raise HTTPException(401, 'Owner session expired')

    if security.limiter.blocked(f'auth-fail:{ip}', limit=20, window=60):
        raise HTTPException(429, 'Too many failed attempts. Try again in a minute.')
    configured = os.environ.get('DEMO_API_KEY', 'change-me-for-local-demo')
    raw_keys = os.environ.get('API_KEYS', '')
    resolved = resolve_key(x_api_key, configured, default_tenant, raw_keys)
    if not resolved:
        security.limiter.record(f'auth-fail:{ip}')
        raise HTTPException(401, 'Invalid API key')
    tenant, role = resolved
    return {'tenant': tenant, 'role': role, 'actor': f'{role}-user'}

def tenant_auth(auth: Dict[str, str] = Depends(get_auth)) -> str:
    return auth['tenant']

def log(db: Session, tenant: str, action: str, entity_id: int | None = None, actor: str = 'system', role: str = 'admin'):
    latest = db.scalar(
        select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.desc()).limit(1)
    )
    prev = (latest.hash if latest and latest.hash else GENESIS) if latest else GENESIS
    t_stamp = now()
    curr_hash = chain_hash(prev, tenant, action, entity_id, t_stamp, actor or 'system', role or 'admin')
    entry = Audit(
        tenant_id=tenant,
        action=action,
        entity_id=entity_id,
        timestamp=t_stamp,
        actor=actor or 'system',
        role=role or 'admin',
        prev_hash=prev,
        hash=curr_hash
    )
    db.add(entry)

def get_claim(db: Session, tenant: str, claim_id: int) -> Claim:
    result = db.scalar(select(Claim).where(Claim.id == claim_id, Claim.tenant_id == tenant))
    if not result:
        raise HTTPException(404, 'Claim not found')
    return result

def serialize(c: Claim) -> Dict[str, Any]:
    return {
        key: getattr(c, key, None)
        for key in [
            'id', 'clinic_name', 'claim_number', 'payer',
            'service_date', 'denial_code', 'denied_amount',
            'recovered_amount', 'appeal_deadline', 'status', 'notes', 'created_at'
        ]
    }

def validate_dates(item: ClaimInput):
    try:
        date.fromisoformat(item.service_date)
        if item.appeal_deadline:
            date.fromisoformat(item.appeal_deadline)
    except (TypeError, ValueError):
        raise HTTPException(422, 'Dates must be ISO YYYY-MM-DD')

def check_for_phi_indicators(text: str):
    if re.search(r'\b\d{3}-\d{2}-\d{4}\b', text or ''):
        raise HTTPException(422, 'Safety check: Text appears to contain an SSN. Real PHI is prohibited.')

app = FastAPI(
    title='AI Insurance Denial Recovery — Enterprise RCM Demo API',
    version='2.0.0',
    description='Action-oriented clinical denial triage, explainable ML denial intelligence, remittance parsing, recovery forecasting, hash-chained audit, and appeal recovery engine.'
)

# Same-origin by default (the bundled UI needs no CORS). Extend with CORS_ORIGINS=https://a,https://b
_cors = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=False,
    allow_methods=['GET', 'POST', 'PATCH', 'PUT'],
    allow_headers=['X-API-Key', 'Content-Type']
)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

@app.middleware('http')
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers['Content-Security-Policy'] = CSP
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=()'
    resp.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    resp.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    if request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https':
        resp.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains'
    if request.url.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

# Ensure database tables exist (with safe column upgrade for recovered_amount and audit hash chain)
Base.metadata.create_all(engine)
security.init_store(engine)
accounts.configure(engine)
app.include_router(accounts.router)
if DB_URL.startswith("sqlite"):
    import sqlite3
    db_file = DB_URL.replace("sqlite:///", "")
    try:
        with sqlite3.connect(db_file) as s_con:
            s_cur = s_con.cursor()
            cols_c = [r[1] for r in s_cur.execute("PRAGMA table_info(claims)").fetchall()]
            if "recovered_amount" not in cols_c:
                s_cur.execute("ALTER TABLE claims ADD COLUMN recovered_amount REAL DEFAULT 0.0")
            cols_a = [r[1] for r in s_cur.execute("PRAGMA table_info(appeals)").fetchall()]
            if "level" not in cols_a:
                s_cur.execute("ALTER TABLE appeals ADD COLUMN level INTEGER DEFAULT 1")
            cols_au = [r[1] for r in s_cur.execute("PRAGMA table_info(audit_events)").fetchall()]
            if "actor" not in cols_au:
                s_cur.execute("ALTER TABLE audit_events ADD COLUMN actor VARCHAR(100) DEFAULT 'system'")
            if "role" not in cols_au:
                s_cur.execute("ALTER TABLE audit_events ADD COLUMN role VARCHAR(50) DEFAULT 'admin'")
            if "prev_hash" not in cols_au:
                s_cur.execute("ALTER TABLE audit_events ADD COLUMN prev_hash VARCHAR(64) DEFAULT 'GENESIS'")
            if "hash" not in cols_au:
                s_cur.execute("ALTER TABLE audit_events ADD COLUMN hash VARCHAR(64)")
            s_con.commit()
    except Exception:
        pass

# Frontend Static Endpoints
@app.get('/')
def index():
    return FileResponse(ROOT / 'frontend' / 'index.html')

@app.get('/app.css')
def css():
    return FileResponse(ROOT / 'frontend' / 'app.css', media_type='text/css')

@app.get('/app.js')
def javascript():
    return FileResponse(ROOT / 'frontend' / 'app.js', media_type='application/javascript')

# Public pages (sign in / sign up / pricing & support / owner login)
for _path, _file in (('/signin', 'auth.html'), ('/signup', 'auth.html'), ('/auth', 'auth.html'), ('/pricing', 'pricing.html'), ('/owner-login', 'owner-login.html')):
    app.add_api_route(_path, (lambda f=_file: FileResponse(ROOT / 'frontend' / f)), methods=['GET'], include_in_schema=False)
for _path, _file in (('/auth.js', 'auth.js'), ('/pricing.js', 'pricing.js'), ('/owner-login.js', 'owner-login.js')):
    app.add_api_route(_path, (lambda f=_file: FileResponse(ROOT / 'frontend' / f, media_type='application/javascript')),
                      methods=['GET'], include_in_schema=False)
app.add_api_route('/owner-auto-login', accounts.owner_auto_login, methods=['GET'], include_in_schema=False)

@app.get('/health')
def health():
    return {
        'status': 'ok',
        'mode': 'demo',
        'ai_mode': 'explainable-rules',
        'version': '1.2.0'
    }

# --------------------------------------------------------------------------------------
# Post-quantum owner access (passwordless) + attestation
# --------------------------------------------------------------------------------------
class OwnerLoginInput(BaseModel):
    token: str = Field(min_length=20, max_length=8192)

@app.get('/owner-login')
def owner_login_page():
    return FileResponse(ROOT / 'frontend' / 'owner-login.html')

@app.get('/owner-login.js')
def owner_login_js():
    return FileResponse(ROOT / 'frontend' / 'owner-login.js', media_type='application/javascript')

@app.post('/api/owner/login')
def owner_login(payload: OwnerLoginInput, request: Request, response: Response):
    ip = _client_ip(request)
    if security.limiter.blocked(f'owner-login:{ip}', limit=5, window=60):
        raise HTTPException(429, 'Too many attempts. Wait a minute.')
    security.limiter.record(f'owner-login:{ip}')
    try:
        claims = security.verify_owner_token(payload.token)
    except security.TokenError:
        raise HTTPException(401, 'Invalid or expired owner token')
    if not security.consume_jti(engine, claims):
        raise HTTPException(401, 'Invalid or expired owner token')
    sid = security.create_session(engine)
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie(security.SESSION_COOKIE, sid, max_age=security.SESSION_TTL, httponly=True,
                        secure=secure, samesite='strict', path='/')
    with SessionLocal() as db:
        log(db, os.environ.get('TENANT_ID', 'demo-clinic'), 'owner_login_pqc', None,
            actor=security.OWNER_EMAIL, role='owner')
        db.commit()
    return {'ok': True, 'owner': security.OWNER_EMAIL, 'algorithm': security.ALGORITHM}

@app.get('/api/owner/session')
def owner_session(request: Request):
    ok = security.session_valid(engine, request.cookies.get(security.SESSION_COOKIE))
    return {'owner': ok, 'email': security.OWNER_EMAIL if ok else None}

@app.post('/api/owner/logout')
def owner_logout(request: Request, response: Response):
    security.destroy_session(engine, request.cookies.get(security.SESSION_COOKIE))
    response.delete_cookie(security.SESSION_COOKIE, path='/')
    return {'ok': True}

@app.get('/api/security/attestation')
def security_attestation(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    """ML-DSA-65 signature over the audit chain head: tamper evidence that survives quantum attackers."""
    head = db.scalar(select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.desc()).limit(1))
    count = db.scalar(select(func.count()).select_from(Audit).where(Audit.tenant_id == tenant)) or 0
    att = security.attest(tenant, (head.hash if head and head.hash else GENESIS), int(count), now())
    att['self_check'] = security.verify_attestation(att)
    return att

# Claims Management
@app.get('/api/claims')
def list_claims(
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    sort: str | None = Query(default=None)
):
    stmt = select(Claim).where(Claim.tenant_id == tenant)
    if status and status != 'all':
        stmt = stmt.where(Claim.status == status)

    claims = db.scalars(stmt.order_by(Claim.id.desc())).all()

    filtered = []
    for c in claims:
        d = serialize(c)
        analysis = assess(c)
        d['analysis'] = analysis

        if search:
            q = search.lower()
            if not (q in c.claim_number.lower() or q in c.payer.lower() or q in c.denial_code.lower() or q in c.clinic_name.lower()):
                continue

        if category and category != 'all':
            if analysis['category'].lower() != category.lower():
                continue

        filtered.append(d)

    if sort == 'priority':
        filtered.sort(key=lambda x: x['analysis']['priority_score'], reverse=True)
    elif sort == 'amount':
        filtered.sort(key=lambda x: x['denied_amount'], reverse=True)
    elif sort == 'deadline':
        filtered.sort(key=lambda x: x['appeal_deadline'] or '9999-99-99')

    return filtered

@app.post('/api/claims', status_code=201)
def create_claim(
    item: ClaimInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    validate_dates(item)
    check_for_phi_indicators(item.notes)

    if db.scalar(select(Claim.id).where(Claim.tenant_id == tenant, Claim.claim_number == item.claim_number)):
        raise HTTPException(409, 'Claim number already exists within clinic')

    c = Claim(**item.model_dump(), tenant_id=tenant, created_at=now())
    db.add(c)
    db.flush()
    log(db, tenant, 'claim_created', c.id)
    db.commit()
    db.refresh(c)
    return serialize(c)

@app.get('/api/claims/{claim_id}')
def claim_detail(
    claim_id: int,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    appeals = db.scalars(select(Appeal).where(Appeal.claim_id == c.id, Appeal.tenant_id == tenant).order_by(Appeal.id.desc())).all()
    appeal_list = [{'id': a.id, 'body': a.body, 'level': getattr(a, 'level', 1), 'approved': bool(a.approved), 'created_at': a.created_at} for a in appeals]

    return {
        'claim': serialize(c),
        'analysis': assess(c),
        'appeals': appeal_list
    }

@app.patch('/api/claims/{claim_id}/status')
def change_status(
    claim_id: int,
    payload: StatusInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    valid_statuses = {'new', 'in_review', 'appeal_drafted', 'submitted_manually', 'resolved', 'closed', 'paid', 'approved', 'denied_again'}
    if payload.status not in valid_statuses:
        raise HTTPException(422, f'Unsupported status: {payload.status}')
    c = get_claim(db, tenant, claim_id)
    c.status = payload.status
    if payload.recovered_amount is not None:
        c.recovered_amount = float(payload.recovered_amount)
    if payload.notes:
        c.notes = (c.notes + " | " + payload.notes).strip(" |")
    log(db, tenant, 'status_changed_' + payload.status, c.id)
    db.commit()
    return serialize(c)

# Practical Action: Outcome & Recovery Recorder
@app.post('/api/claims/{claim_id}/outcome')
def record_outcome(
    claim_id: int,
    payload: OutcomeInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    c.status = payload.outcome_status
    c.recovered_amount = payload.recovered_amount
    if payload.notes:
        c.notes = f"{c.notes or ''} [Outcome: {payload.notes}]".strip()
    log(db, tenant, f'outcome_recorded_{payload.outcome_status}', c.id)
    db.commit()
    return {
        'claim_id': c.id,
        'status': c.status,
        'recovered_amount': c.recovered_amount,
        'message': f'Outcome recorded: ${payload.recovered_amount:,.2f} recovered.'
    }

# Practical Action: Batch Status Updates
@app.post('/api/claims/batch-status')
def batch_status_update(
    payload: BatchStatusInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    valid_statuses = {'new', 'in_review', 'appeal_drafted', 'submitted_manually', 'resolved', 'closed', 'paid', 'approved'}
    if payload.status not in valid_statuses:
        raise HTTPException(422, 'Unsupported status')

    claims = db.scalars(
        select(Claim).where(Claim.tenant_id == tenant, Claim.id.in_(payload.claim_ids))
    ).all()

    for c in claims:
        c.status = payload.status

    log(db, tenant, f'batch_status_{payload.status}', len(claims))
    db.commit()
    return {'updated_count': len(claims), 'new_status': payload.status}

# Practical Action: Batch Appeal Drafting
@app.post('/api/claims/batch-appeals')
def batch_appeal_drafting(
    payload: BatchAppealInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    claims = db.scalars(
        select(Claim).where(Claim.tenant_id == tenant, Claim.id.in_(payload.claim_ids))
    ).all()

    created_appeals = []
    for c in claims:
        ana = assess(c)
        draft_text = draft_appeal(c, ana, level=payload.level, radar=latest_radar_result(db, tenant, c.id))
        a = Appeal(
            claim_id=c.id,
            tenant_id=tenant,
            body=draft_text,
            level=payload.level,
            created_at=now()
        )
        db.add(a)
        c.status = 'appeal_drafted'
        created_appeals.append(a)

    db.flush()
    log(db, tenant, 'batch_appeals_created', len(created_appeals))
    db.commit()
    return {'generated_count': len(created_appeals), 'level': payload.level}

# Appeals (Single)
@app.post('/api/claims/{claim_id}/appeals', status_code=201)
def create_appeal(
    claim_id: int,
    level: int = Query(default=1),
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    analysis = assess(c)
    draft_text = draft_appeal(c, analysis, level=level, radar=latest_radar_result(db, tenant, c.id))
    a = Appeal(
        claim_id=c.id,
        tenant_id=tenant,
        body=draft_text,
        level=level,
        created_at=now()
    )
    db.add(a)
    c.status = 'appeal_drafted'
    db.flush()
    log(db, tenant, f'appeal_draft_created_l{level}', a.id)
    db.commit()
    db.refresh(a)
    return {'id': a.id, 'claim_id': c.id, 'body': a.body, 'level': a.level, 'approved': False}

@app.get('/api/appeals/{appeal_id}')
def read_appeal(
    appeal_id: int,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    a = db.scalar(select(Appeal).where(Appeal.id == appeal_id, Appeal.tenant_id == tenant))
    if not a:
        raise HTTPException(404, 'Appeal not found')
    return {'id': a.id, 'claim_id': a.claim_id, 'body': a.body, 'level': getattr(a, 'level', 1), 'approved': bool(a.approved)}

@app.put('/api/appeals/{appeal_id}')
def update_appeal(
    appeal_id: int,
    payload: AppealUpdateInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    a = db.scalar(select(Appeal).where(Appeal.id == appeal_id, Appeal.tenant_id == tenant))
    if not a:
        raise HTTPException(404, 'Appeal not found')
    a.body = payload.body
    log(db, tenant, 'appeal_draft_edited', a.id)
    db.commit()
    return {'id': a.id, 'claim_id': a.claim_id, 'body': a.body, 'approved': bool(a.approved)}

@app.patch('/api/appeals/{appeal_id}/approval')
def approve_appeal(
    appeal_id: int,
    payload: AppealInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    a = db.scalar(select(Appeal).where(Appeal.id == appeal_id, Appeal.tenant_id == tenant))
    if not a:
        raise HTTPException(404, 'Appeal not found')
    a.approved = int(payload.approved)
    log(db, tenant, 'appeal_approval_changed', a.id)
    db.commit()
    return {'id': a.id, 'approved': bool(a.approved), 'note': 'Approval recorded. Claim is ready for manual submission.'}

# Evidence Gap Radar
def latest_scan(db: Session, tenant: str, claim_id: int) -> EvidenceScan | None:
    return db.scalar(
        select(EvidenceScan)
        .where(EvidenceScan.claim_id == claim_id, EvidenceScan.tenant_id == tenant)
        .order_by(EvidenceScan.id.desc())
    )

def latest_radar_result(db: Session, tenant: str, claim_id: int) -> Dict[str, Any] | None:
    scan = latest_scan(db, tenant, claim_id)
    return json.loads(scan.result_json) if scan else None

def scan_payload(scan: EvidenceScan) -> Dict[str, Any]:
    return {**json.loads(scan.result_json), 'scan_id': scan.id, 'scanned_at': scan.created_at}

@app.post('/api/claims/{claim_id}/evidence-radar', status_code=201)
def run_evidence_radar(
    claim_id: int,
    payload: RadarInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    found = phi_findings(payload.clinical_text)
    if found:
        raise HTTPException(422, 'Safety check: text appears to contain ' + ', '.join(found) + '. Paste de-identified text only.')
    try:
        result = analyze_evidence(c, assess(c)['category'], payload.clinical_text, payload.conservative_weeks)
    except ValueError as e:
        raise HTTPException(422, str(e))
    scan = EvidenceScan(
        claim_id=c.id,
        tenant_id=tenant,
        coverage=result['coverage'],
        verdict=result['verdict'],
        note_sha256=result['note_sha256'],
        result_json=json.dumps(result),
        created_at=now()
    )
    db.add(scan)
    db.flush()
    log(db, tenant, 'evidence_radar_scanned', c.id)
    db.commit()
    db.refresh(scan)
    return scan_payload(scan)

@app.get('/api/claims/{claim_id}/evidence-radar')
def read_evidence_radar(
    claim_id: int,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    scan = latest_scan(db, tenant, c.id)
    return {'scan': scan_payload(scan) if scan else None}

@app.get('/api/claims/{claim_id}/evidence-radar/sample')
def evidence_radar_sample(
    claim_id: int,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    category = assess(c)['category']
    return {'category': category, 'clinical_text': sample_note(category)}

# Metrics & Analytics
@app.get('/api/metrics')
def metrics(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    rows = db.scalars(select(Claim).where(Claim.tenant_id == tenant)).all()
    categories: Dict[str, int] = {}
    payer_exposure: Dict[str, float] = {}
    total_denied = 0.0
    total_recovered = 0.0
    pending_count = 0
    resolved_count = 0
    critical_count = 0

    for c in rows:
        d_amt = float(c.denied_amount or 0)
        r_amt = float(getattr(c, 'recovered_amount', 0) or 0)
        total_denied += d_amt
        total_recovered += r_amt

        payer = c.payer or 'Other Payer'
        payer_exposure[payer] = payer_exposure.get(payer, 0.0) + d_amt

        ana = assess(c)
        cat = ana['category']
        categories[cat] = categories.get(cat, 0) + 1

        if c.status in ('resolved', 'closed', 'paid'):
            resolved_count += 1
        else:
            pending_count += 1

        if ana['priority_score'] >= 75:
            critical_count += 1

    return {
        'claim_count': len(rows),
        'denied_total': round(total_denied, 2),
        'recovered_total': round(total_recovered, 2),
        'pending_count': pending_count,
        'resolved_count': resolved_count,
        'critical_count': critical_count,
        'by_category': categories,
        'by_payer': {k: round(v, 2) for k, v in sorted(payer_exposure.items(), key=lambda x: x[1], reverse=True)[:5]}
    }

# CSV Import & Export
@app.post('/api/import/csv')
async def import_csv(
    file: UploadFile = File(...),
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    data = await file.read(2_000_001)
    if len(data) > 2_000_000:
        raise HTTPException(413, 'CSV limit 2 MB')
    try:
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
        if not reader.fieldnames:
            raise ValueError('Missing header')
        needed = {'clinic_name', 'claim_number', 'payer', 'service_date', 'denial_code', 'denied_amount'}
        if not needed.issubset(reader.fieldnames):
            raise ValueError('Missing columns: ' + ', '.join(sorted(needed - set(reader.fieldnames))))
        parsed = [
            ClaimInput(
                **{k: (row.get(k) or None) for k in needed},
                appeal_deadline=row.get('appeal_deadline') or None,
                notes=row.get('notes') or ''
            )
            for row in reader
        ]
        if len(parsed) > 1000:
            raise ValueError('Maximum 1000 rows')
        for p in parsed:
            validate_dates(p)
        numbers = [p.claim_number for p in parsed]
        if len(numbers) != len(set(numbers)):
            raise ValueError('Duplicate claim numbers in CSV')
        existing = set(
            db.scalars(
                select(Claim.claim_number).where(
                    Claim.tenant_id == tenant,
                    Claim.claim_number.in_(numbers)
                )
            ).all()
        )
        if existing:
            raise ValueError('Already imported: ' + ', '.join(sorted(existing)[:5]))
    except (UnicodeError, ValueError, TypeError) as e:
        raise HTTPException(422, str(e))

    for p in parsed:
        db.add(Claim(**p.model_dump(), tenant_id=tenant, created_at=now()))
    log(db, tenant, 'csv_import', len(parsed))
    db.commit()
    return {'imported': len(parsed)}

@app.post('/api/claims/seed-demo')
def seed_demo_claims(
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    sample_claims = [
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-01", "payer": "Aetna Commercial", "service_date": "2026-09-02", "denial_code": "CO-50", "denied_amount": 2840.00, "appeal_deadline": "2026-11-05", "notes": "Lumbar MRI denied for conservative therapy failure proof"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-02", "payer": "UnitedHealthcare", "service_date": "2026-08-28", "denial_code": "CO-197", "denied_amount": 4200.00, "appeal_deadline": "2026-10-30", "notes": "Emergency knee arthroscopy prior auth reference disputed"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-03", "payer": "Blue Cross Blue Shield", "service_date": "2026-07-15", "denial_code": "CO-29", "denied_amount": 1650.00, "appeal_deadline": "2026-10-25", "notes": "Payer claimed timely filing window exceeded; clearinghouse 277 shows proof"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-04", "payer": "Cigna HealthCare", "service_date": "2026-09-10", "denial_code": "CO-16", "denied_amount": 950.00, "appeal_deadline": "2026-12-10", "notes": "Operative report missing physician signature stamp"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-05", "payer": "Humana Medicare Advantage", "service_date": "2026-09-14", "denial_code": "CO-18", "denied_amount": 1120.00, "appeal_deadline": "2026-12-01", "notes": "Bilateral excision billed without distinct -59 modifier"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-06", "payer": "Medicare Part B", "service_date": "2026-09-01", "denial_code": "CO-97", "denied_amount": 780.00, "appeal_deadline": "2026-12-15", "notes": "Separate E/M 99214 bundled with minor injection"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-07", "payer": "Kaiser Permanente", "service_date": "2026-09-20", "denial_code": "CO-109", "denied_amount": 3400.00, "appeal_deadline": "2026-11-20", "notes": "COB update needed; primary policy terminated, spouse policy active"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-08", "payer": "UnitedHealthcare", "service_date": "2026-09-22", "denial_code": "CO-50", "denied_amount": 5100.00, "appeal_deadline": "2026-10-28", "notes": "High dollar biologic infusion denied under medical policy"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-09", "payer": "Aetna Commercial", "service_date": "2026-09-25", "denial_code": "CO-197", "denied_amount": 1890.00, "appeal_deadline": "2026-11-12", "notes": "Outpatient echocardiogram pre-auth number omitted on 837P"},
        {"clinic_name": "Metro Health Specialists", "claim_number": "SYN-2026-10", "payer": "Cigna HealthCare", "service_date": "2026-08-10", "denial_code": "CO-29", "denied_amount": 820.00, "appeal_deadline": "2026-10-20", "notes": "Expiring in 4 days! High priority appeal needed immediately"}
    ]

    added = 0
    for item in sample_claims:
        if not db.scalar(select(Claim.id).where(Claim.tenant_id == tenant, Claim.claim_number == item["claim_number"])):
            c = Claim(**item, tenant_id=tenant, created_at=now())
            db.add(c)
            added += 1

    if added > 0:
        log(db, tenant, 'seeded_demo_claims', added)
        db.commit()

    return {'seeded': added, 'total_samples': len(sample_claims)}

@app.get('/api/audit')
def audit(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.desc()).limit(150)
    ).all()
    return [
        {
            'id': x.id,
            'action': x.action,
            'entity_id': x.entity_id,
            'timestamp': x.timestamp,
            'actor': getattr(x, 'actor', 'system') or 'system',
            'role': getattr(x, 'role', 'admin') or 'admin',
            'prev_hash': getattr(x, 'prev_hash', GENESIS) or GENESIS,
            'hash': getattr(x, 'hash', None),
        }
        for x in rows
    ]

@app.get('/api/export/claims.csv')
def export_claims(tenant: str = Depends(tenant_auth), db: Session = Depends(db_session)):
    out = io.StringIO()
    names = [
        'id', 'clinic_name', 'claim_number', 'payer', 'service_date',
        'denial_code', 'denied_amount', 'recovered_amount', 'appeal_deadline', 'status', 'notes', 'created_at'
    ]
    w = csv.DictWriter(out, fieldnames=names)
    w.writeheader()
    for c in db.scalars(select(Claim).where(Claim.tenant_id == tenant).order_by(Claim.id)).all():
        d = serialize(c)
        for k, v in d.items():
            if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')):
                d[k] = "'" + v
        w.writerow(d)
    return PlainTextResponse(
        out.getvalue(),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="claims.csv"'}
    )

# --------------------------------------------------------------------------------------
# Denial Intelligence: Explainable Scoring & Remark Codes
# --------------------------------------------------------------------------------------
@app.get('/api/claims/{claim_id}/intel')
def claim_intel(
    claim_id: int,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    ana = assess(c)
    category = ana['category']
    group_code = 'CO'
    if '-' in c.denial_code:
        group_code = c.denial_code.split('-')[0].upper()
    
    all_tenant_claims = [{**serialize(cl), 'category': assess(cl)['category']} for cl in db.scalars(select(Claim).where(Claim.tenant_id == tenant)).all()]
    hist = payer_history(all_tenant_claims).get((c.payer, category))
    
    radar_scan = latest_radar_result(db, tenant, c.id)
    coverage = radar_scan.get('coverage') if radar_scan else None
    
    d_left = days_until(c.appeal_deadline)
    intel = score_denial(
        category=category,
        group_code=group_code,
        rarcs=[],
        denied_amount=float(c.denied_amount),
        days_to_deadline=d_left,
        payer=c.payer,
        history=hist,
        evidence_coverage=coverage
    )
    return {'claim_id': c.id, 'analysis': ana, 'intel': intel}

# --------------------------------------------------------------------------------------
# Remittance & EOB Ingestion (X12 835 / Text EOB)
# --------------------------------------------------------------------------------------
@app.post('/api/remittance/parse')
def parse_remit(
    payload: RemittanceInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    try:
        if payload.source_type == '835':
            parsed = parse_835(payload.text)
        elif payload.source_type == 'eob':
            parsed = parse_eob_text(payload.text)
        else:
            parsed = parse_remittance(payload.text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    
    all_tenant_claims = [{**serialize(cl), 'category': assess(cl)['category']} for cl in db.scalars(select(Claim).where(Claim.tenant_id == tenant)).all()]
    hist_lookup = payer_history(all_tenant_claims)
    
    scored_lines = []
    for line in parsed.get('lines', []):
        cat = category_for(line['group_code'], line['carc'])
        h = hist_lookup.get((line['payer'], cat))
        d_left = days_until(line.get('appeal_deadline'))
        intel = score_denial(
            category=cat,
            group_code=line['group_code'],
            rarcs=line.get('rarcs', []),
            denied_amount=float(line['denied_amount']),
            days_to_deadline=d_left,
            payer=line['payer'],
            history=h
        )
        item = dict(line)
        item['category'] = cat
        item['intel'] = intel
        scored_lines.append(item)
    
    log(db, tenant, 'remittance_parsed', len(scored_lines))
    db.commit()
    return {
        'format': parsed.get('format', 'unknown'),
        'payer': parsed.get('payer'),
        'clinic_name': parsed.get('clinic_name'),
        'remit_date': parsed.get('remit_date'),
        'count': len(scored_lines),
        'lines': scored_lines
    }

@app.get('/api/remittance/samples')
def remittance_samples():
    return {
        'sample_835': sample_835(),
        'sample_eob': sample_eob()
    }

@app.post('/api/remittance/import', status_code=201)
def import_remittance_claims(
    payload: RemittanceImportInput,
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    if not payload.claims:
        raise HTTPException(422, 'No claims provided to import.')
    
    existing_numbers = set(
        db.scalars(
            select(Claim.claim_number).where(
                Claim.tenant_id == tenant,
                Claim.claim_number.in_([c.claim_number for c in payload.claims])
            )
        ).all()
    )
    
    added_count = 0
    for item in payload.claims:
        if item.claim_number in existing_numbers:
            continue
        c = Claim(
            tenant_id=tenant,
            clinic_name=item.clinic_name or 'Metro Health Specialists',
            claim_number=item.claim_number,
            payer=item.payer,
            service_date=item.service_date or date.today().isoformat(),
            denial_code=item.denial_code,
            denied_amount=float(item.denied_amount),
            appeal_deadline=item.appeal_deadline,
            notes=item.notes or '',
            status='new',
            created_at=now()
        )
        db.add(c)
        added_count += 1
    
    if added_count > 0:
        log(db, tenant, 'remittance_claims_imported', added_count)
        db.commit()
    
    return {'imported': added_count, 'total_submitted': len(payload.claims)}

# --------------------------------------------------------------------------------------
# Recovery Command Center Analytics
# --------------------------------------------------------------------------------------
@app.get('/api/analytics/command-center')
def command_center_analytics(
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    rows = db.scalars(select(Claim).where(Claim.tenant_id == tenant)).all()
    claims_data = []
    
    serialized_all = [serialize(c) for c in rows]
    hist = payer_history(serialized_all)
    
    for c in rows:
        item = serialize(c)
        ana = assess(c)
        item['category'] = ana['category']
        group_code = 'CO'
        if '-' in c.denial_code:
            group_code = c.denial_code.split('-')[0].upper()
        d_left = days_until(c.appeal_deadline)
        item['days'] = d_left
        
        radar_scan = latest_radar_result(db, tenant, c.id)
        cov = radar_scan.get('coverage') if radar_scan else None
        
        intel = score_denial(
            category=ana['category'],
            group_code=group_code,
            rarcs=[],
            denied_amount=float(c.denied_amount),
            days_to_deadline=d_left,
            payer=c.payer,
            history=hist.get((c.payer, ana['category'])),
            evidence_coverage=cov
        )
        item['intel'] = intel
        claims_data.append(item)
    
    cc = build_command_center(claims_data, today=date.today())
    return cc

# --------------------------------------------------------------------------------------
# Compliance & Audit Chain Verification
# --------------------------------------------------------------------------------------
@app.get('/api/compliance/audit')
def read_compliance_audit(
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    rows = db.scalars(
        select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.desc()).limit(200)
    ).all()
    return [
        {
            'id': x.id,
            'action': x.action,
            'entity_id': x.entity_id,
            'timestamp': x.timestamp,
            'actor': getattr(x, 'actor', 'system') or 'system',
            'role': getattr(x, 'role', 'admin') or 'admin',
            'prev_hash': getattr(x, 'prev_hash', GENESIS) or GENESIS,
            'hash': getattr(x, 'hash', None),
        }
        for x in rows
    ]

@app.post('/api/compliance/audit/verify')
def verify_audit(
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    rows = db.scalars(
        select(Audit).where(Audit.tenant_id == tenant).order_by(Audit.id.asc())
    ).all()
    res = verify_chain(rows)
    return res

@app.post('/api/compliance/redact')
def redact_text_endpoint(
    payload: RedactInput,
    tenant: str = Depends(tenant_auth)
):
    redacted, counts = redact_phi(payload.text)
    return {'redacted_text': redacted, 'findings': counts}

# --------------------------------------------------------------------------------------
# LLM Appeal Polish Engine
# --------------------------------------------------------------------------------------
@app.get('/api/llm/status')
def get_llm_status():
    return llm_status()

@app.post('/api/claims/{claim_id}/enhance-appeal')
def enhance_appeal_endpoint(
    claim_id: int,
    payload: LLMEnhanceInput = Body(default=LLMEnhanceInput()),
    tenant: str = Depends(tenant_auth),
    db: Session = Depends(db_session)
):
    c = get_claim(db, tenant, claim_id)
    ana = assess(c)
    
    body_to_enhance = payload.body
    if not body_to_enhance:
        latest_appeal = db.scalar(
            select(Appeal).where(Appeal.claim_id == c.id, Appeal.tenant_id == tenant).order_by(Appeal.id.desc())
        )
        if latest_appeal:
            body_to_enhance = latest_appeal.body
        else:
            body_to_enhance = draft_appeal(c, ana, level=1, radar=latest_radar_result(db, tenant, c.id))
    
    facts = {
        'category': ana['category'],
        'payer': c.payer,
        'claim_number': c.claim_number,
        'denied_amount': c.denied_amount,
    }
    
    res = enhance_appeal(body_to_enhance, facts)
    enhanced = res.get('text') or body_to_enhance
    log(db, tenant, 'appeal_llm_enhanced', c.id)
    db.commit()
    
    return {
        'claim_id': c.id,
        'enhanced_body': enhanced,
        'meta': res,
        'differs': enhanced.strip() != body_to_enhance.strip()
    }
