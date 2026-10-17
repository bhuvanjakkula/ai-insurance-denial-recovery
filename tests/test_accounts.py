import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('DEMO_API_KEY', 'acct-key')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import update  # noqa: E402
from backend import accounts, security  # noqa: E402
from backend.main import app, engine  # noqa: E402

PW = 'Str0ng!Passw0rd'
GUARD = {'X-Owner-Session': '1'}
_n = {'i': 0}


@pytest.fixture(autouse=True)
def _reset():
    security.limiter.hits.clear()


def new_client():
    return TestClient(app)


def signup(c, email=None, **kw):
    _n['i'] += 1
    body = {'email': email or f'user{_n["i"]}@example.com', 'mobile': '+14155550123', 'password': PW,
            'name': 'Dr Test', 'clinic_name': 'Test Clinic'}
    body.update(kw)
    return c.post('/api/auth/signup', json=body)


def test_pricing_is_public_usd_and_three_plans():
    p = new_client().get('/api/pricing').json()
    assert p['currency'] == 'USD' and p['trial_days'] == 90 and p['card_required'] is False
    assert [x['id'] for x in p['plans']] == ['starter', 'professional', 'enterprise']
    assert [x['price_usd'] for x in p['plans']] == [79, 199, 499]
    assert p['support_email'] == 'bjtmusic12@gmail.com'


def test_signup_creates_trial_and_session():
    c = new_client()
    r = signup(c)
    assert r.status_code == 201
    acct = r.json()['account']
    assert acct['status'] == 'trial' and 89 <= acct['days_left'] <= 90
    ck = r.headers['set-cookie'].lower()
    assert 'httponly' in ck and 'samesite=strict' in ck
    assert 'pw_hash' not in r.text and PW not in r.text
    assert c.get('/api/account/me').json()['email'] == acct['email']


@pytest.mark.parametrize('bad,msg', [
    ({'email': 'not-an-email'}, 'e-mail'),
    ({'mobile': '123'}, 'mobile'),
    ({'password': 'short1A!'}, '10 characters'),
    ({'password': 'alllowercaseletters'}, 'at least 3'),
    ({'password': 'password123'}, 'too easy'),
])
def test_signup_validation(bad, msg):
    r = signup(new_client(), **bad)
    assert r.status_code == 422 and msg in r.json()['detail']


def test_duplicate_email_rejected_case_insensitive():
    c = new_client()
    signup(c, email='dup@example.com')
    assert signup(new_client(), email='DUP@Example.com').status_code == 409


def test_password_stored_as_scrypt_hash():
    signup(new_client(), email='hash@example.com')
    from sqlalchemy import select
    with engine.connect() as conn:
        row = conn.execute(select(accounts.users).where(accounts.users.c.email == 'hash@example.com')).mappings().first()
    assert row['pw_hash'].startswith('scrypt$') and PW not in row['pw_hash']


def test_signin_signout_and_generic_errors():
    signup(new_client(), email='login@example.com')
    c = new_client()
    assert c.post('/api/auth/signin', json={'email': 'login@example.com', 'password': 'wrong-password'}).status_code == 401
    unknown = c.post('/api/auth/signin', json={'email': 'ghost@example.com', 'password': PW})
    assert unknown.status_code == 401 and unknown.json()['detail'] == 'Invalid e-mail or password.'
    ok = c.post('/api/auth/signin', json={'email': 'LOGIN@example.com', 'password': PW})
    assert ok.status_code == 200
    assert c.get('/api/account/me').status_code == 200
    c.post('/api/auth/signout')
    c.cookies.clear()
    assert c.get('/api/account/me').status_code == 401


def test_account_lockout_after_failures():
    signup(new_client(), email='lock@example.com')
    c = new_client()
    for _ in range(5):
        assert c.post('/api/auth/signin', json={'email': 'lock@example.com', 'password': 'bad-password-1'}).status_code == 401
    assert c.post('/api/auth/signin', json={'email': 'lock@example.com', 'password': PW}).status_code == 429


