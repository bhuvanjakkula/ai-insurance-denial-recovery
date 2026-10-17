"""
Clinical Revenue Cycle Management (RCM) & Denial Recovery Engine.
Provides deterministic, explainable rule classification, multi-factor urgency scoring,
payer submission directory, Level 1 & Level 2 escalation drafting, and evidence checklists.
"""
from datetime import date, datetime
from typing import Dict, Any, List

PAYER_DISPATCH_DIRECTORY = {
    'aetna': {
        'payer_name': 'Aetna Health Plans',
        'appeals_portal': 'https://www.availity.com',
        'appeals_fax': '1-859-455-8650',
        'appeals_address': 'Aetna Grievances & Appeals, PO Box 14079, Lexington, KY 40512',
        'timely_filing_limit_days': 180,
        'appeal_window_days': 180,
        'notes': 'Accepts Level 1 reconsideration via Availity portal or fax. Include complete EOB copy.'
    },
    'unitedhealthcare': {
        'payer_name': 'UnitedHealthcare (UHC)',
        'appeals_portal': 'https://www.uhcprovider.com',
        'appeals_fax': '1-801-994-1082',
        'appeals_address': 'UnitedHealthcare Claims Appeals, PO Box 30432, Salt Lake City, UT 84130',
        'timely_filing_limit_days': 90,
        'appeal_window_days': 180,
        'notes': 'Requires UHC Provider Portal submission or certified mail. Peer-to-peer within 14 calendar days of denial.'
    },
    'uhc': {
        'payer_name': 'UnitedHealthcare (UHC)',
        'appeals_portal': 'https://www.uhcprovider.com',
        'appeals_fax': '1-801-994-1082',
        'appeals_address': 'UnitedHealthcare Claims Appeals, PO Box 30432, Salt Lake City, UT 84130',
        'timely_filing_limit_days': 90,
        'appeal_window_days': 180,
        'notes': 'Requires UHC Provider Portal submission or certified mail. Peer-to-peer within 14 calendar days of denial.'
    },
    'cigna': {
        'payer_name': 'Cigna HealthCare',
        'appeals_portal': 'https://cignaforhcp.cigna.com',
        'appeals_fax': '1-877-815-4827',
        'appeals_address': 'Cigna National Appeals Unit, PO Box 188011, Chattanooga, TN 37422',
        'timely_filing_limit_days': 90,
        'appeal_window_days': 180,
        'notes': 'Requires Request for Health Care Professional Review form attached to first-level appeal packet.'
    },
    'blue cross': {
        'payer_name': 'Blue Cross Blue Shield (BCBS)',
        'appeals_portal': 'https://www.availity.com',
        'appeals_fax': '1-888-285-2242',
        'appeals_address': 'BCBS Provider Appeals Department, PO Box 7904, Boise, ID 83707',
        'timely_filing_limit_days': 365,
        'appeal_window_days': 180,
        'notes': 'Direct to local Home Plan jurisdiction using BlueCard routing rules on member ID prefix.'
    },
    'bcbs': {
        'payer_name': 'Blue Cross Blue Shield (BCBS)',
        'appeals_portal': 'https://www.availity.com',
        'appeals_fax': '1-888-285-2242',
        'appeals_address': 'BCBS Provider Appeals Department, PO Box 7904, Boise, ID 83707',
        'timely_filing_limit_days': 365,
        'appeal_window_days': 180,
        'notes': 'Direct to local Home Plan jurisdiction using BlueCard routing rules on member ID prefix.'
    },
    'humana': {
        'payer_name': 'Humana Medicare & Commercial',
        'appeals_portal': 'https://www.availity.com',
        'appeals_fax': '1-800-949-2961',
        'appeals_address': 'Humana Grievance & Appeals, PO Box 14555, Lexington, KY 40512',
        'timely_filing_limit_days': 180,
        'appeal_window_days': 180,
        'notes': 'Expedited 72-hour clinical appeals accepted by phone or dedicated fax.'
    },
    'medicare': {
        'payer_name': 'Medicare Part B (CMS)',
        'appeals_portal': 'https://www.cms.gov/medicare/appeals-grievances',
        'appeals_fax': 'Contact Regional MAC',
        'appeals_address': 'Regional Medicare Administrative Contractor (MAC) Appeals Dept',
        'timely_filing_limit_days': 365,
        'appeal_window_days': 120,
        'notes': 'Level 1: Redetermination via Form CMS-20027 within 120 days from date of initial determination.'
    },
    'kaiser': {
        'payer_name': 'Kaiser Permanente',
        'appeals_portal': 'https://providers.kaiserpermanente.org',
        'appeals_fax': '1-866-512-2122',
        'appeals_address': 'Kaiser Claims Administration Appeals, PO Box 34584, Seattle, WA 98124',
        'timely_filing_limit_days': 180,
        'appeal_window_days': 180,
        'notes': 'Non-contracted providers must include CMS Non-Contracted Provider Payment Dispute form.'
    }
}

