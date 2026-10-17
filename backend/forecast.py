"""
Recovery Command Center analytics.

Pure functions over already-scored claim records: revenue-at-risk, expected-cash forecast,
payer behaviour analytics, a payer x root-cause heatmap, front-end prevention opportunities,
and a ranked "today's play" action queue. Every assumption is returned alongside the numbers.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List

RESOLVED_STATUSES = {'paid', 'approved', 'resolved', 'closed', 'denied_again'}
WIN_STATUSES = {'paid', 'approved'}
IDLE_STATUSES = {'new', 'in_review'}

# Days from today until money is expected to land, by workflow stage (illustrative assumptions).
LAG_DAYS_BY_STATUS = {
    'new': 56, 'in_review': 49, 'appeal_drafted': 42, 'submitted_manually': 28, 'denied_again': 70,
}
FORECAST_WEEKS = 12

# category -> (playbook headline, playbook, owner, preventable share assumption)
PREVENTION: Dict[str, tuple] = {
    'Authorization missing': ('Front-end authorization gate',
                              'Verify and attach the authorization number at scheduling; block check-out for payer-flagged CPTs without one.',
                              'Front desk', 0.70),
    'Missing information': ('Claim scrubber before submission',
                            'Add pre-bill edits for required fields (NPI, signatures, itemization) so incomplete claims never leave the building.',
                            'Billing', 0.80),
    'Missing documentation': ('Documentation completeness check',
                              'Auto-attach operative/progress notes and require provider signature before the claim is dropped.',
                              'Clinical documentation', 0.80),
    'Coding error': ('Modifier and diagnosis pointer audit',
                     'Run modifier/diagnosis edits against payer policy before billing; review top offending CPT pairs monthly.',
                     'Coding', 0.80),
    'Duplicate claim': ('Distinct-service modifier discipline',
                        'Require modifier 59/X{EPSU} justification at charge entry when the same CPT repeats on one date.',
                        'Coding', 0.50),
    'Incorrect payer': ('Real-time eligibility at every visit',
                        'Run a 270/271 at check-in and capture COB order; re-verify on the date of service.',
                        'Front desk', 0.75),
    'Timely filing': ('Submission-age alerting',
                      'Alert at 50% and 75% of each payer\'s filing window and archive 277CA acceptance reports automatically.',
                      'Billing', 0.85),
    'Bundled service': ('NCCI edit pre-check',
                        'Check NCCI procedure-to-procedure edits and modifier indicators at charge entry.',
                        'Coding', 0.40),
    'Medical necessity': ('Documentation templates by policy',
                          'Give clinicians templates that capture conservative-therapy dates, exam findings and rationale per payer policy.',
                          'Clinical leadership', 0.30),
    'Non-covered charge': ('Benefit verification and patient notice',
                           'Verify benefits pre-service and obtain signed notices for likely non-covered services.',
                           'Front desk', 0.20),
}


def _is_open(c: Dict[str, Any]) -> bool:
    return c['status'] not in RESOLVED_STATUSES


def _is_win(c: Dict[str, Any]) -> bool:
    return c['status'] in WIN_STATUSES or (c.get('recovered_amount') or 0) > 0


def payer_history(claims: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, int]]:
    """(payer, category) -> {'n': resolved claims, 'wins': won claims}."""
    hist: Dict[tuple, Dict[str, int]] = defaultdict(lambda: {'n': 0, 'wins': 0})
    for c in claims:
        if c.get('status') in RESOLVED_STATUSES and c.get('status') != 'closed':
            cat = c.get('category') or 'Other / needs review'
            payer = c.get('payer') or 'Other Payer'
            slot = hist[(payer, cat)]
            slot['n'] += 1
            slot['wins'] += 1 if _is_win(c) else 0
    return dict(hist)


def build_command_center(claims: List[Dict[str, Any]], today: date | None = None) -> Dict[str, Any]:
    today = today or date.today()
    open_claims = [c for c in claims if _is_open(c)]
    pursuable = [c for c in open_claims if c['intel']['worth_pursuing']]

    denied_total = sum(c['denied_amount'] for c in claims)
    recovered_total = sum(c.get('recovered_amount') or 0 for c in claims)
    resolved = [c for c in claims if c['status'] in RESOLVED_STATUSES]
    resolved_denied = sum(c['denied_amount'] for c in resolved)

    def amount_where(pred) -> float:
        return round(sum(c['denied_amount'] for c in open_claims if c['days'] is not None and pred(c['days'])), 2)

    kpis = {
        'open_claims': len(open_claims),
        'open_denied': round(sum(c['denied_amount'] for c in open_claims), 2),
        'expected_recoverable': round(sum(c['intel']['expected_value'] for c in pursuable), 2),
        'net_expected_value': round(sum(c['intel']['net_value'] for c in pursuable), 2),
        'recovered_to_date': round(recovered_total, 2),
        'recovery_rate': round(100 * recovered_total / resolved_denied, 1) if resolved_denied else None,
        'expiring_7d': amount_where(lambda d: 0 <= d <= 7),
        'expiring_14d': amount_where(lambda d: 0 <= d <= 14),
        'expiring_30d': amount_where(lambda d: 0 <= d <= 30),
        'already_expired': amount_where(lambda d: d < 0),
        'idle_expected_value_at_risk': round(sum(
            c['intel']['expected_value'] for c in pursuable
            if c['status'] in IDLE_STATUSES and c['days'] is not None and 0 <= c['days'] <= 14), 2),
        'denied_total': round(denied_total, 2),
    }

    # ---- Expected-cash forecast -----------------------------------------------------
    weeks = [{'week': i + 1, 'start': (today + timedelta(days=7 * i)).isoformat(), 'expected': 0.0, 'claims': 0}
             for i in range(FORECAST_WEEKS)]
    for c in pursuable:
        lag = LAG_DAYS_BY_STATUS.get(c['status'], 56)
        idx = min(FORECAST_WEEKS - 1, lag // 7)
        weeks[idx]['expected'] += c['intel']['expected_value']
        weeks[idx]['claims'] += 1
    running = 0.0
    for w in weeks:
        w['expected'] = round(w['expected'], 2)
        running += w['expected']
        w['cumulative'] = round(running, 2)

    # ---- Payer analytics -----------------------------------------------------------------
    by_payer: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'claims': 0, 'denied': 0.0, 'recovered': 0.0, 'resolved': 0, 'wins': 0, 'open': 0, 'age_days': [], 'cats': defaultdict(float)})
    for c in claims:
        p = by_payer[c['payer']]
        p['claims'] += 1
        p['denied'] += c['denied_amount']
        p['recovered'] += c.get('recovered_amount') or 0
        p['cats'][c['category']] += c['denied_amount']
        if c['status'] in RESOLVED_STATUSES:
            p['resolved'] += 1
            p['wins'] += 1 if _is_win(c) else 0
        else:
            p['open'] += 1
        if c.get('created_days_ago') is not None:
            p['age_days'].append(c['created_days_ago'])
    payers = []
    for name, p in by_payer.items():
        top_cat = max(p['cats'].items(), key=lambda kv: kv[1])[0] if p['cats'] else None
        payers.append({
            'payer': name, 'claims': p['claims'], 'open': p['open'], 'denied': round(p['denied'], 2),
            'recovered': round(p['recovered'], 2),
            'win_rate': round(100 * p['wins'] / p['resolved']) if p['resolved'] else None,
            'resolved': p['resolved'],
            'avg_age_days': round(sum(p['age_days']) / len(p['age_days'])) if p['age_days'] else None,
            'top_category': top_cat,
        })
    payers.sort(key=lambda x: x['denied'], reverse=True)

    # ---- Root-cause heatmap (payer x category) ------------------------------------------
    top_payers = [p['payer'] for p in payers[:6]]
    cats = sorted({c['category'] for c in claims}, key=lambda k: -sum(c['denied_amount'] for c in claims if c['category'] == k))
    cells: Dict[str, Dict[str, Dict[str, float]]] = {p: {} for p in top_payers}
    max_cell = 0.0
    for c in claims:
        if c['payer'] in cells:
            cell = cells[c['payer']].setdefault(c['category'], {'amount': 0.0, 'count': 0})
            cell['amount'] = round(cell['amount'] + c['denied_amount'], 2)
            cell['count'] += 1
            max_cell = max(max_cell, cell['amount'])

    # ---- Prevention opportunities -------------------------------------------------------
    cat_totals: Dict[str, Dict[str, float]] = defaultdict(lambda: {'amount': 0.0, 'count': 0})
    for c in claims:
        cat_totals[c['category']]['amount'] += c['denied_amount']
        cat_totals[c['category']]['count'] += 1
    prevention = []
    for cat, t in cat_totals.items():
        if cat not in PREVENTION:
            continue
        headline, playbook, owner, share = PREVENTION[cat]
        prevention.append({
            'category': cat, 'headline': headline, 'playbook': playbook, 'owner': owner,
            'denied': round(t['amount'], 2), 'count': int(t['count']),
            'preventable_share_assumed': share, 'preventable': round(t['amount'] * share, 2),
        })
    prevention.sort(key=lambda x: x['preventable'], reverse=True)

    # ---- Today's play ---------------------------------------------------------------------
    def weight(days):
        if days is None:
            return 1.0
        return 0.4 if days < 0 else 3.0 if days <= 7 else 2.0 if days <= 21 else 1.0
    ranked = sorted(pursuable, key=lambda c: c['intel']['expected_value'] * weight(c['days']), reverse=True)[:8]
    play = []
    for c in ranked:
        d = c['days']
        urgency = 'expired' if d is not None and d < 0 else (f'{d}d left' if d is not None else 'no deadline')
        play.append({
            'id': c['id'], 'claim_number': c['claim_number'], 'payer': c['payer'], 'category': c['category'],
            'denied_amount': c['denied_amount'], 'days': d, 'status': c['status'],
            'probability': c['intel']['probability'], 'expected_value': c['intel']['expected_value'],
            'path': c['intel']['path'], 'path_label': c['intel']['path_label'],
            'why': f"{urgency} · expected ${c['intel']['expected_value']:,.0f} at {c['intel']['probability']}% · {c['intel']['path_label']}",
        })

    return {
        'generated_at': today.isoformat(),
        'kpis': kpis,
        'forecast': weeks,
        'payers': payers,
        'heatmap': {'payers': top_payers, 'categories': cats, 'cells': cells, 'max_amount': round(max_cell, 2)},
        'prevention': prevention,
        'preventable_total': round(sum(p['preventable'] for p in prevention), 2),
        'todays_plays': play,
        'todays_play': play,  # legacy alias
        'assumptions': {
            'lag_days_by_status': LAG_DAYS_BY_STATUS,
            'forecast_weeks': FORECAST_WEEKS,
            'preventable_shares': {k: v[3] for k, v in PREVENTION.items()},
            'note': 'Forecast timing and preventable shares are illustrative assumptions; probabilities come from the explainable Denial Intelligence model.',
        },
    }