def test_each_customer_has_isolated_data():
    a, b = new_client(), new_client()
    signup(a)
    signup(b)
    assert a.post('/api/claims/seed-demo', headers=GUARD).status_code in (200, 201)
    assert len(a.get('/api/claims').json()) >= 10
    assert b.get('/api/claims').json() == []


def test_writes_need_csrf_header():
    c = new_client()
    signup(c)
    assert c.post('/api/claims/seed-demo').status_code == 403


def test_expired_trial_blocks_app_but_not_pricing_or_support():
    c = new_client()
    r = signup(c, email='expired@example.com')
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with engine.begin() as conn:
        conn.execute(update(accounts.users).where(accounts.users.c.email == 'expired@example.com').values(trial_end=past))
    assert c.get('/api/claims').status_code == 402
    assert c.get('/api/account/me').json()['status'] == 'expired'
    assert c.get('/api/pricing').status_code == 200
    assert c.post('/api/support', json={'name': 'Exp', 'email': 'expired@example.com',
                                        'message': 'Please help me upgrade my plan'}).status_code == 201


def test_select_plan_reserves_without_charging():
    c = new_client()
    signup(c)
    assert c.post('/api/billing/select-plan', json={'plan': 'professional'}).status_code == 403   # no CSRF header
    r = c.post('/api/billing/select-plan', json={'plan': 'professional'}, headers=GUARD)
    assert r.status_code == 200 and r.json()['plan_requested'] == 'professional'
    assert c.post('/api/billing/select-plan', json={'plan': 'free-forever'}, headers=GUARD).status_code == 422
    assert c.get('/api/account/me').json()['status'] == 'trial'      # still on trial: nothing was charged


def test_enquiry_flow_inbox_is_owner_only():
    anon = new_client()
    r = anon.post('/api/support', json={'name': 'Pat', 'email': 'pat@example.com', 'topic': 'demo',
                                        'message': 'I would like a demo for my three clinics.'})
    assert r.status_code == 201 and 'bjtmusic12@gmail.com' in r.json()['message']
    assert anon.get('/api/support/inbox').status_code == 403
    cust = new_client()
    signup(cust)
    assert cust.get('/api/support/inbox').status_code == 403            # customers cannot read others' enquiries

    import tempfile as tf
    from pathlib import Path
    os.environ['MEDRECOVER_KEYDIR'] = tf.mkdtemp()
    os.environ.pop('OWNER_PUBKEY_B64', None)
    security.generate_keypair('owner')
    owner = new_client()
    assert owner.post('/api/owner/login', json={'token': security.mint_owner_token()}).status_code == 200
    box = owner.get('/api/support/inbox').json()
    assert any(e['email'] == 'pat@example.com' for e in box['enquiries'])
    eid = box['enquiries'][0]['id']
    assert owner.post(f'/api/support/{eid}/resolve', headers=GUARD).status_code == 200


def test_enquiry_validation_honeypot_and_rate_limit():
    c = new_client()
    assert c.post('/api/support', json={'name': 'X', 'email': 'bad', 'message': 'long enough message here'}).status_code == 422
    bot = c.post('/api/support', json={'name': 'Bot', 'email': 'b@example.com',
                                       'message': 'buy cheap stuff now please', 'website': 'http://spam'})
    assert bot.status_code == 201 and bot.json()['id'] == 0               # silently dropped
    ok = {'name': 'Rate', 'email': 'rate@example.com', 'message': 'testing the rate limiter okay'}
    codes = [c.post('/api/support', json=ok).status_code for _ in range(7)]
    assert 429 in codes


def test_pages_served():
    c = new_client()
    for p in ('/signin', '/signup', '/pricing', '/auth.js', '/pricing.js'):
        assert c.get(p).status_code == 200, p