RULES = {
    # CARC 16: Missing/Incomplete Information
    '16': (
        'Missing information',
        'Review remittance remark codes for specific missing fields (e.g., operative note, NDC code, or itemized invoice). Collate verified documentation and submit corrected claim packet.',
        ['Complete Operative/Procedure Report', 'Itemized Superbill with CPT/HCPCS & ICD-10', 'Ordering Physician Signature & NPI', 'Patient Insurance Card Scan (Front & Back)']
    ),
    'CO-16': (
        'Missing information',
        'Review remittance remark codes for specific missing fields; assemble supporting clinical documents and file a formal redetermination with corrected data.',
        ['Complete Operative/Procedure Report', 'Itemized Superbill with CPT/HCPCS & ICD-10', 'Ordering Physician Signature & NPI', 'Patient Insurance Card Scan (Front & Back)']
    ),
    # CARC 18: Exact Duplicate Claim / Service
    '18': (
        'Duplicate claim',
        'Analyze service dates, anatomic sites, and procedure line items. If services were distinct, resubmit with Modifier -59 (Distinct Procedural Service), -XE, -XP, -XS, or -XU as appropriate.',
        ['Operative Report Describing Distinct Sessions/Sites', 'Proof of Distinct Encounter Times / Anatomical Sites', 'Prior Remittance Advice / EOB Comparison Table', 'Physician Letter of Medical Distinction']
    ),
    'CO-18': (
        'Duplicate claim',
        'Review original claim history against new charge. If non-duplicate, append modifier -59 or distinct service modifier with documentation justifying separate procedural encounter.',
        ['Operative Report Describing Distinct Sessions/Sites', 'Proof of Distinct Encounter Times / Anatomical Sites', 'Prior Remittance Advice / EOB Comparison Table', 'Physician Letter of Medical Distinction']
    ),
    # CARC 29: Timely Filing
    '29': (
        'Timely filing',
        'Audit initial electronic submission history. Retrieve clearinghouse EDI 277/999 acceptance timestamps proving receipt within payer contractual filing window (typically 90-365 days).',
        ['Clearinghouse EDI 277 / 999 Batch Transmission Log', 'Initial Claim Submission Confirmation with Date Stamp', 'Proof of Secondary Payer Delay or COB Coordination', 'Payer Contract Filing Window Clause Citation']
    ),
    'CO-29': (
        'Timely filing',
        'Verify payer filing limit against electronic clearinghouse transmission acceptance receipt. Submit proof of timely initial filing with appeal letter.',
        ['Clearinghouse EDI 277 / 999 Batch Transmission Log', 'Initial Claim Submission Confirmation with Date Stamp', 'Proof of Secondary Payer Delay or COB Coordination', 'Payer Contract Filing Window Clause Citation']
    ),
    # CARC 50: Medical Necessity
    '50': (
        'Medical necessity',
        'Initiate clinical peer review. Cross-reference clinical documentation against payer Local Coverage Determinations (LCD) or National Coverage Determinations (NCD). Prove conservative treatment failure.',
        ['Attending Physician Clinical Notes & Chronology', 'Documentation of Failed Conservative Therapies', 'Diagnostic Imaging / Pathology / Laboratory Results', 'Relevant CMS LCD/NCD or Clinical Guidelines Citation']
    ),
    'CO-50': (
        'Medical necessity',
        'Submit comprehensive clinical documentation establishing diagnosis, disease severity, failed conservative treatments, and standard-of-care justification matching CMS LCD/NCD criteria.',
        ['Attending Physician Clinical Notes & Chronology', 'Documentation of Failed Conservative Therapies', 'Diagnostic Imaging / Pathology / Laboratory Results', 'Relevant CMS LCD/NCD or Clinical Guidelines Citation']
    ),
    # CARC 96: Non-Covered Charges
    '96': (
        'Non-covered charge',
        'Verify patient benefit schedule, summary plan description (SPD), and clinical exemption criteria. Request reconsideration under plan benefit exception or off-label coverage criteria.',
        ['Signed Advance Beneficiary Notice (ABN) if Applicable', 'Summary Plan Description (SPD) Benefit Excerpt', 'Peer-Reviewed Clinical Literature Supporting Efficacy', 'Prior Authorization / Pre-Determination File']
    ),
    'CO-96': (
        'Non-covered charge',
        'Audit contract coverage exclusions. Determine whether plan exclusion applies or whether clinical criteria support a medical exception.',
        ['Signed Advance Beneficiary Notice (ABN) if Applicable', 'Summary Plan Description (SPD) Benefit Excerpt', 'Peer-Reviewed Clinical Literature Supporting Efficacy', 'Prior Authorization / Pre-Determination File']
    ),
    # CARC 97: Bundled / Included in Primary Service
    '97': (
        'Bundled service',
        'Verify NCCI edits. If procedure was independent or separately identifiable, appeal with documentation supporting Modifier -25 (Significant, Separately Identifiable E/M) or -59.',
        ['Detailed Operative Notes Showing Independent Service', 'NCCI Edit Manual Exemption Verification', 'Time and Complexity Documentation', 'Modifier -25 / -59 Justification Statement']
    ),
    'CO-97': (
        'Bundled service',
        'Inspect National Correct Coding Initiative (NCCI) edit guidelines. Present clinical records proving procedure was distinctly separate from primary surgical package.',
        ['Detailed Operative Notes Showing Independent Service', 'NCCI Edit Manual Exemption Verification', 'Time and Complexity Documentation', 'Modifier -25 / -59 Justification Statement']
    ),
    # CARC 109: Not Covered by This Payer / Coordination of Benefits
    '109': (
        'Incorrect payer',
        'Perform real-time eligibility (EDI 270/271) check. Verify primary vs secondary payer order under NAIC model regulations and birthday rule. Re-route to correct payer.',
        ['Coordination of Benefits (COB) Questionnaire', 'Primary Payer EOB / Remittance (if billing secondary)', 'Current Electronic Eligibility Verification (271)', 'Patient Signed Coordination Statement']
    ),
    'CO-109': (
        'Incorrect payer',
        'Verify primary insurance policyholder status and coordination of benefits rules. Rebill primary insurer or submit primary EOB to secondary insurer.',
        ['Coordination of Benefits (COB) Questionnaire', 'Primary Payer EOB / Remittance (if billing secondary)', 'Current Electronic Eligibility Verification (271)', 'Patient Signed Coordination Statement']
    ),
    # CARC 197: Precertification / Prior Authorization Absent
    '197': (
        'Authorization missing',
        'Audit pre-service records for authorization reference numbers or referral orders. If urgent/emergent, invoke retrospective authorization exception under payer clinical policy.',
        ['Original Prior Authorization Request & Timestamp', 'Payer Authorization Approval / Reference Number', 'Emergency / Urgent Care Medical Record Justification', 'Attending Physician Peer-to-Peer Review Request']
    ),
    'CO-197': (
        'Authorization missing',
        'Verify existing pre-auth confirmation number or request expedited retroactive authorization based on medical urgency and physician clinical certification.',
        ['Original Prior Authorization Request & Timestamp', 'Payer Authorization Approval / Reference Number', 'Emergency / Urgent Care Medical Record Justification', 'Attending Physician Peer-to-Peer Review Request']
    ),
    # CARC 252: Missing Documentation
    '252': (
        'Missing documentation',
        'Remit requested clinical notes, operative reports, diagnostic studies, or signed treatment plans matching the specific claim line.',
        ['Physician Progress Notes and Treatment Plan', 'Diagnostic Test Results with Interpretation', 'Physician Signature and Attestation', 'Itemized Charge Description']
    ),
    'CO-252': (
        'Missing documentation',
        'Remit requested clinical notes, operative reports, diagnostic studies, or signed treatment plans matching the specific claim line.',
        ['Physician Progress Notes and Treatment Plan', 'Diagnostic Test Results with Interpretation', 'Physician Signature and Attestation', 'Itemized Charge Description']
    ),
    # Additional CARCs commonly seen on 835 remittances
    '4': (
        'Coding error',
        'Compare the billed modifier to the procedure code and payer modifier policy. If a required modifier was omitted or wrong, submit a corrected claim (frequency code 7) rather than a narrative appeal.',
        ['Itemized Superbill with CPT/HCPCS & Modifiers', 'Procedure / Operative Note Supporting Modifier', 'Payer Modifier Policy Excerpt', 'Corrected Claim (CMS-1500 / 837P frequency code 7)']
    ),
    '11': (
        'Coding error',
        'Verify that the diagnosis supports the procedure under payer coverage policy. Correct the diagnosis pointer or ICD-10 specificity and resubmit as a corrected claim with supporting documentation.',
        ['Clinical Note Establishing Diagnosis', 'Corrected ICD-10 Diagnosis Pointer / Specificity', 'Payer Diagnosis-to-Procedure Coverage Policy', 'Corrected Claim (837P frequency code 7)']
    ),
    '15': (
        'Authorization missing',
        'Authorization number was missing, invalid, or did not apply to the billed service/provider. Locate the valid authorization, confirm it matches CPT, date and rendering provider, and resubmit or appeal with the reference.',
        ['Payer Authorization Approval / Reference Number', 'Authorization Dates, CPT and Provider Match Proof', 'Original Prior Authorization Request & Timestamp', 'Corrected Claim With Authorization Number (REF*G1)']
    ),
    '22': (
        'Incorrect payer',
        'Payer states another carrier may be primary under coordination of benefits. Verify COB order, obtain the primary EOB, and rebill in the correct sequence.',
        ['Coordination of Benefits (COB) Questionnaire', 'Primary Payer EOB / Remittance', 'Current Electronic Eligibility Verification (271)', 'Patient Signed Coordination Statement']
    ),
    '27': (
        'Incorrect payer',
        'Expenses incurred after coverage terminated. Re-verify eligibility on the date of service (270/271) and identify any successor coverage or retroactive reinstatement.',
        ['Eligibility Verification (271) for Date of Service', 'Coverage Effective / Termination Dates', 'Successor or Retroactive Coverage Documentation', 'Patient Insurance Card Scan (Front & Back)']
    ),
    '31': (
        'Incorrect payer',
        'Patient could not be identified as the payer\'s insured. Confirm member ID, name and date-of-birth match the payer file and rebill with corrected demographics.',
        ['Insurance Card Scan (Front & Back)', 'Current Electronic Eligibility Verification (271)', 'Corrected Subscriber / Member ID Demographics', 'Patient Registration Record']
    ),
    '55': (
        'Non-covered charge',
        'Service deemed experimental/investigational. Appeal with peer-reviewed evidence, FDA status, and a physician statement of medical necessity for this patient.',
        ['Peer-Reviewed Clinical Literature Supporting Efficacy', 'FDA Approval / Clearance Status', 'Physician Letter of Medical Necessity', 'Summary Plan Description (SPD) Benefit Excerpt']
    ),
    '119': (
        'Non-covered charge',
        'Benefit maximum for the period was reached. Verify accumulators against the benefit plan and confirm the maximum was applied correctly; limited appeal options if applied properly.',
        ['Summary Plan Description (SPD) Benefit Excerpt', 'Benefit Accumulator / Maximum History', 'Prior Remittance Advice / EOB History', 'Medical Exception Request (if clinically supported)']
    ),
    '204': (
        'Non-covered charge',
        'Service not covered under the patient\'s current benefit plan. Verify plan coverage, request benefit exception where clinically justified, and review the patient notice on file.',
        ['Summary Plan Description (SPD) Benefit Excerpt', 'Prior Authorization / Pre-Determination File', 'Physician Letter of Medical Necessity', 'Signed Advance Beneficiary Notice (ABN) if Applicable']
    ),
    # Patient responsibility (deductible / coinsurance / copay) — not an appeal
    '1': ('Patient responsibility', 'Deductible applied. This is patient responsibility, not a payer denial: bill the patient per contract and do not appeal.', ['Patient Statement', 'Remittance Advice (835) Showing Deductible Application']),
    '2': ('Patient responsibility', 'Coinsurance applied. This is patient responsibility, not a payer denial: bill the patient per contract and do not appeal.', ['Patient Statement', 'Remittance Advice (835) Showing Coinsurance Application']),
    '3': ('Patient responsibility', 'Copayment applied. This is patient responsibility, not a payer denial: collect from the patient and do not appeal.', ['Patient Statement', 'Remittance Advice (835) Showing Copay Application']),
    # Contractual adjustment — not an appeal
    '45': ('Contractual adjustment', 'Charge exceeds the contracted fee schedule. This is an expected contractual write-down; only review if the allowed amount looks wrong versus the contract.', ['Payer Fee Schedule / Contract Rate', 'Remittance Advice (835) Showing Allowed Amount']),
}

