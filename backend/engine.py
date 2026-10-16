"""Explainable, deterministic denial triage: suggestions, NOT coverage decisions."""
from datetime import date

RULES = {
    '16': ('Missing information', 'Review missing/invalid claim information; verify payer-specific remark codes and resubmit corrected information if appropriate.', ['Original claim', 'Corrected claim fields', 'Supporting documentation']),
    '18': ('Duplicate claim', 'Compare payer claim identifiers and submission history; seek reconsideration if this is not a duplicate.', ['Submission history', 'Prior remittance', 'Claim identifiers']),
    '29': ('Timely filing', 'Check the applicable payer contract and proof of timely submission before appealing.', ['Payer filing policy', 'Clearinghouse acceptance', 'Submission timestamp']),
    '50': ('Medical necessity', 'Review clinical documentation and applicable payer medical-necessity criteria; route for clinical review.', ['Relevant clinical records', 'Medical necessity rationale', 'Payer policy']),
    '96': ('Non-covered charge', 'Verify member benefit details, policy exclusions, and any authorization.', ['Eligibility verification', 'Benefit details', 'Authorization if available']),
    '109': ('Incorrect payer', 'Verify coverage and coordination of benefits; direct corrected claim to the appropriate payer.', ['Insurance eligibility', 'COB details', 'Correct payer information']),
    '197': ('Authorization missing', 'Check prior authorization records and payer exceptions; escalate to authorization staff.', ['Authorization approval', 'Clinical documentation', 'Payer policy']),
    'CO-16': ('Missing information', 'Review missing/invalid claim information and any accompanying remark codes.', ['Corrected claim', 'Supporting documentation']),
    'CO-29': ('Timely filing', 'Confirm filing cutoff and receipt evidence; review appeal rights.', ['Filing evidence', 'Applicable payer policy']),
    'CO-50': ('Medical necessity', 'Clinician should review medical necessity and policy applicability.', ['Clinical records', 'Payer medical policy']),
    'CO-197': ('Authorization missing', 'Verify authorization evidence and payer exception criteria.', ['Authorization record', 'Clinical records']),
}

def assess(claim):
    raw = str(claim.denial_code or '').strip().upper()
    key = raw if raw in RULES else raw.split('-')[-1]
    category, action, docs = RULES.get(key, ('Other / needs review', 'Review the full remittance advice, payer policy, and appeal rights with billing staff.', ['Original claim', 'Remittance advice', 'Applicable payer policy']))
    amt = float(claim.denied_amount or 0)
    days = (date.fromisoformat(claim.appeal_deadline) - date.today()).days if claim.appeal_deadline else None
    score = min(100, round(min(amt / 100, 40) + (35 if days is not None and days <= 7 else 20 if days is not None and days <= 21 else 0) + (20 if key in RULES else 5)))
    if days is not None and days < 0:
        action = 'Recorded appeal deadline has passed. Verify actual appeal rights and exceptions with the payer; do not assume appeal eligibility. ' + action
    return {'category': category, 'recommended_action': action, 'required_documents': docs, 'priority_score': score, 'days_to_deadline': days, 'explanation': f'Rule based on entered denial code {raw or "(missing)"}, denied amount ${amt:,.2f}, and entered deadline. Priority is operational only, not likelihood of appeal success.', 'method': 'transparent_rules_v1'}

def draft_appeal(claim, analysis):
    return f'''DRAFT — HUMAN REVIEW REQUIRED — NOT SUBMITTED

To: Appeals / Claims Review Department, {claim.payer}
Re: Request for review of denied claim {claim.claim_number}
Clinic: {claim.clinic_name}
Date of service: {claim.service_date}
Claim denial code: {claim.denial_code}
Amount disputed: ${claim.denied_amount:,.2f}

Dear Claims Review Team,

We request review of the adverse claim determination identified above. The stated denial category is: {analysis['category']}.

Basis for review (verify and complete with evidence):
{analysis['recommended_action']}

Supporting information to gather: {', '.join(analysis['required_documents'])}.

Please review the enclosed, verified claim-specific documents and provide a written determination with any applicable policy references. This template does not assert clinical facts, prior authorization, coverage, compliance with filing deadlines, or payer obligations without evidence.

Sincerely,
Authorized clinic billing representative

CHECKLIST BEFORE USE: verify patient/claim identity, remittance/remark codes, plan terms, payer-specific address, appeal level, deadlines, supporting records, and authorization to disclose minimum-necessary information. Obtain human sign-off. Do not send this draft without review.
'''
