"""
Security layer: post-quantum owner access, session handling, rate limiting, attestation.

Algorithms
  * ML-DSA-65 (FIPS 204 / CRYSTALS-Dilithium) signs owner login tokens and audit attestations.
  * SHA3-256 hashes session identifiers at rest (the raw session id exists only in the cookie).

Trust model
  * The owner's PRIVATE key never lives on the server. It is generated and kept locally by
    tools/owner_access.py. The server only holds the PUBLIC key (OWNER_PUBKEY_B64 env var or
    <keydir>/owner_pk.b64), so a server/database leak cannot be used to log in as the owner.
  * An owner login token is: short-lived (<= 5 min), audience-bound, email-bound, single-use
    (jti is recorded and refused on replay) and signed. No password exists to phish or brute force.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, Optional

from dilithium_py.ml_dsa import ML_DSA_65 as DSA
from sqlalchemy import Column, Integer, MetaData, String, Table, select, delete, insert
from sqlalchemy.engine import Engine

OWNER_EMAIL = 'bhuvanjakkula@gmail.com'
TOKEN_AUDIENCE = 'medrecover-owner-login'
TOKEN_MAX_TTL = 300            # seconds
SESSION_TTL = 8 * 3600         # seconds
SESSION_COOKIE = 'mr_owner'
ALGORITHM = 'ML-DSA-65'


def keydir() -> Path:
    if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        return Path(os.environ.get('MEDRECOVER_KEYDIR') or '/tmp/.medrecover')
    return Path(os.environ.get('MEDRECOVER_KEYDIR') or (Path.home() / '.medrecover'))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


# --------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------
def generate_keypair(name: str, overwrite: bool = False) -> Path:
    """Create <keydir>/<name>_pk.b64 (public) and <name>_sk.b64 (private, owner-only perms)."""
    d = keydir()
    d.mkdir(parents=True, exist_ok=True)
    pk_path, sk_path = d / f'{name}_pk.b64', d / f'{name}_sk.b64'
    if sk_path.exists() and not overwrite:
        raise FileExistsError(f'{sk_path} already exists (use overwrite to rotate)')
    pk, sk = DSA.keygen()
    pk_path.write_text(b64e(pk))
    sk_path.write_text(b64e(sk))
    try:
        os.chmod(sk_path, 0o600)
    except OSError:
        pass
    return d


def load_public_key(name: str, env_var: Optional[str] = None) -> Optional[bytes]:
    raw = os.environ.get(env_var, '') if env_var else ''
    if raw:
        return b64d(raw.strip())
    p = keydir() / f'{name}_pk.b64'
    return b64d(p.read_text().strip()) if p.exists() else None


def load_secret_key(name: str) -> bytes:
    p = keydir() / f'{name}_sk.b64'
    if not p.exists():
        raise FileNotFoundError(f'{p} not found')
    return b64d(p.read_text().strip())


# --------------------------------------------------------------------------------------
# Signed owner token
# --------------------------------------------------------------------------------------
def mint_owner_token(ttl: int = 120, email: str = OWNER_EMAIL) -> str:
    """Run on the owner's machine. Returns '<payload>.<signature>' (both base64url)."""
    ttl = max(10, min(ttl, TOKEN_MAX_TTL))
    now = int(time.time())
    payload = json.dumps(
        {'aud': TOKEN_AUDIENCE, 'sub': email, 'iat': now, 'exp': now + ttl, 'jti': secrets.token_urlsafe(16)},
        separators=(',', ':'), sort_keys=True).encode()
    sig = DSA.sign(load_secret_key('owner'), payload)
    return f'{b64e(payload)}.{b64e(sig)}'


class TokenError(Exception):
    pass