BENCHMARK_REVERSAL_RATES = {
    'Coding error': 78,
    'Patient responsibility': 5,
    'Contractual adjustment': 5,
    'Missing information': 88,
    'Missing documentation': 88,
    'Duplicate claim': 82,
    'Incorrect payer': 80,
    'Authorization missing': 68,
    'Medical necessity': 62,
    'Bundled service': 58,
    'Timely filing': 54,
    'Non-covered charge': 42,
    'Other / needs review': 45
}

STATUTORY_CITATIONS = {
    'Medical necessity': 'CMS Medicare Benefit Policy Manual Ch. 15 §50; ERISA 29 C.F.R. § 2560.503-1; AMA Clinical Guidelines',
    'Authorization missing': 'AMA Prior Authorization Reform Principles; Payer Provider Manual §12; State Prompt Payment Rules',
    'Timely filing': 'ACA 42 C.F.R. § 424.44; State Insurance Code Prompt Filing Laws; Clearinghouse EDI Acceptance Standard',
    'Missing information': 'HIPAA Electronic Transaction Standards 45 C.F.R. Part 162; CMS-1500 Billing Regulations',
    'Missing documentation': 'HIPAA Electronic Transaction Standards 45 C.F.R. Part 162; CMS-1500 Billing Regulations',
    'Duplicate claim': 'CMS NCCI Policy Manual Ch. 1; AMA CPT Modifier -59/-X{EPSU} Clarification Guidelines',
    'Bundled service': 'CMS National Correct Coding Initiative (NCCI); AMA CPT Principles of Coding §4',
    'Incorrect payer': 'NAIC Coordination of Benefits Model Regulation; Medicare Secondary Payer (MSP) Manual',
    'Non-covered charge': 'ERISA Summary Plan Description Requirements 29 U.S.C. § 1022',
    'Coding error': 'CMS-1500 / 837P Billing Instructions; AMA CPT Modifier Guidelines; Corrected Claim (Frequency Code 7) Rules',
    'Patient responsibility': 'Provider-Payer Contract Patient Cost-Sharing Provisions',
    'Contractual adjustment': 'Provider-Payer Contract Fee Schedule Terms',
}


