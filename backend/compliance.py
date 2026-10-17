"""
Compliance guardrails: PHI redaction, role-based access control, tamper-evident audit chain.

* `redact_phi` — heuristic redaction of common direct identifiers. It cannot reliably detect
  free-text names; treat it as a safety net that complements de-identification, not a
  replacement for it.
* RBAC — API keys map to roles; each role holds an explicit permission set. Approval of an
  appeal is deliberately withheld from billers (segregation of duties).
* Audit chain — each audit row stores SHA-256(prev_hash | tenant | action | entity | ts | actor | role).
  Editing, deleting or reordering any row breaks verification from that row onward. Publish the
  head hash externally (or to WORM storage) to also detect truncation of the newest rows.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------------------
# PHI redaction
# --------------------------------------------------------------------------------------
_DATE = r'(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})'

_REDACTIONS: List[Tuple[str, re.Pattern, str]] = [
    ('SSN', re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN]'),
    ('email', re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'), '[EMAIL]'),
    ('phone', re.compile(r'(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)'), '[PHONE]'),
    ('date of birth', re.compile(r'\b(?P<k>DOB|date\s+of\s+birth|born(?:\s+on)?)\s*[:\-]?\s*' + _DATE, re.I), r'\g<k>: [DOB]'),
    ('MRN', re.compile(r'\b(?P<k>MRN|medical\s+record\s+(?:number|no\.?|#))\s*[:#]?\s*[A-Z0-9-]{4,}', re.I), r'\g<k>: [MRN]'),
    ('member ID', re.compile(r'\b(?P<k>(?:member|subscriber|policy|insurance)\s*(?:id|number|no\.?|#))\s*[:#]?\s*[A-Z0-9-]{6,}', re.I), r'\g<k>: [MEMBER-ID]'),
    ('address', re.compile(r'\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way)\b\.?'), '[ADDRESS]'),
    ('name', re.compile(r'\b(?P<k>patient(?:\s+name)?|pt|name|mr\.?|mrs\.?|ms\.?)\s*[:.]?\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+\b'), r'\g<k>: [NAME]'),
]


def redact_phi(text: str) -> Tuple[str, Dict[str, int]]:
    """Return (redacted_text, {label: count}). Heuristic; see module docstring."""
    out = text or ''
    counts: Dict[str, int] = {}
    for label, pattern, repl in _REDACTIONS:
        out, n = pattern.subn(repl, out)
        if n:
            counts[label] = n
    return out, counts


# --------------------------------------------------------------------------------------
# Role-based access control
# --------------------------------------------------------------------------------------
ALL_PERMISSIONS = frozenset({
    'read', 'write_claims', 'import_remit', 'draft_appeals', 'edit_appeals', 'approve_appeals',
    'record_outcomes', 'run_radar', 'use_llm', 'command_center', 'view_audit', 'verify_audit', 'redact',
})

ROLE_PERMISSIONS: Dict[str, frozenset] = {
    'admin': ALL_PERMISSIONS,
    'director': ALL_PERMISSIONS,
    'biller': frozenset({'read', 'write_claims', 'import_remit', 'draft_appeals', 'edit_appeals',
                         'record_outcomes', 'run_radar', 'use_llm', 'command_center', 'redact'}),
    'clinician': frozenset({'read', 'run_radar', 'draft_appeals', 'edit_appeals', 'approve_appeals',
                            'use_llm', 'redact'}),
    'compliance': frozenset({'read', 'command_center', 'view_audit', 'verify_audit', 'redact'}),
}


def parse_api_keys(raw: str | None) -> List[Tuple[str, str, Optional[str]]]:
    """'key:role[:tenant],key2:role2' -> [(key, role, tenant|None)]; unknown roles are ignored."""
    entries = []
    for part in (raw or '').split(','):
        bits = [b.strip() for b in part.strip().split(':')]
        if len(bits) >= 2 and bits[0] and bits[1] in ROLE_PERMISSIONS:
            entries.append((bits[0], bits[1], bits[2] if len(bits) > 2 and bits[2] else None))
    return entries


def resolve_key(key: str | None, demo_key: str, default_tenant: str, api_keys_raw: str | None) -> Optional[Tuple[str, str]]:
    """Return (tenant, role) for a presented API key, or None. Constant-time comparisons."""
    if not key:
        return None
    for k, role, tenant in parse_api_keys(api_keys_raw):
        if secrets.compare_digest(key, k):
            return tenant or default_tenant, role
    if secrets.compare_digest(key, demo_key):
        return default_tenant, 'admin'
    return None


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


# --------------------------------------------------------------------------------------
# Tamper-evident audit chain
# --------------------------------------------------------------------------------------
GENESIS = 'GENESIS'


def chain_hash(prev_hash: str, tenant: str, action: str, entity_id: Any, timestamp: str, actor: str, role: str) -> str:
    material = '|'.join([prev_hash, tenant, action, '' if entity_id is None else str(entity_id), timestamp, actor, role])
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def verify_chain(rows: Iterable[Any]) -> Dict[str, Any]:
    """
    Verify rows (ordered by id ascending). Rows without a hash are legacy (pre-chain) events:
    they are counted but cannot be verified, and the chain restarts from GENESIS after them.
    """
    expected_prev = GENESIS
    total = chained = legacy = 0
    head = None
    for row in rows:
        total += 1
        if not row.hash:
            legacy += 1
            expected_prev = GENESIS
            continue
        chained += 1
        if row.prev_hash != expected_prev:
            return {'intact': False, 'total': total, 'chained': chained, 'legacy': legacy, 'head_hash': head,
                    'broken_at': row.id,
                    'reason': 'Previous-hash link mismatch — a row before this one was deleted, inserted or reordered.'}
        recomputed = chain_hash(row.prev_hash, row.tenant_id, row.action, row.entity_id, row.timestamp,
                                row.actor or '', row.role or '')
        if recomputed != row.hash:
            return {'intact': False, 'total': total, 'chained': chained, 'legacy': legacy, 'head_hash': head,
                    'broken_at': row.id, 'reason': 'Row content was altered after it was written.'}
        expected_prev = row.hash
        head = row.hash
    return {'intact': True, 'total': total, 'chained': chained, 'legacy': legacy, 'head_hash': head,
            'broken_at': None, 'reason': None}
