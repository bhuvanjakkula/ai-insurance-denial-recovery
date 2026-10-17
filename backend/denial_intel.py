"""
Denial Intelligence — remittance parsing and explainable denial scoring.

* `parse_835` reads an X12 835 electronic remittance (ERA) and extracts denial lines
  (CAS group/CARC/amount, LQ*HE / MOA remark codes). Patient identity segments (NM1*QC etc.)
  are deliberately never read — minimum-necessary by construction.
* `parse_eob_text` extracts the same facts from pasted EOB / denial-letter text.
* `score_denial` produces an additive, fully explainable win-probability with a recommended
  path (appeal / corrected claim / rebill payer / bill patient / write off), blending the
  category benchmark with the clinic's own payer history (empirical-Bayes shrinkage),
  remark-code effects, deadline pressure and chart evidence coverage.

Code descriptions are brief paraphrases for orientation — confirm against the current
X12 CARC/RARC code lists before relying on them.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.engine import BENCHMARK_REVERSAL_RATES, lookup_payer_dispatch

GROUP_CODES = {
    'CO': 'Contractual obligation (provider liability)',
    'PR': 'Patient responsibility',
    'OA': 'Other adjustment',
    'PI': 'Payer-initiated reduction',
    'CR': 'Correction / reversal',
}

CARC_DESCRIPTIONS: Dict[str, str] = {
    '1': 'Deductible amount', '2': 'Coinsurance amount', '3': 'Co-payment amount',
    '4': 'Procedure code inconsistent with modifier, or required modifier missing',
    '11': 'Diagnosis inconsistent with the procedure',
    '15': 'Authorization number missing, invalid, or does not apply to the billed service/provider',
    '16': 'Claim lacks information needed for adjudication',
    '18': 'Exact duplicate claim/service',
    '22': 'May be covered by another payer per coordination of benefits',
    '27': 'Expenses incurred after coverage terminated',
    '29': 'Time limit for filing has expired',
    '31': 'Patient cannot be identified as the payer\'s insured',
    '45': 'Charge exceeds fee schedule / maximum allowable (contractual adjustment)',
    '50': 'Not deemed a medical necessity by the payer',
    '55': 'Procedure/treatment deemed experimental or investigational',
    '96': 'Non-covered charge(s)',
    '97': 'Benefit included in the allowance for another service/procedure (bundled)',
    '109': 'Claim/service not covered by this payer / contractor',
    '119': 'Benefit maximum for this time period or occurrence has been reached',
    '197': 'Precertification / authorization / notification absent',
    '204': 'Service not covered under the patient\'s current benefit plan',
    '252': 'Additional documentation needed to process the claim',
}

# effect: unprocessable | fixable | policy | neutral
RARC_CATALOG: Dict[str, Dict[str, str]] = {
    'MA130': {'text': 'Claim contains incomplete/invalid information; unprocessable, no appeal rights', 'effect': 'unprocessable'},
    'M51': {'text': 'Missing/incomplete/invalid procedure code(s)', 'effect': 'fixable'},
    'M62': {'text': 'Missing/incomplete/invalid treatment authorization code', 'effect': 'fixable'},
    'M76': {'text': 'Missing/incomplete/invalid diagnosis or condition', 'effect': 'fixable'},
    'M80': {'text': 'Not covered when performed during the same session/date as a previously processed service', 'effect': 'neutral'},
    'M127': {'text': 'Missing patient medical record for this service', 'effect': 'fixable'},
    'N19': {'text': 'Procedure code incidental to primary procedure', 'effect': 'neutral'},
    'N20': {'text': 'Service not payable with other service rendered on the same date', 'effect': 'neutral'},
    'N54': {'text': 'Claim information inconsistent with pre-certified/authorized services', 'effect': 'fixable'},
    'N115': {'text': 'Decision based on a Local Coverage Determination (LCD)', 'effect': 'policy'},
    'N130': {'text': 'Consult plan benefit documents for restrictions on this service', 'effect': 'policy'},
    'N290': {'text': 'Missing/incomplete/invalid rendering provider primary identifier', 'effect': 'fixable'},
    'N362': {'text': 'Days or units of service exceed the acceptable maximum', 'effect': 'neutral'},
    'N522': {'text': 'Duplicate of a claim processed, or to be processed, as a crossover claim', 'effect': 'neutral'},
}

PATH_LABELS = {
    'appeal': 'File a formal appeal',
    'corrected_claim': 'Submit a corrected claim (not an appeal)',
    'rebill_payer': 'Re-verify eligibility / COB and rebill the correct payer',
    'bill_patient': 'Bill the patient (cost-sharing, not a denial)',
    'no_action': 'No action — contractual write-down',
    'good_cause_review': 'Deadline passed — request good-cause review before any appeal',
    'write_off': 'Consider write-off — expected recovery is below the cost of working it',
}
_NON_APPEAL_PATHS = {'bill_patient', 'no_action', 'write_off'}

_SEGMENT_CASE_RE = re.compile(r'^[A-Z0-9]{2,3}$')


def appeal_cost_usd() -> float:
    try:
        return max(0.0, float(os.environ.get('APPEAL_COST_USD', '40')))
    except ValueError:
        return 40.0


def _iso(d: str | None) -> Optional[str]:
    """Normalise CCYYMMDD / YYMMDD / MM/DD/YYYY / YYYY-MM-DD into ISO date."""
    if not d:
        return None
    d = d.strip()
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%y%m%d'):
        try:
            return datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amt(v: str | None) -> float:
    try:
        return float(str(v).replace(',', '').replace('$', ''))
    except (TypeError, ValueError):
        return 0.0


def _add_days(iso: str | None, days: int) -> Optional[str]:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()
    except ValueError:
        return None


def category_for(group: str, carc: str) -> str:
    """Local import avoids a hard cycle if engine ever needs this module."""
    from backend.engine import RULES
    key = f'{group}-{carc}'
    return (RULES.get(key) or RULES.get(carc) or ('Other / needs review',))[0]


def path_for(category: str, group: str, rarcs: List[str], days_left: Optional[int]) -> str:
    if group == 'PR' or category == 'Patient responsibility':
        return 'bill_patient'
    if category == 'Contractual adjustment':
        return 'no_action'
    if any(RARC_CATALOG.get(r, {}).get('effect') == 'unprocessable' for r in rarcs):
        return 'corrected_claim'
    if category == 'Coding error':
        return 'corrected_claim'
    if days_left is not None and days_left < 0:
        return 'good_cause_review'
    if category == 'Incorrect payer':
        return 'rebill_payer'
    return 'appeal'


# --------------------------------------------------------------------------------------
# 835 parsing
# --------------------------------------------------------------------------------------
def _split_segments(text: str):
    t = text.strip()
    elem, sub, seg = '*', ':', '~'
    if t.startswith('ISA') and len(t) >= 106:
        elem, sub, seg = t[3], t[104], t[105]
    elif '~' not in t:
        seg = '\n'
    segs = [s.strip() for s in t.split(seg) if s.strip()]
    return [s.split(elem) for s in segs], sub


def _finalize_claim(c: Dict[str, Any], out: List[Dict[str, Any]], ctx: Dict[str, Any]) -> None:
    if not c:
        return
    adj: Dict[tuple, Dict[str, Any]] = {}
    sources = [(c['cas'], c['rarcs'], None)] + [(s['cas'], c['rarcs'] + s['rarcs'], s['proc']) for s in c['svcs']]
    for cas_list, rarcs, proc in sources:
        for group, carc, amount in cas_list:
            slot = adj.setdefault((group, carc), {'amount': 0.0, 'rarcs': [], 'procs': []})
            slot['amount'] += amount
            for r in rarcs:
                if r not in slot['rarcs']:
                    slot['rarcs'].append(r)
            if proc and proc not in slot['procs']:
                slot['procs'].append(proc)
    lines = [(k, v) for k, v in adj.items() if v['amount'] > 0]
    multi = len(lines) > 1
    for (group, carc), v in lines:
        service_date = c['service_date'] or ctx.get('remit_date')
        payer = ctx.get('payer') or 'Unknown Payer'
        window = lookup_payer_dispatch(payer)['appeal_window_days']
        out.append({
            'source': '835',
            'claim_number': c['id'],
            'claim_ref': f"{c['id']}-{group}{carc}" if multi else c['id'],
            'payer_claim_control': c.get('payer_ctrl'),
            'payer': payer,
            'clinic_name': ctx.get('payee') or 'Imported Clinic',
            'service_date': service_date,
            'remit_date': ctx.get('remit_date'),
            'group_code': group,
            'carc': carc,
            'denial_code': f'{group}-{carc}',
            'carc_description': CARC_DESCRIPTIONS.get(carc, 'See payer remittance for CARC meaning'),
            'denied_amount': round(v['amount'], 2),
            'rarcs': v['rarcs'],
            'procedures': v['procs'],
            'claim_billed': c['billed'],
            'claim_paid': c['paid'],
            'appeal_deadline': _add_days(ctx.get('remit_date'), window),
        })


def parse_835(text: str) -> Dict[str, Any]:
    """Parse an X12 835 into denial/adjustment lines. Never reads patient-identity segments."""
    segments, sub = _split_segments(text)
    if not segments or not any(s[0] == 'CLP' for s in segments):
        raise ValueError('No CLP (claim payment) segments found — this does not look like an 835.')
    ctx: Dict[str, Any] = {'payer': None, 'payee': None, 'remit_date': None}
    out: List[Dict[str, Any]] = []
    claim: Dict[str, Any] = {}
    svc: Optional[Dict[str, Any]] = None

    def get(els: List[str], i: int) -> str:
        return els[i].strip() if len(els) > i and els[i] else ''

    for els in segments:
        tag = els[0]
        if tag == 'BPR':
            ctx['remit_date'] = _iso(get(els, 16)) or ctx['remit_date']
        elif tag == 'N1':
            if get(els, 1) == 'PR':
                ctx['payer'] = get(els, 2)
            elif get(els, 1) == 'PE':
                ctx['payee'] = get(els, 2)
        elif tag == 'CLP':
            _finalize_claim(claim, out, ctx)
            claim = {
                'id': get(els, 1), 'status': get(els, 2), 'billed': _amt(get(els, 3)), 'paid': _amt(get(els, 4)),
                'payer_ctrl': get(els, 7) or None, 'cas': [], 'rarcs': [], 'svcs': [], 'service_date': None,
            }
            svc = None
        elif not claim:
            continue
        elif tag == 'SVC':
            proc = get(els, 1).split(sub)[-1] if get(els, 1) else ''
            svc = {'proc': proc, 'cas': [], 'rarcs': []}
            claim['svcs'].append(svc)
        elif tag == 'CAS':
            group = get(els, 1)
            i = 2
            while i < len(els) and get(els, i):
                carc, amount = get(els, i), _amt(get(els, i + 1))
                (svc if svc else claim)['cas'].append((group, carc, amount))
                i += 3
        elif tag == 'LQ' and get(els, 1) == 'HE':
            code = get(els, 2)
            if code:
                (svc if svc else claim)['rarcs'].append(code)
        elif tag == 'MOA':
            for i in range(3, 8):
                if get(els, i):
                    claim['rarcs'].append(get(els, i))
        elif tag == 'DTM':
            q, d = get(els, 1), _iso(get(els, 2))
            if q in ('232', '472') and d and not claim['service_date']:
                claim['service_date'] = d
    _finalize_claim(claim, out, ctx)
    return {
        'format': '835', 'payer': ctx['payer'], 'clinic_name': ctx['payee'],
        'remit_date': ctx['remit_date'], 'lines': out,
        'note': 'Patient identity segments (NM1*QC and similar) are never read or returned.',
    }


# --------------------------------------------------------------------------------------
# EOB / denial-letter text parsing
# --------------------------------------------------------------------------------------
_DATE_TOKEN = r'(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})'
_RARC_RE = re.compile(r'\b(MA\d{1,3}|M\d{1,3}|N\d{1,4})\b(?!\.\d)')
_GROUP_CARC_RE = re.compile(r'\b(CO|PR|OA|PI|CR)\s*[-: ]\s*(\d{1,3})\b', re.I)
_CARC_ONLY_RE = re.compile(r'\b(?:CARC|reason\s+code|denial\s+code|adjustment\s+code)\s*[:#]?\s*(\d{1,3})\b', re.I)


def _first(pattern: str, text: str, flags=re.I) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _parse_eob_chunk(chunk: str) -> Optional[Dict[str, Any]]:
    claim_no = _first(r'claim\s*(?:#|no\.?|number|id)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})', chunk)
    m = _GROUP_CARC_RE.search(chunk)
    if m:
        group, carc = m.group(1).upper(), m.group(2)
    else:
        m2 = _CARC_ONLY_RE.search(chunk)
        if not m2:
            return None
        group, carc = 'CO', m2.group(1)
    others = sorted({f'{g.upper()}-{c}' for g, c in _GROUP_CARC_RE.findall(chunk)} - {f'{group}-{carc}'})
    amount = None
    for label in (r'denied', r'disallowed', r'not\s+covered', r'patient\s+responsibility', r'billed|charge'):
        mm = re.search(r'(?:' + label + r')[^$\n]{0,30}\$\s*([\d,]+(?:\.\d{2})?)', chunk, re.I)
        if mm:
            amount = _amt(mm.group(1))
            break
    payer = _first(r'(?:payer|insurance|plan|carrier)\s*[:\-]\s*([^\n\r]+)', chunk)
    clinic = _first(r'(?:provider|clinic|facility|practice)\s*(?:name)?\s*[:\-]\s*([^\n\r]+)', chunk)
    service_date = _iso(_first(r'(?:date\s+of\s+service|service\s+date|DOS)\s*[:\-]?\s*' + _DATE_TOKEN, chunk))
    processed = _iso(_first(r'(?:date\s+processed|processed(?:\s+on)?|remit(?:tance)?\s+date|letter\s+date)\s*[:\-]?\s*' + _DATE_TOKEN, chunk))
    deadline = _iso(_first(r'appeal(?:s)?\s+(?:deadline|due(?:\s+date)?|by)\s*[:\-]?\s*' + _DATE_TOKEN, chunk))
    if not deadline:
        within = _first(r'appeal[^.\n]{0,60}within\s+(\d{2,3})\s+(?:calendar\s+)?days', chunk)
        if within and processed:
            deadline = _add_days(processed, int(within))
    payer = payer or 'Unknown Payer'
    if not deadline and processed:
        deadline = _add_days(processed, lookup_payer_dispatch(payer)['appeal_window_days'])
    rarcs = list(dict.fromkeys(_RARC_RE.findall(chunk)))
    return {
        'source': 'text',
        'claim_number': claim_no,
        'claim_ref': claim_no,
        'payer': payer,
        'clinic_name': clinic or 'Imported Clinic',
        'service_date': service_date or processed,
        'remit_date': processed,
        'group_code': group,
        'carc': carc,
        'denial_code': f'{group}-{carc}',
        'carc_description': CARC_DESCRIPTIONS.get(carc, 'See payer letter for reason meaning'),
        'denied_amount': round(amount or 0.0, 2),
        'rarcs': rarcs,
        'procedures': [],
        'secondary_codes': others,
        'appeal_deadline': deadline,
    }


def parse_eob_text(text: str) -> Dict[str, Any]:
    body = text.strip()
    parts = re.split(r'(?im)^(?=\s*claim\s*(?:#|no\.?|number|id)\b)', body)
    chunks = [p for p in parts if p.strip()] if len(parts) > 1 else [body]
    lines = [r for r in (_parse_eob_chunk(c) for c in chunks) if r]
    if not lines:
        raise ValueError('No adjustment/denial codes (e.g. CO-50, PR-2, CARC 197) found in the text.')
    return {'format': 'text', 'payer': lines[0]['payer'], 'clinic_name': lines[0]['clinic_name'],
            'remit_date': lines[0]['remit_date'], 'lines': lines}


def parse_remittance(text: str) -> Dict[str, Any]:
    """Auto-detects X12 835 versus free-text EOB."""
    t = (text or '').strip()
    if not t:
        raise ValueError('Empty remittance text.')
    looks_x12 = t.startswith('ISA') or bool(re.search(r'(^|[~\n])CLP\*', t))
    return parse_835(t) if looks_x12 else parse_eob_text(t)


# --------------------------------------------------------------------------------------
# Explainable scoring
# --------------------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def score_denial(
    *,
    category: str,
    group_code: str,
    rarcs: List[str],
    denied_amount: float,
    days_to_deadline: Optional[int],
    payer: str,
    history: Optional[Dict[str, int]] = None,
    evidence_coverage: Optional[int] = None,
) -> Dict[str, Any]:
    baseline = BENCHMARK_REVERSAL_RATES.get(category, 45)
    factors: List[Dict[str, Any]] = []
    prob = float(baseline)

    def add(name: str, delta: float, detail: str) -> None:
        nonlocal prob
        delta = round(delta, 1)
        prob += delta
        factors.append({'name': name, 'delta': delta, 'detail': detail})

    if group_code == 'PR':
        factors.append({'name': 'Patient responsibility', 'delta': 0.0,
                        'detail': 'PR group code means cost-sharing owed by the patient — not a payer denial.'})

    seen_effects = set()
    for r in rarcs:
        info = RARC_CATALOG.get(r)
        if not info or info['effect'] in seen_effects:
            continue
        seen_effects.add(info['effect'])
        if info['effect'] == 'fixable':
            add(f'Remark {r}: fixable defect', +6, info['text'] + ' — typically cured by supplying the missing element.')
        elif info['effect'] == 'policy':
            add(f'Remark {r}: policy-based', -4, info['text'] + ' — expect to argue against written policy criteria.')
        elif info['effect'] == 'unprocessable':
            factors.append({'name': f'Remark {r}: unprocessable', 'delta': 0.0,
                            'detail': info['text'] + ' — fix and resubmit; an appeal is not the right vehicle.'})
        else:
            factors.append({'name': f'Remark {r}', 'delta': 0.0, 'detail': info['text']})

    if days_to_deadline is not None:
        if days_to_deadline < 0:
            add('Appeal deadline passed', -30, f'{abs(days_to_deadline)} days past the recorded deadline; only good-cause exceptions remain.')
        elif days_to_deadline <= 2:
            add('Deadline in 2 days or less', -5, 'Little time to gather evidence and transmit; execution risk is high.')

    if history and history.get('n', 0) >= 3:
        n, wins = history['n'], history.get('wins', 0)
        prior_strength = 8
        est = 100 * (wins + prior_strength * baseline / 100) / (n + prior_strength)
        delta = _clamp(est - baseline, -25, 25)
        add('Your history with this payer', delta,
            f'{wins} of {n} resolved {category.lower()} claims won with {payer} '
            f'(shrunk toward the {baseline}% benchmark so small samples do not overreact).')

    if evidence_coverage is not None:
        add('Chart evidence coverage', _clamp((evidence_coverage - 60) * 0.35, -15, 15),
            f'Evidence Gap Radar coverage is {evidence_coverage}/100 versus a neutral 60.')

    prob = round(_clamp(prob, 2, 97))
    path = path_for(category, group_code, rarcs, days_to_deadline)
    cost = appeal_cost_usd()
    expected = round(denied_amount * prob / 100, 2)
    net = round(expected - cost, 2)
    if path not in _NON_APPEAL_PATHS and net <= 0:
        path = 'write_off'
    worth = path not in _NON_APPEAL_PATHS
    return {
        'probability': prob,
        'baseline': baseline,
        'factors': factors,
        'path': path,
        'path_label': PATH_LABELS[path],
        'appealable': path in ('appeal', 'good_cause_review'),
        'worth_pursuing': worth,
        'expected_value': expected,
        'work_cost_assumption': cost,
        'net_value': net,
        'method': 'intel_additive_v1',
        'disclaimer': 'Illustrative, explainable heuristic — not a guarantee of payer outcome.',
    }


def days_until(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso[:10]) - date.today()).days
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Synthetic sample 835 (dates relative to today so deadlines are live)
# --------------------------------------------------------------------------------------
def sample_835() -> str:
    today = date.today()
    remit = (today - timedelta(days=165)).strftime('%Y%m%d')   # UHC 180-day window -> ~15 days left
    dos = lambda n: (today - timedelta(days=n)).strftime('%Y%m%d')
    isa = ('ISA*00*' + ' ' * 10 + '*00*' + ' ' * 10 + '*ZZ*' + 'UHCPAYER'.ljust(15) + '*ZZ*' + 'METROCLINIC'.ljust(15) +
           '*' + today.strftime('%y%m%d') + '*1253*^*00501*000000905*0*T*:~')
    segs = [
        isa,
        'GS*HP*UHCPAYER*METROCLINIC*' + today.strftime('%Y%m%d') + '*1253*1*X*005010X221A1~',
        'ST*835*0001~',
        f'BPR*I*1520.00*C*ACH*CCP*01*999999999*DA*123456*1512345678**01*999988880*DA*98765*{remit}~',
        'TRN*1*12345*1512345678~',
        'N1*PR*UnitedHealthcare~',
        'N1*PE*METRO HEALTH SPECIALISTS*XX*1234567893~',
        # Claim 1: medical necessity denial, LCD based
        'CLP*SYN-835-1001*4*2840*0*0*12*ICN7700001~', 'NM1*QC*1*SYNTHETIC*PATIENT****MI*000000001~',
        f'DTM*232*{dos(210)}~', f'SVC*HC:72148*2840*0~', f'DTM*472*{dos(210)}~', 'CAS*CO*50*2840~', 'LQ*HE*N115~',
        # Claim 2: authorization defect
        'CLP*SYN-835-1002*4*1890*0*0*12*ICN7700002~', f'DTM*232*{dos(200)}~',
        'SVC*HC:93306*1890*0~', f'DTM*472*{dos(200)}~', 'CAS*CO*197*1890~', 'LQ*HE*M62~',
        # Claim 3: unprocessable incomplete claim
        'CLP*SYN-835-1003*4*960*0*0*12*ICN7700003~', f'DTM*232*{dos(195)}~',
        'SVC*HC:99214*960*0~', f'DTM*472*{dos(195)}~', 'CAS*CO*16*960~', 'LQ*HE*MA130~',
        # Claim 4: paid claim with contractual write-down, coinsurance and a bundled line
        'CLP*SYN-835-1004*1*1700*1520*85*12*ICN7700004~', f'DTM*232*{dos(190)}~',
        'SVC*HC:99213*220*180~', f'DTM*472*{dos(190)}~', 'CAS*CO*45*40~', 'CAS*PR*2*85~',
        'SVC*HC:20610*780*0~', f'DTM*472*{dos(190)}~', 'CAS*CO*97*780~', 'LQ*HE*N20~',
        'SE*30*0001~', 'GE*1*1~', 'IEA*1*000000905~',
    ]
    return '\n'.join(segs)


def sample_eob() -> str:
    today = date.today()
    dos1 = (today - timedelta(days=28)).strftime('%Y-%m-%d')
    dos2 = (today - timedelta(days=22)).strftime('%Y-%m-%d')
    remit = (today - timedelta(days=10)).strftime('%Y-%m-%d')
    deadline1 = (today + timedelta(days=35)).strftime('%Y-%m-%d')
    deadline2 = (today + timedelta(days=45)).strftime('%Y-%m-%d')
    return f"""EXPLANATION OF BENEFITS / ADVERSE BENEFIT DETERMINATION
Payer: Aetna Health Plans
Provider: Metro Health Specialists
Processed Date: {remit}

Claim Number: EOB-2026-4401
Date of Service: {dos1}
Billed Amount: $3,250.00
Denied Amount: $2,840.00
Adjustment Code: CO-50 (Not deemed a medical necessity by the payer)
Remark Code: N115
Remarks: Lumbar MRI not supported by sufficient conservative therapy duration in documentation.
Appeal Deadline: {deadline1}

Claim Number: EOB-2026-4402
Date of Service: {dos2}
Billed Amount: $2,100.00
Denied Amount: $1,890.00
Adjustment Code: CO-197 (Precertification/authorization absent)
Remark Code: M62
Remarks: Echocardiogram outpatient service lacks prior authorization code.
Appeal Deadline: {deadline2}
"""