def lookup_payer_dispatch(payer_name: str) -> Dict[str, Any]:
    """Retrieves verified payer submission channels, portal, fax, and mailing address."""
    p_norm = (payer_name or '').strip().lower()
    for key, info in PAYER_DISPATCH_DIRECTORY.items():
        if key in p_norm:
            return info

    return {
        'payer_name': payer_name or 'Insurance Payer',
        'appeals_portal': 'https://www.availity.com',
        'appeals_fax': 'Check Payer Provider Portal',
        'appeals_address': 'Payer Claims Appeals & Grievance Department',
        'timely_filing_limit_days': 180,
        'appeal_window_days': 180,
        'notes': 'Verify payer-specific submission rules on the Explanation of Benefits / Remittance Advice.'
    }


def assess(claim) -> Dict[str, Any]:
    """
    Evaluates claim details and returns a deterministic, multi-factor analysis:
    - Primary denial category and rule reasoning
    - Actionable recommendations and required clinical evidence
    - Multi-dimensional priority score (financial exposure + deadline urgency + recoverability)
    - Reversal probability benchmark and statutory legal citations
    - Verified Payer Submission Dispatch details (fax, portal, address)
    """
    raw = str(getattr(claim, 'denial_code', '') or '').strip().upper()
    key = raw if raw in RULES else raw.split('-')[-1]

    category, action, docs = RULES.get(
        key,
        (
            'Other / needs review',
            'Review the full remittance advice, payer clinical policy, and appeal rights with billing staff.',
            ['Original claim', 'Remittance advice', 'Applicable payer policy']
        )
    )

    amt = float(getattr(claim, 'denied_amount', 0) or 0)
    deadline_str = getattr(claim, 'appeal_deadline', None)
    days = None
    urgency_tier = 'NONE'

    if deadline_str:
        try:
            deadline_date = date.fromisoformat(deadline_str[:10])
            days = (deadline_date - date.today()).days
            if days < 0:
                urgency_tier = 'EXPIRED'
            elif days <= 7:
                urgency_tier = 'CRITICAL'
            elif days <= 21:
                urgency_tier = 'URGENT'
            elif days <= 45:
                urgency_tier = 'MONITOR'
            else:
                urgency_tier = 'COMFORTABLE'
        except Exception:
            days = None

    # Operational priority score calculation
    amt_score = min(40, round(min(amt / 100, 40)))

    if days is not None:
        if days < 0:
            urgency_score = 10
        elif days <= 7:
            urgency_score = 35
        elif days <= 21:
            urgency_score = 25
        elif days <= 45:
            urgency_score = 15
        else:
            urgency_score = 5
    else:
        urgency_score = 15

    reversal_pct = BENCHMARK_REVERSAL_RATES.get(category, 50)
    rule_score = round(reversal_pct * 0.25)

    total_priority = min(100, max(1, amt_score + urgency_score + rule_score))

    if total_priority >= 75:
        priority_label = 'CRITICAL'
    elif total_priority >= 50:
        priority_label = 'HIGH'
    elif total_priority >= 30:
        priority_label = 'MEDIUM'
    else:
        priority_label = 'LOW'

    citation = STATUTORY_CITATIONS.get(category, 'Standard Health Plan Claims Adjudication Standards')
    dispatch = lookup_payer_dispatch(getattr(claim, 'payer', ''))

    if days is not None and days < 0:
        action = f"Recorded appeal deadline ({deadline_str}) has passed. Verify actual appeal rights and good-cause exceptions with the payer; do not assume appeal eligibility. " + action

    return {
        'category': category,
        'recommended_action': action,
        'required_documents': docs,
        'priority_score': total_priority,
        'priority_label': priority_label,
        'urgency_tier': urgency_tier,
        'days_to_deadline': days,
        'benchmark_reversal_rate': reversal_pct,
        'statutory_citation': citation,
        'payer_dispatch': dispatch,
        'explanation': f"Rule based on entered denial code {raw or '(missing)'}, denied amount ${amt:,.2f}, and entered deadline. Priority is operational only, not likelihood of appeal success.",
        'method': 'transparent_rules_v1'
    }