def verify_owner_token(token: str, pubkey: Optional[bytes] = None, now: Optional[int] = None) -> dict:
    """Verify signature + claims. Raises TokenError (generic message; never reveals which check)."""
    generic = TokenError('Invalid or expired owner token')
    pubkey = pubkey if pubkey is not None else load_public_key('owner', 'OWNER_PUBKEY_B64')
    if not pubkey or not token or len(token) > 8192 or token.count('.') != 1:
        raise generic
    try:
        p_b64, s_b64 = token.split('.')
        payload, sig = b64d(p_b64), b64d(s_b64)
        if not DSA.verify(pubkey, payload, sig):
            raise generic
        claims = json.loads(payload)
    except TokenError:
        raise
    except Exception:
        raise generic
    t = int(now if now is not None else time.time())
    if (claims.get('aud') != TOKEN_AUDIENCE
            or str(claims.get('sub', '')).lower() != OWNER_EMAIL
            or not isinstance(claims.get('exp'), int) or not isinstance(claims.get('iat'), int)
            or claims['exp'] < t or claims['iat'] > t + 30
            or claims['exp'] - claims['iat'] > TOKEN_MAX_TTL
            or not claims.get('jti')):
        raise generic
    return claims


# --------------------------------------------------------------------------------------
# Replay + session storage
# --------------------------------------------------------------------------------------
_meta = MetaData()
used_tokens = Table('owner_used_tokens', _meta,
                    Column('jti', String(64), primary_key=True), Column('exp', Integer, nullable=False))
owner_sessions = Table('owner_sessions', _meta,
                       Column('sid_hash', String(64), primary_key=True), Column('exp', Integer, nullable=False))


def init_store(engine: Engine) -> None:
    _meta.create_all(engine)


def _h(s: str) -> str:
    return hashlib.sha3_256(s.encode()).hexdigest()


def consume_jti(engine: Engine, claims: dict) -> bool:
    """True the first time a token id is seen; False on replay."""
    now = int(time.time())
    with engine.begin() as c:
        c.execute(delete(used_tokens).where(used_tokens.c.exp < now))
        if c.execute(select(used_tokens.c.jti).where(used_tokens.c.jti == claims['jti'])).first():
            return False
        c.execute(insert(used_tokens).values(jti=claims['jti'], exp=claims['exp'] + 60))
    return True


def create_session(engine: Engine) -> str:
    sid = secrets.token_urlsafe(32)  # 256-bit
    now = int(time.time())
    with engine.begin() as c:
        c.execute(delete(owner_sessions).where(owner_sessions.c.exp < now))
        c.execute(insert(owner_sessions).values(sid_hash=_h(sid), exp=now + SESSION_TTL))
    return sid


def session_valid(engine: Engine, sid: Optional[str]) -> bool:
    if not sid or len(sid) > 128:
        return False
    with engine.connect() as c:
        row = c.execute(select(owner_sessions.c.exp).where(owner_sessions.c.sid_hash == _h(sid))).first()
    return bool(row and row[0] >= int(time.time()))


def destroy_session(engine: Engine, sid: Optional[str]) -> None:
    if sid:
        with engine.begin() as c:
            c.execute(delete(owner_sessions).where(owner_sessions.c.sid_hash == _h(sid)))


# --------------------------------------------------------------------------------------
# Rate limiting (sliding window, in-memory, per client key)
# --------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self) -> None:
        self.hits: Dict[str, Deque[float]] = defaultdict(deque)

    def blocked(self, key: str, limit: int, window: int) -> bool:
        q, now = self.hits[key], time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    def record(self, key: str, window: int = 60) -> None:
        self.hits[key].append(time.monotonic())


limiter = RateLimiter()


# --------------------------------------------------------------------------------------
# Post-quantum attestation of the audit chain
# --------------------------------------------------------------------------------------
def attestation_keys() -> tuple[bytes, bytes]:
    """Server attestation keypair; auto-created on first use in the keydir."""
    d = keydir()
    if not (d / 'attest_sk.b64').exists():
        generate_keypair('attest')
    return b64d((d / 'attest_pk.b64').read_text().strip()), load_secret_key('attest')


def attest(tenant: str, head_hash: str, count: int, ts: str) -> dict:
    pk, sk = attestation_keys()
    msg = json.dumps({'tenant': tenant, 'head': head_hash, 'events': count, 'ts': ts},
                     separators=(',', ':'), sort_keys=True).encode()
    return {'algorithm': ALGORITHM, 'message': msg.decode(), 'signature': b64e(DSA.sign(sk, msg)),
            'public_key': b64e(pk), 'public_key_sha3_256': hashlib.sha3_256(pk).hexdigest()}


def verify_attestation(att: dict) -> bool:
    try:
        return bool(DSA.verify(b64d(att['public_key']), att['message'].encode(), b64d(att['signature'])))
    except Exception:
        return False
