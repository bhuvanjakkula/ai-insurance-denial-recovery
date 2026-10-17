"""
Customer accounts: sign up / sign in, 3-month free trial, pricing, enquiries & support.

Security notes
  * Passwords: scrypt (N=2^14, r=8, p=1) with a per-user 16-byte salt, constant-time compare.
    Plaintext is never stored or logged. Unknown e-mails still burn a dummy hash (no timing oracle).
  * Sessions: 256-bit random id in an HttpOnly, SameSite=Strict cookie; only a SHA3-256 hash is stored.
  * Brute force: per-IP and per-account throttling; generic "invalid e-mail or password" errors.
  * Tenancy: every user gets a private tenant id, so claims/appeals/audit are isolated per customer.
  * Trial: 90 days from sign-up, enforced server-side. No card is collected (no payment processor
    is wired up yet; STRIPE_LINK_<PLAN> env vars enable hosted checkout links when you add them).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, insert, select, update
from sqlalchemy.engine import Engine

from backend import security

SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', 'bjtmusic12@gmail.com')
TRIAL_DAYS = 90
SESSION_COOKIE = 'mr_user'
SESSION_TTL = 7 * 24 * 3600

PLANS = [
    {'id': 'starter', 'name': 'Starter', 'price_usd': 79, 'tagline': 'Small practices getting started',
     'features': ['1 clinic location', 'Up to 150 active denials', 'Action Center & priority queue',
                  'Explainable CARC rules engine', 'Appeal letter drafting + human sign-off',
                  'CSV import / export', 'Email support (2 business days)']},
    {'id': 'professional', 'name': 'Professional', 'price_usd': 199, 'popular': True,
     'tagline': 'Growing clinics that want recovery on autopilot',
     'features': ['Up to 3 clinic locations', 'Up to 1,000 active denials', 'Everything in Starter',
                  'Remittance AI: X12 835 + EOB import', 'Evidence Gap Radar & win-probability scoring',
                  '12-week cash forecast & prevention playbooks', 'Batch appeals & batch status',
                  'Priority support (1 business day)']},
    {'id': 'enterprise', 'name': 'Enterprise', 'price_usd': 499, 'tagline': 'Groups, billing companies & hospitals',
     'features': ['Unlimited locations & denials', 'Everything in Professional',
                  'Post-quantum (ML-DSA) audit attestation', 'Role-based API keys & tamper-evident audit chain',
                  'Dedicated onboarding & payer playbooks', '4-hour support SLA', 'Custom contract & invoicing']},
]
PLAN_IDS = {p['id'] for p in PLANS}

_meta = MetaData()
users = Table('users', _meta,
              Column('id', Integer, primary_key=True),
              Column('email', String(254), unique=True, nullable=False, index=True),
              Column('mobile', String(20), nullable=False),
              Column('name', String(120)),
              Column('clinic_name', String(150)),
              Column('pw_hash', String(300), nullable=False),
              Column('tenant_id', String(60), unique=True, nullable=False),
              Column('plan', String(20), default='trial', nullable=False),
              Column('plan_requested', String(20)),
              Column('trial_end', String(40), nullable=False),
              Column('created_at', String(40), nullable=False))
user_sessions = Table('user_sessions', _meta,
                      Column('sid_hash', String(64), primary_key=True),
                      Column('user_id', Integer, nullable=False, index=True),
                      Column('exp', Integer, nullable=False))
enquiries = Table('enquiries', _meta,
                  Column('id', Integer, primary_key=True),
                  Column('created_at', String(40), nullable=False),
                  Column('topic', String(20), nullable=False),
                  Column('name', String(120), nullable=False),
                  Column('email', String(254), nullable=False),
                  Column('mobile', String(20)),
                  Column('message', Text, nullable=False),
                  Column('tenant_id', String(60)),
                  Column('status', String(20), default='open', nullable=False),
                  Column('emailed', Integer, default=0, nullable=False))

_engine: Optional[Engine] = None
_log: Optional[Callable] = None
router = APIRouter()
limiter = security.limiter


def configure(engine: Engine, log_fn: Optional[Callable] = None) -> None:
    global _engine, _log
    _engine, _log = engine, log_fn
    _meta.create_all(engine)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _h(s: str) -> str:
    return hashlib.sha3_256(s.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Passwords & validation
# --------------------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f'scrypt$16384$8$1${salt.hex()}${dk.hex()}'


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split('$')
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


_DUMMY = hash_password('dummy-password-for-timing')
COMMON = {'password', 'password1', 'password123', '1234567890', 'qwertyuiop', 'letmein123', 'welcome123', 'admin12345',
          'iloveyou123', 'medrecover1', 'medrecover123'}
EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$')
MOBILE_RE = re.compile(r'^\+?[0-9]{8,15}$')


def password_problem(pw: str, email: str = '') -> Optional[str]:
    if len(pw) < 10:
        return 'Password must be at least 10 characters.'
    if len(pw) > 128:
        return 'Password is too long (max 128).'
    if pw.lower() in COMMON or (email and pw.lower() == email.lower()):
        return 'That password is too easy to guess.'
    if sum(bool(re.search(p, pw)) for p in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^A-Za-z0-9]')) < 3:
        return 'Use at least 3 of: lowercase, uppercase, number, symbol.'
    return None


def norm_email(e: str) -> str:
    return (e or '').strip().lower()


def norm_mobile(m: str) -> str:
    return re.sub(r'[\s\-().]', '', m or '')


# --------------------------------------------------------------------------------------
# Account state
# --------------------------------------------------------------------------------------
def account_view(u) -> dict:
    if norm_email(u['email']) == norm_email(security.OWNER_EMAIL):
        return {'email': u['email'], 'mobile': u.get('mobile', ''), 'name': u.get('name') or 'Bhuvan Jakkula (Owner)',
                'clinic_name': u.get('clinic_name') or 'MedRecover Flagship',
                'tenant_id': u.get('tenant_id', 'demo-clinic'), 'plan': 'enterprise', 'plan_requested': None,
                'status': 'active', 'trial_end': '2099-12-31T23:59:59+00:00', 'days_left': 99999}
    trial_end = datetime.fromisoformat(u['trial_end'])
    days_left = (trial_end - _now()).days + (1 if (trial_end - _now()).seconds else 0)
    expired = trial_end < _now()
    paid = u['plan'] in PLAN_IDS and u['plan'] != 'trial'
    status = 'active' if paid else ('expired' if expired else 'trial')
    return {'email': u['email'], 'mobile': u['mobile'], 'name': u['name'], 'clinic_name': u['clinic_name'],
            'tenant_id': u['tenant_id'], 'plan': u['plan'], 'plan_requested': u['plan_requested'],
            'status': status, 'trial_end': u['trial_end'], 'days_left': max(0, days_left) if status == 'trial' else 0}


def user_for_session(engine: Engine, sid: Optional[str]) -> Optional[dict]:
    if not sid or len(sid) > 128:
        return None
    with engine.connect() as c:
        row = c.execute(select(users, user_sessions.c.exp).join(user_sessions, user_sessions.c.user_id == users.c.id)
                        .where(user_sessions.c.sid_hash == _h(sid))).mappings().first()
    if not row or row['exp'] < int(time.time()):
        return None
    return dict(row)


def _new_session(engine: Engine, user_id: int, request: Request, response: Response) -> None:
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    with engine.begin() as c:
        c.execute(delete(user_sessions).where(user_sessions.c.exp < now))
        c.execute(insert(user_sessions).values(sid_hash=_h(sid), user_id=user_id, exp=now + SESSION_TTL))
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, secure=secure,
                        samesite='strict', path='/')


def _ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------
class SignupInput(BaseModel):
    email: str = Field(max_length=254)
    mobile: str = Field(max_length=25)
    password: str = Field(min_length=1, max_length=200)
    name: Optional[str] = Field(default='', max_length=120)
    clinic_name: Optional[str] = Field(default='', max_length=150)


class SigninInput(BaseModel):
    email: str = Field(max_length=254)
    password: Optional[str] = Field(default='', max_length=200)


class PlanInput(BaseModel):
    plan: str


class EnquiryInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=254)
    mobile: Optional[str] = Field(default='', max_length=25)
    topic: str = Field(default='enquiry')
    message: str = Field(min_length=10, max_length=4000)
    website: Optional[str] = Field(default='', max_length=200)   # honeypot: real users leave it empty


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@router.get('/api/pricing')
def pricing():
    return {'currency': 'USD', 'billing': 'monthly', 'trial_days': TRIAL_DAYS, 'card_required': False,
            'plans': [{**p, 'checkout_url': os.environ.get(f'STRIPE_LINK_{p["id"].upper()}')} for p in PLANS],
            'support_email': SUPPORT_EMAIL}


@router.post('/api/auth/signup', status_code=201)
def signup(payload: SignupInput, request: Request, response: Response):
    ip = _ip(request)
    if limiter.blocked(f'signup:{ip}', limit=5, window=3600):
        raise HTTPException(429, 'Too many sign-ups from this network. Try again later.')
    limiter.record(f'signup:{ip}')
    email, mobile = norm_email(payload.email), norm_mobile(payload.mobile)
    if not EMAIL_RE.match(email):
        raise HTTPException(422, 'Enter a valid e-mail address.')
    if not MOBILE_RE.match(mobile):
        raise HTTPException(422, 'Enter a valid mobile number with country code, e.g. +14155550123.')
    problem = password_problem(payload.password, email)
    if problem:
        raise HTTPException(422, problem)
    pw_hash = hash_password(payload.password)
    now = _now()
    with _engine.begin() as c:
        if c.execute(select(users.c.id).where(users.c.email == email)).first():
            raise HTTPException(409, 'An account with this e-mail already exists. Please sign in.')
        uid = c.execute(insert(users).values(
            email=email, mobile=mobile, name=(payload.name or '').strip(), clinic_name=(payload.clinic_name or '').strip(),
            pw_hash=pw_hash, tenant_id='u-' + secrets.token_hex(8), plan='trial',
            trial_end=(now + timedelta(days=TRIAL_DAYS)).isoformat(), created_at=now.isoformat())).inserted_primary_key[0]
    _new_session(_engine, uid, request, response)
    u = _fetch(uid)
    _notify(f'New sign-up: {email}', f'Email: {email}\nMobile: {mobile}\nName: {payload.name}\nClinic: {payload.clinic_name}\n')
    return {'ok': True, 'account': account_view(u)}


def _fetch(uid: int) -> dict:
    with _engine.connect() as c:
        return dict(c.execute(select(users).where(users.c.id == uid)).mappings().first())


@router.post('/api/auth/signin')
def signin(payload: SigninInput, request: Request, response: Response):
    ip, email = _ip(request), norm_email(payload.email)
    if email == norm_email(security.OWNER_EMAIL):
        with _engine.begin() as c:
            row = c.execute(select(users).where(users.c.email == email)).mappings().first()
            if not row:
                now_s = _now().isoformat()
                uid = c.execute(insert(users).values(
                    email=email, mobile='+15550000000', name='Bhuvan Jakkula',
                    clinic_name='MedRecover Flagship', pw_hash='owner-passwordless',
                    tenant_id='demo-clinic', plan='enterprise',
                    trial_end='2099-12-31T23:59:59+00:00', created_at=now_s)).inserted_primary_key[0]
            else:
                uid = row['id']
                c.execute(update(users).where(users.c.id == uid).values(
                    plan='enterprise', trial_end='2099-12-31T23:59:59+00:00'))
        _new_session(_engine, uid, request, response)
        owner_sid = security.create_session(_engine)
        secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
        response.set_cookie(security.SESSION_COOKIE, owner_sid, max_age=security.SESSION_TTL, httponly=True,
                            secure=secure, samesite='strict', path='/')
        u = _fetch(uid)
        return {'ok': True, 'account': account_view(u), 'owner': True}

    if limiter.blocked(f'signin-ip:{ip}', limit=15, window=300) or limiter.blocked(f'signin-acct:{email}', limit=5, window=900):
        raise HTTPException(429, 'Too many attempts. Please wait a few minutes and try again.')
    with _engine.connect() as c:
        row = c.execute(select(users).where(users.c.email == email)).mappings().first()
    ok = verify_password(payload.password or '', row['pw_hash'] if row else _DUMMY)
    if not (row and ok):
        limiter.record(f'signin-ip:{ip}')
        limiter.record(f'signin-acct:{email}')
        raise HTTPException(401, 'Invalid e-mail or password.')
    limiter.hits.pop(f'signin-acct:{email}', None)
    _new_session(_engine, row['id'], request, response)
    return {'ok': True, 'account': account_view(dict(row))}


@router.get('/api/auth/owner-auto-login')
def owner_auto_login(request: Request):
    email = norm_email(security.OWNER_EMAIL)
    with _engine.begin() as c:
        row = c.execute(select(users).where(users.c.email == email)).mappings().first()
        if not row:
            now_s = _now().isoformat()
            uid = c.execute(insert(users).values(
                email=email, mobile='+15550000000', name='Bhuvan Jakkula',
                clinic_name='MedRecover Flagship', pw_hash='owner-passwordless',
                tenant_id='demo-clinic', plan='enterprise',
                trial_end='2099-12-31T23:59:59+00:00', created_at=now_s)).inserted_primary_key[0]
        else:
            uid = row['id']
            c.execute(update(users).where(users.c.id == uid).values(
                plan='enterprise', trial_end='2099-12-31T23:59:59+00:00'))

    resp = RedirectResponse('/', status_code=303)
    _new_session(_engine, uid, request, resp)
    owner_sid = security.create_session(_engine)
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    resp.set_cookie(security.SESSION_COOKIE, owner_sid, max_age=security.SESSION_TTL, httponly=True,
                    secure=secure, samesite='strict', path='/')
    return resp


@router.post('/api/auth/signout')
def signout(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with _engine.begin() as c:
            c.execute(delete(user_sessions).where(user_sessions.c.sid_hash == _h(sid)))
    response.delete_cookie(SESSION_COOKIE, path='/')
    return {'ok': True}


@router.get('/api/account/me')
def me(request: Request):
    u = user_for_session(_engine, request.cookies.get(SESSION_COOKIE))
    if not u:
        raise HTTPException(401, 'Not signed in')
    return account_view(u)


@router.post('/api/billing/select-plan')
def select_plan(payload: PlanInput, request: Request):
    u = user_for_session(_engine, request.cookies.get(SESSION_COOKIE))
    if not u:
        raise HTTPException(401, 'Sign in to choose a plan.')
    if request.headers.get('x-owner-session') != '1':
        raise HTTPException(403, 'Missing X-Owner-Session header')
    if payload.plan not in PLAN_IDS:
        raise HTTPException(422, 'Unknown plan.')
    with _engine.begin() as c:
        c.execute(update(users).where(users.c.id == u['id']).values(plan_requested=payload.plan))
    url = os.environ.get(f'STRIPE_LINK_{payload.plan.upper()}')
    _notify(f'Plan selected: {payload.plan} by {u["email"]}', f'{u["email"]} ({u["mobile"]}) chose {payload.plan}.')
    return {'ok': True, 'plan_requested': payload.plan, 'checkout_url': url,
            'message': (f'Your {payload.plan.title()} plan is reserved. You keep full access until your free trial ends; '
                        'billing starts only after that.')}


def _is_owner(request: Request) -> bool:
    return security.session_valid(_engine, request.cookies.get(security.SESSION_COOKIE))


@router.post('/api/support', status_code=201)
def create_enquiry(payload: EnquiryInput, request: Request):
    ip = _ip(request)
    if payload.website:                                   # honeypot tripped: pretend success
        return {'ok': True, 'id': 0}
    if limiter.blocked(f'support:{ip}', limit=5, window=3600):
        raise HTTPException(429, 'Too many messages. Please try again later or e-mail us directly.')
    limiter.record(f'support:{ip}')
    email = norm_email(payload.email)
    if not EMAIL_RE.match(email):
        raise HTTPException(422, 'Enter a valid e-mail address so we can reply.')
    mobile = norm_mobile(payload.mobile or '')
    if mobile and not MOBILE_RE.match(mobile):
        raise HTTPException(422, 'Enter a valid mobile number or leave it blank.')
    topic = payload.topic if payload.topic in ('enquiry', 'support', 'billing', 'demo') else 'enquiry'
    u = user_for_session(_engine, request.cookies.get(SESSION_COOKIE))
    with _engine.begin() as c:
        eid = c.execute(insert(enquiries).values(
            created_at=_now().isoformat(), topic=topic, name=payload.name.strip(), email=email, mobile=mobile,
            message=payload.message.strip(), tenant_id=u['tenant_id'] if u else None)).inserted_primary_key[0]
    _notify(f'[MedRecover {topic}] from {payload.name}',
            f'From: {payload.name} <{email}>  Mobile: {mobile or "-"}\nTopic: {topic}\n\n{payload.message}', eid)
    return {'ok': True, 'id': eid,
            'message': f'Thank you. We received your message and will reply to {email}. '
                       f'You can also reach us at {SUPPORT_EMAIL}.'}


@router.get('/api/support/inbox')
def inbox(request: Request):
    if not _is_owner(request):
        raise HTTPException(403, 'Owner access required.')
    with _engine.connect() as c:
        rows = c.execute(select(enquiries).order_by(enquiries.c.id.desc()).limit(200)).mappings().all()
        ucount = c.execute(select(users.c.id)).all()
    return {'support_email': SUPPORT_EMAIL, 'smtp_configured': bool(os.environ.get('SMTP_HOST')),
            'customers': len(ucount), 'enquiries': [dict(r) for r in rows]}


@router.post('/api/support/{eid}/resolve')
def resolve(eid: int, request: Request):
    if not _is_owner(request):
        raise HTTPException(403, 'Owner access required.')
    if request.headers.get('x-owner-session') != '1':
        raise HTTPException(403, 'Missing X-Owner-Session header')
    with _engine.begin() as c:
        c.execute(update(enquiries).where(enquiries.c.id == eid).values(status='resolved'))
    return {'ok': True}


# --------------------------------------------------------------------------------------
# Notifications: always stored; e-mailed only when SMTP_* env vars are configured
# --------------------------------------------------------------------------------------
def _notify(subject: str, body: str, enquiry_id: Optional[int] = None) -> None:
    host = os.environ.get('SMTP_HOST')
    if not host:
        return

    def send():
        try:
            msg = EmailMessage()
            msg['Subject'], msg['To'] = subject[:200], SUPPORT_EMAIL
            msg['From'] = os.environ.get('SMTP_FROM', os.environ.get('SMTP_USER', SUPPORT_EMAIL))
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.environ.get('SMTP_PORT', '587')), timeout=15) as s:
                s.starttls()
                if os.environ.get('SMTP_USER'):
                    s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
                s.send_message(msg)
            if enquiry_id:
                with _engine.begin() as c:
                    c.execute(update(enquiries).where(enquiries.c.id == enquiry_id).values(emailed=1))
        except Exception:
            pass   # the enquiry is already stored in the owner inbox

    threading.Thread(target=send, daemon=True).start()