def _excerpt_text(ev: Dict[str, Any], limit: int = 150) -> str:
    text = f"{ev.get('before', '')}{ev.get('match', '')}{ev.get('after', '')}".strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _evidence_exhibit(radar: Dict[str, Any] | None):
    """Renders the Evidence Gap Radar result as (evidence_block, gap_checklist_lines)."""
    if not radar:
        return '', ''
    lines = [
        '',
        '',
        'EVIDENCE MAP (auto-generated from chart review — verify every line before sending):',
        f"  Chart evidence coverage: {radar.get('coverage', 0)}/100 ({radar.get('verdict', 'n/a')})",
    ]
    supported = [c for c in radar.get('criteria', []) if c['status'] == 'MET']
    partial = [c for c in radar.get('criteria', []) if c['status'] == 'PARTIAL']
    if supported:
        lines.append('  SUPPORTED BY CHART:')
        for c in supported:
            suffix = f" — \"{_excerpt_text(c['evidence'][0])}\"" if c.get('evidence') else ''
            lines.append(f"   [x] {c['label']}{suffix}")
    if partial:
        lines.append('  PARTIALLY SUPPORTED (strengthen or soften related claims):')
        for c in partial:
            lines.append(f"   [~] {c['label']} — {c['detail']}")
    block = '\n'.join(lines)

    open_gaps = [g for g in radar.get('gap_plan', []) if g['status'] == 'MISSING'][:6]
    gap_lines = ''.join(
        f"[ ] Close evidence gap before sending: {g['label']} (owner: {g['owner']}).\n" for g in open_gaps
    )
    return block, gap_lines


