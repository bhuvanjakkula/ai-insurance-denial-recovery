import base64
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('DEMO_API_KEY', 'sec-key')

from fastapi.testclient import TestClient  # noqa: E402
from backend import security  # noqa: E402
from backend.main import app  # noqa: E402

KEY = os.environ['DEMO_API_KEY']


@pytest.fixture()
def owner(monkeypatch):
    tmp_path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('MEDRECOVER_KEYDIR', str(tmp_path))
    monkeypatch.delenv('OWNER_PUBKEY_B64', raising=False)
    security.generate_keypair('owner')
    security.limiter.hits.clear()
    return TestClient(app)


def login(c, token):
    return c.post('/api/owner/login', json={'token': token})


def test_owner_login_is_passwordless_and_grants_access(owner):
    assert owner.get('/api/claims').status_code == 401
    r = login(owner, security.mint_owner_token())
    assert r.status_code == 200 and r.json()['algorithm'] == 'ML-DSA-65'
    c = r.headers['set-cookie'].lower()
    assert 'httponly' in c and 'samesite=strict' in c
    assert owner.get('/api/claims').status_code == 200          # reads: cookie only
    assert owner.get('/api/owner/session').json() == {'owner': True, 'email': security.OWNER_EMAIL}


def test_csrf_header_required_for_owner_writes(owner):
    login(owner, security.mint_owner_token())
    assert owner.post('/api/claims/seed-demo').status_code == 403
    assert owner.post('/api/claims/seed-demo', headers={'X-Owner-Session': '1'}).status_code in (200, 201)


def test_token_replay_is_refused(owner):
    t = security.mint_owner_token()
    assert login(owner, t).status_code == 200
    assert login(owner, t).status_code == 401


def test_tampered_token_refused(owner):
    p, s = security.mint_owner_token().split('.')
    claims = json.loads(security.b64d(p))
    claims['exp'] += 100000
    forged = security.b64e(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode()) + '.' + s
    assert login(owner, forged).status_code == 401


def test_signature_from_other_key_refused(owner, monkeypatch):
    real_dir = os.environ['MEDRECOVER_KEYDIR']
    other = Path(tempfile.mkdtemp())
    monkeypatch.setenv('MEDRECOVER_KEYDIR', str(other))
    security.generate_keypair('owner')
    attacker_token = security.mint_owner_token()
    monkeypatch.setenv('MEDRECOVER_KEYDIR', str(real_dir))      # server trusts the real owner key
    assert login(owner, attacker_token).status_code == 401


def test_expired_and_wrong_identity_refused(owner):
    pk = security.load_public_key('owner')
    tok = security.mint_owner_token()
    with pytest.raises(security.TokenError):
        security.verify_owner_token(tok, pk, now=int(time.time()) + 4000)
    with pytest.raises(security.TokenError):
        security.verify_owner_token(security.mint_owner_token(email='someone@else.com'), pk)


def test_logout_kills_session(owner):
    login(owner, security.mint_owner_token())
    owner.post('/api/owner/logout')
    owner.cookies.clear()
    assert owner.get('/api/claims').status_code == 401


def test_login_rate_limited(owner):
    for _ in range(5):
        assert login(owner, 'x' * 40).status_code == 401
    assert login(owner, 'x' * 40).status_code == 429


def test_api_key_failures_rate_limited(owner):
    codes = [owner.get('/api/claims', headers={'X-API-Key': 'wrong'}).status_code for _ in range(22)]
    assert 401 in codes and codes[-1] == 429


def test_api_key_still_works(owner):
    assert owner.get('/api/claims', headers={'X-API-Key': KEY}).status_code == 200


def test_security_headers_present(owner):
    h = owner.get('/').headers
    assert "default-src 'self'" in h['content-security-policy']
    assert h['x-frame-options'] == 'DENY' and h['x-content-type-options'] == 'nosniff'
    assert owner.get('/api/claims', headers={'X-API-Key': KEY}).headers['cache-control'] == 'no-store'


def test_pqc_attestation_verifies_and_detects_tamper(owner):
    a = owner.get('/api/security/attestation', headers={'X-API-Key': KEY}).json()
    assert a['algorithm'] == 'ML-DSA-65' and a['self_check'] is True
    assert security.verify_attestation(a)
    a['message'] = a['message'].replace('"events":', '"events":9')
    assert security.verify_attestation(a) is False


def test_no_private_key_material_on_server_db(owner):
    login(owner, security.mint_owner_token())
    from backend.main import engine
    from sqlalchemy import text
    with engine.connect() as c:
        rows = c.execute(text('select sid_hash from owner_sessions')).fetchall()
    assert rows and all(len(r[0]) == 64 for r in rows)          # only SHA3-256 hashes stored