def draft_appeal(claim, analysis, level: int = 1, radar: Dict[str, Any] | None = None) -> str:
    """
    Generates a formal, professional medical appeal draft ready for clinical human review.
    Supports Level 1 Reconsideration and Level 2 Independent / External Escalation.
    When an Evidence Gap Radar result is supplied, an evidence map and open-gap checklist are included.
    """
    today_str = date.today().strftime("%B %d, %Y")
    citation = analysis.get('statutory_citation', 'Applicable State and Federal Claims Regulations')
    checklist_text = '\n'.join([f"  [ ] {doc}" for doc in analysis.get('required_documents', [])])
    evidence_block, gap_lines = _evidence_exhibit(radar)
    dispatch = analysis.get('payer_dispatch', {})

    if level == 2:
        return f"""DRAFT — HUMAN REVIEW REQUIRED — NOT SUBMITTED
================================================================================
LEVEL 2 FORMAL ESCALATION APPEAL & NOTICE OF DISPUTE
Date: {today_str}

TO:
  External Appeals & Grievance Review Committee / Medical Director
  {claim.payer}
  Submission Channel: {dispatch.get('appeals_portal', 'Payer Portal')} | Fax: {dispatch.get('appeals_fax', 'N/A')}
  Mailing Address:    {dispatch.get('appeals_address', 'Payer Headquarters')}

FROM:
  Clinical Revenue Cycle Operations & Physician Leadership
  {claim.clinic_name}

RE: LEVEL 2 FORMAL ADVERSE DETERMINATION APPEAL & REQUEST FOR EXTERNAL REVIEW
  Claim Reference:     {claim.claim_number}
  Date of Service:     {claim.service_date}
  Denial Code:         {claim.denial_code}
  Denial Category:     {analysis['category']}
  Disputed Amount:     ${claim.denied_amount:,.2f}
  Level 1 Status:      Upheld / Adverse Determination Disputed

Dear Medical Director and Appeals Committee:

This letter constitutes a formal LEVEL 2 ESCALATION APPEAL and notice of provider dispute under applicable state prompt-payment statutes and federal ERISA regulations (29 U.S.C. § 1133). 

1. STATEMENT OF ESCALATED DISPUTE:
Our initial Level 1 request for reconsideration regarding Claim #{claim.claim_number} was improperly upheld under code {claim.denial_code} ("{analysis['category']}"). We have conducted an executive clinical review of the patient's medical records and verified standard-of-care medical necessity. The continuous non-payment of this legitimately authorized and rendered service violates contractual payment provisions.

2. DETAILED CLINICAL JUSTIFICATION & LEGAL CITATIONS:
{analysis['recommended_action']}

This dispute is governed by:
- {citation}
- Federal ERISA 29 C.F.R. § 2560.503-1 (Full and Fair Review Mandate)
- State Prompt Pay Act Requirements (Requiring payment with statutory interest upon wrongful denial)

3. ENCLOSED EVIDENCE PACKET:
{checklist_text}
  [ ] Copy of Initial Level 1 Claim Submission and Prior Denial EOB
  [ ] Attending Physician Formal Affidavit of Medical Necessity{evidence_block}

4. DEMAND FOR RELIEF:
We formally demand that {claim.payer} overturn this denial immediately and issue payment in the full amount of ${claim.denied_amount:,.2f} within thirty (30) days. Failing satisfactory resolution, this claim will be submitted to the State Department of Insurance and Independent External Medical Review.

Sincerely,

___________________________________________________
Chief Medical Officer / Authorized Clinic Representative
{claim.clinic_name}

================================================================================
MANDATORY HUMAN REVIEW CHECKLIST (Level 2 Escalation):
[ ] Confirm prior Level 1 denial letter was received and logged.
[ ] Attending physician signed clinical attestation attached.
[ ] Verify payer external review filing deadline.
{gap_lines}Do not transmit this escalation without executive clinical sign-off.
"""

    return f"""DRAFT — HUMAN REVIEW REQUIRED — NOT SUBMITTED
================================================================================
FORMAL MEDICAL CLAIM APPEAL & REQUEST FOR RECONSIDERATION (LEVEL 1)
Date: {today_str}

TO:
  Appeals and Grievances Department
  {claim.payer}
  Submission Channel: {dispatch.get('appeals_portal', 'Payer Portal')} | Fax: {dispatch.get('appeals_fax', 'N/A')}
  Mailing Address:    {dispatch.get('appeals_address', 'Payer Headquarters')}

FROM:
  Billing & Revenue Operations
  {claim.clinic_name}

RE: Formal Appeal of Adverse Benefit Determination
  Claim Number:        {claim.claim_number}
  Date of Service:     {claim.service_date}
  Denial Code:         {claim.denial_code}
  Denial Category:     {analysis['category']}
  Disputed Amount:     ${claim.denied_amount:,.2f}
  Appeal Deadline:     {getattr(claim, 'appeal_deadline', None) or 'Not Specified'}

Dear Claims Adjudication & Appeals Committee,

On behalf of {claim.clinic_name}, we are formally submitting this first-level appeal and request for reconsideration regarding the denial of claim #{claim.claim_number} for services rendered on {claim.service_date}.

1. STATEMENT OF DISPUTE:
The claim was denied under code {claim.denial_code} citing: "{analysis['category']}". We have reviewed the adjudication against the clinical documentation, payer guidelines, and established medical standards. We respectfully submit that the service rendered was medically necessary, accurately coded, and eligible for full payment under contract terms.

2. BASIS FOR RECONSIDERATION & CORRECTIVE ACTION:
{analysis['recommended_action']}

3. REGULATORY AND CONTRACTUAL GOVERNANCE:
This appeal is governed by standard provider contract provisions and applicable statutes, including:
{citation}

4. ENCLOSED CLINICAL & BILLING EVIDENCE:
In accordance with your appeals protocol, the following verified records are attached:
{checklist_text}{evidence_block}

5. REQUEST FOR RESOLUTION:
Based on the enclosed clinical documentation and standard billing guidelines, we request that you overturn this denial and issue full reimbursement in the amount of ${claim.denied_amount:,.2f}. If this determination is upheld, please furnish a specific written explanation identifying the exact clinical criteria, Local/National Coverage Determination (LCD/NCD), or contractual policy provision relied upon.

Sincerely,

___________________________________________________
Authorized Clinic Billing Representative / Clinician
{claim.clinic_name}

================================================================================
MANDATORY HUMAN REVIEW CHECKLIST (Must be completed prior to physical submission):
[ ] Confirm patient identity and privacy safeguards (Minimum Necessary standard).
[ ] Confirm exact procedure codes and billed amounts match itemized billing ledger.
[ ] Verify payer appeal submission address (Portal / Certified Mail / Fax).
[ ] Verify timely filing limit has not expired.
[ ] Physician or clinical supervisor signature obtained.
{gap_lines}Do not transmit this draft without human review and verification.
"""
