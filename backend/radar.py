"""
Evidence Gap Radar — chart-vs-policy evidence audit for denied claims.

Given a de-identified clinical note and the denial category, the radar:
  1. Checks the note against a category-specific criteria template (the kind of
     elements payer medical policies and LCD/NCD-style rules ask for).
  2. Marks each criterion MET / PARTIAL / MISSING with the exact supporting excerpt,
     handling negation ("no prior physical therapy") and numeric thresholds
     ("4 weeks documented; policy expects >= 6").
  3. Scores coverage per axis (radar chart) and overall, with a transparent heuristic
     that blends the category benchmark reversal rate with chart coverage.
  4. Ranks a gap-closure plan by critical-first, then marginal win-rate / dollar gain.

Everything is deterministic and explainable. Criteria are generic templates, not a
substitute for the payer's current medical policy — always verify before filing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern, Tuple

from backend.engine import BENCHMARK_REVERSAL_RATES

METHOD = 'evidence_radar_v1'
DISCLAIMER = (
    'Criteria are generic policy-pattern templates, and win likelihood is an illustrative '
    'heuristic (benchmark reversal rate adjusted by chart coverage), not a prediction. '
    'Verify against the payer\'s current medical policy and have a clinician review before filing.'
)

# --------------------------------------------------------------------------------------
# Regex building blocks (patterns are compiled case-insensitive; use (?-i:...) to opt out)
# --------------------------------------------------------------------------------------
_DATE = (
    r'(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})'
)
_ICD_DEC = r'(?-i:\b[A-TV-Z]\d{2}\.[A-Z0-9]{1,4}\b)'
_THERAPY = (
    r'(?:physical\s+therapy|(?-i:PT)|physiotherapy|chiropractic|NSAIDs?|ibuprofen|naproxen|'
    r'acetaminophen|analgesics?|muscle\s+relaxants?|home\s+exercise\w*|exercise\s+program|'
    r'bracing|brace|steroid\s+(?:injection|taper)|injections?|activity\s+modification|TENS|'
    r'acupuncture|medications?|conservative\s+(?:therapy|treatment|management|care))'
)
_FAIL = (
    r'(?:fail\w*|no\s+(?:significant\s+)?(?:relief|improvement|benefit|response)|'
    r'minimal\s+(?:relief|improvement|benefit)|little\s+(?:relief|improvement|benefit)|'
    r'without\s+(?:relief|improvement)|unsuccessful|inadequate|insufficient|persist\w*|'
    r'despite|refractory|not\s+(?:responded|responding|improved|improving)|'
    r'did\s+not\s+(?:respond|improve|help))'
)

_NEG = re.compile(
    r"\b(?:no|not|denies|without|never|absent|lacks?|lacking|none|"
    r"unable\s+to\s+(?:obtain|locate|find)|has\s+not|have\s+not|did\s+not|didn't|"
    r"wasn't|was\s+not|isn't|missing|pending|awaiting|n/a)\b[^.;\n]{0,25}$",
    re.I,
)

_NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
}
_NUM = r'(?:\d+(?:\.\d+)?|' + '|'.join(_NUM_WORDS) + r')'
_DUR_RE = re.compile(
    r'(' + _NUM + r')(?:\s*(?:-|to)\s*(' + _NUM + r'))?\s*[- ]?'
    r'(weeks?|wks?|months?|mos?|years?|yrs?|days?)\b',
    re.I,
)
_STATUS_VALUE = {'MET': 1.0, 'PARTIAL': 0.5, 'MISSING': 0.0}
_EFFORT_ORDER = {'low': 0, 'medium': 1, 'high': 2}


# --------------------------------------------------------------------------------------
# Criteria model
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Criterion:
    id: str
    axis: str
    label: str
    weight: int
    strong: Tuple[Pattern, ...]
    weak: Tuple[Pattern, ...] = ()
    critical: bool = False
    optional: bool = False       # excluded from scoring when absent (only counts if present)
    needs_duration: bool = False  # strong hit must also state a duration >= policy weeks
    negatable: bool = True       # "no X" contradicts the criterion
    effort: str = 'low'
    owner: str = 'Clinician'
    specific: str = ''           # what a complete entry looks like (gap action)
    why: str = ''


def _compile(patterns) -> Tuple[Pattern, ...]:
    return tuple(re.compile(p, re.I) if isinstance(p, str) else p for p in patterns)


def _c(id: str, axis: str, label: str, weight: int, strong, weak=(), **kw) -> Criterion:
    return Criterion(id=id, axis=axis, label=label, weight=weight,
                     strong=_compile(strong), weak=_compile(weak), **kw)


def _attest(weight: int = 2, critical: bool = True,
            label: str = 'Provider signature / attestation on record') -> Criterion:
    return _c(
        'attestation', 'Attestation', label, weight,
        strong=(
            r'\b(?:electronically\s+signed|e-?signed|signed\s+by|signature|attest\w*|'
            r'authenticated\s+by|digitally\s+signed)\b',
            r'\bNPI\s*[:#]?\s*\d{10}\b',
        ),
        weak=(r'\bDr\.', r'(?-i:\b(?:MD|DO|NP|PA-C)\b)', r'\b(?:physician|provider|clinician)\b'),
        critical=critical, effort='low', owner='Clinician',
        specific='Add the signed/authenticated note showing provider name, credentials and 10-digit NPI.',
        why='Payers routinely reject unsigned or unauthenticated records on review.',
    )


# --------------------------------------------------------------------------------------
# Criteria templates by denial category
# --------------------------------------------------------------------------------------
_MEDICAL_NECESSITY = [
    _c('diagnosis', 'Diagnosis', 'Diagnosis documented with ICD-10', 2,
       strong=(_ICD_DEC, r'\b(?:ICD-?10|dx|diagnosis)\b[^.\n]{0,30}(?-i:\b[A-TV-Z]\d{2}\b)'),
       weak=(r'\b(?:diagnos\w+|assessment|impression)\b',),
       critical=True, effort='low', owner='Clinician',
       specific='State the working diagnosis with its ICD-10 code in the assessment.',
       why='Medical-necessity policies are diagnosis-driven; no coded diagnosis means no coverage match.'),
    _c('symptoms', 'Clinical Picture', 'Symptom chronology and severity', 2,
       strong=(
           r'\b(?:onset|since|worsen\w*|progressive|chronic|acute)\b',
           r'\bfor\s+(?:the\s+past\s+)?' + _NUM + r'\s*(?:days?|weeks?|months?|years?)\b',
           r'\b' + _NUM + r'\s*(?:days?|weeks?|months?|years?)\s+(?:of|history)\b',
           r'\b\d{1,2}\s*/\s*10\b',
       ),
       weak=(r'\b(?:pain|symptom\w*|complain\w*|presents?)\b',),
       negatable=False, effort='low', owner='Clinician',
       specific='Document onset/duration and a severity score (e.g. 7/10) in the HPI.',
       why='Duration and severity establish that the condition is persistent enough to warrant the service.'),
    _c('red_flags', 'Clinical Picture', 'Red-flag / progression screening documented', 1,
       strong=(r'\b(?:red\s+flags?|cauda\s+equina|progressive\s+(?:weakness|deficit|neurolog\w+)|'
               r'bowel|bladder|saddle|weight\s+loss|fever|night\s+pain|trauma|malignan\w+)\b',),
       optional=True, negatable=False, effort='low', owner='Clinician',
       specific='Document red-flag screening results (or their absence) where clinically relevant.',
       why='Red flags can bypass conservative-therapy requirements entirely.'),
    _c('conservative', 'Conservative Tx', 'Conservative therapy tried and failed', 3,
       strong=(r'\b' + _THERAPY + r'\b[^.\n]{0,100}\b' + _FAIL,
               r'\b' + _FAIL + r'\b[^.\n]{0,60}\b' + _THERAPY + r'\b'),
       weak=(r'\b' + _THERAPY + r'\b',),
       critical=True, needs_duration=True, effort='medium', owner='Clinician',
       specific='List each conservative treatment, its dates/duration, and the outcome (failed / inadequate response).',
       why='Most elective-service policies require a documented trial and failure of conservative care first.'),
    _c('exam', 'Objective Findings', 'Objective examination findings', 2,
       strong=(r'\b(?:straight[- ]leg\s+raise|SLR|reflex\w*|[0-5]\s*/\s*5|range\s+of\s+motion|'
               r'(?-i:ROM)|sensory|neurolog\w+\s+(?:exam|deficit|findings?)|tender\w*|atrophy|gait|'
               r'palpat\w+|edema|swelling|erythema|crepitus|motor\s+(?:strength|deficit))\b',),
       weak=(r'\b(?:exam\w*|physical\s+exam\w*|on\s+examination)\b',),
       negatable=False, effort='low', owner='Clinician',
       specific='Add measurable exam findings (strength grades, reflexes, ROM, positive provocative tests).',
       why='Reviewers look for objective, reproducible findings — not just patient-reported symptoms.'),
    _c('diagnostics', 'Objective Findings', 'Prior diagnostic results documented', 2,
       strong=(r'\b(?:x-?ray|radiograph\w*|MRI|(?-i:CT)(?:\s+scan)?|EMG|NCS|ultrasound|sonogram|labs?|'
               r'laborator\w+|biopsy|culture|ECG|EKG|pathology)\b[^.\n]{0,80}\b(?:show\w*|reveal\w*|'
               r'demonstrat\w*|result\w*|finding\w*|impression|negative|positive|normal|abnormal|'
               r'consistent\s+with|evidence\s+of|noted)\b',),
       weak=(r'\b(?:x-?ray|radiograph\w*|EMG|ultrasound|labs?|laborator\w+|biopsy|pathology|ECG|EKG)\b',),
       negatable=False, effort='medium', owner='Clinician',
       specific='Reference prior imaging/lab/EMG results with date and key impression.',
       why='Prior results show the requested service is the logical next step, not a first-line test.'),
    _c('function', 'Functional Impact', 'Functional impairment documented', 2,
       strong=(r'\b(?:unable|difficulty|limit\w*|impair\w*|cannot|can\'t|restrict\w*)\b[^.\n]{0,80}\b(?:work\w*|'
               r'walk\w*|stand\w*|sit\w*|sleep\w*|ADLs?|daily|lift\w*|climb\w*|dress\w*)\b',
               r'\b(?:ADLs?|oswestry|disability\s+index|functional\s+(?:limitation|status|score))\b'),
       weak=(r'\b(?:function\w*|activities)\b',),
       effort='low', owner='Clinician',
       specific='Describe how the condition limits work, walking, sleep or daily activities (or a validated score).',
       why='Functional loss supports urgency and necessity beyond a diagnosis alone.'),
    _c('rationale', 'Clinical Rationale', 'Test/treatment tied to management plan', 2,
       strong=(r'\b(?:to|will|would|in\s+order\s+to)\s+(?:help\s+)?(?:change|guide|determine|inform|direct|plan|'
               r'decide|rule\s+out|evaluate|assess)\b[^.\n]{0,80}\b(?:management|treatment|surgery|surgical|'
               r'plan|therapy|intervention|diagnosis|decision)\b',
               r'\b(?:surgical\s+(?:candidate|evaluation|consult\w*|planning)|pre-?operative)\b'),
       weak=(r'\b(?:plan|recommend\w*|order\w*|request\w*|refer\w*)\b',),
       effort='low', owner='Clinician',
       specific='State how the result will change management (e.g. surgical vs. continued conservative care).',
       why='Policies require that the service will alter the treatment decision.'),
    _c('guideline', 'Clinical Rationale', 'Guideline / policy criteria referenced', 1,
       strong=(r'\b(?:ACR|appropriateness\s+criteria|LCD\s*L?\d+|NCD\s*\d|InterQual|MCG|Milliman|NASS|AAOS|'
               r'clinical\s+(?:practice\s+)?guideline\w*|guideline\w*)\b',),
       weak=(r'\b(?:standard\s+of\s+care|evidence[- ]based|per\s+(?:policy|criteria))\b',),
       effort='medium', owner='Biller / Appeals',
       specific='Cite the specific guideline, LCD/NCD, or payer policy criteria the case satisfies.',
       why='Mapping the case to named criteria lets the reviewer approve without re-deriving necessity.'),
    _attest(),
]

_AUTHORIZATION = [
    _c('auth_number', 'Authorization Trail', 'Authorization / referral reference number', 3,
       strong=(r'\b(?:auth\w*|pre-?auth\w*|precert\w*|referral)\b[^.\n]{0,30}?[:#]\s*(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,}',
               r'\b(?:auth\w*|precert\w*|referral)\s+(?:number|no\.?|id)\b[^.\n]{0,15}(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,}'),
       weak=(r'\b(?:auth\w*|precert\w*|referral)\b',),
       critical=True, effort='medium', owner='Front desk / Biller',
       specific='Add the authorization/referral reference number exactly as issued by the payer.',
       why='A valid reference number is usually the single fact that overturns a CO-197 denial.'),
    _c('auth_request', 'Authorization Trail', 'Request date / submission method', 2,
       strong=(r'\b(?:request\w*|submitt?ed|called|faxed|portal)\b[^.\n]{0,60}' + _DATE,),
       weak=(r'\b(?:request\w*|submitt?ed)\b',),
       effort='medium', owner='Front desk / Biller',
       specific='Record when and how authorization was requested (date, portal/fax/phone).',
       why='Request timestamps prove diligence and support retro-authorization exceptions.'),
    _c('auth_match', 'Authorization Trail', 'Authorized service matches billed service', 2,
       strong=(r'\bCPT\s*[:#]?\s*\d{5}\b', r'\b\d{5}\b[^.\n]{0,40}\b(?:authorized|approved)\b',
               r'\b(?:authorized|approved)\b[^.\n]{0,60}\b\d{5}\b'),
       weak=(r'\b(?:CPT|procedure\s+code)\b',),
       effort='low', owner='Biller / Appeals',
       specific='Show the authorized CPT/date/site against the billed CPT/date/site.',
       why='Mismatched codes or dates are the second most common authorization-denial trigger.'),
    _c('payer_contact', 'Payer Contact', 'Payer representative / call reference', 1,
       strong=(r'\b(?:spoke|call(?:ed)?|rep(?:resentative)?|agent|confirmation)\b[^.\n]{0,40}\b(?:with|ref\w*|#|number)\b',),
       weak=(r'\b(?:called|phone)\b',),
       effort='low', owner='Front desk / Biller',
       specific='Log the representative name or call reference number and date.',
       why='A call reference gives the appeals reviewer something verifiable to look up.'),
    _c('urgency', 'Urgency Justification', 'Medical urgency / emergency justification', 3,
       strong=(r'\b(?:emergen\w+|urgent\w*|life-?threatening|acute|stat|ED\s+visit|after-?hours|'
               r'could\s+not\s+wait|delay\w*\s+(?:would|could))\b',),
       weak=(r'\b(?:severe|worsening|deteriorat\w+)\b',),
       critical=True, negatable=False, effort='low', owner='Clinician',
       specific='Document why the service could not wait for prior authorization (clinical urgency).',
       why='Retroactive authorization hinges on a clinician-certified urgency rationale.'),
    _c('necessity', 'Clinical Necessity', 'Clinical necessity statement', 2,
       strong=(r'\b(?:medically\s+necessary|medical\s+necessity|indicated|standard\s+of\s+care|failed)\b',),
       weak=(r'\bdiagnos\w+\b',), effort='low', owner='Clinician',
       specific='Add a clear statement of why the service was medically necessary for this patient.',
       why='Even with auth issues, necessity is evaluated alongside the administrative defect.'),
    _attest(),
]

_TIMELY_FILING = [
    _c('submission_proof', 'Submission Proof', 'Original submission date documented', 3,
       strong=(r'\b(?:submitt?\w*|filed|transmitt?\w*|sent|billed)\b[^.\n]{0,60}' + _DATE,),
       weak=(r'\b(?:submitt?\w*|filed|transmitt?\w*)\b',),
       critical=True, effort='medium', owner='Biller / Appeals',
       specific='State the date the claim was first submitted and by what channel (clearinghouse/portal).',
       why='Timely-filing denials turn on the first submission date, not the resubmission date.'),
    _c('clearinghouse', 'Clearinghouse Evidence', 'Clearinghouse / payer acceptance report', 3,
       strong=(r'\b(?:277(?:CA)?|999|TA1|acceptance\s+report|accepted\s+by\s+(?:the\s+)?(?:payer|clearinghouse)|'
               r'clearinghouse\s+(?:report|confirmation|acknowledg\w+))\b',),
       weak=(r'\b(?:clearinghouse|EDI|electronic)\b',),
       critical=True, effort='medium', owner='Biller / Appeals',
       specific='Attach the 277CA/999 acceptance report with timestamp and batch/claim identifiers.',
       why='An acceptance report is the contractual proof most payers honor for timely filing.'),
    _c('payer_ack', 'Clearinghouse Evidence', 'Payer claim control number (ICN/DCN) assigned', 2,
       strong=(r'\b(?:ICN|DCN|payer\s+claim\s+(?:control\s+)?(?:number|#)|acknowledg\w+\s+(?:of\s+)?receipt)\b',),
       weak=(r'\breceived\b',), effort='medium', owner='Biller / Appeals',
       specific='Record the payer-assigned ICN/DCN that confirms receipt.',
       why='A payer-issued control number proves the payer had the claim within the window.'),
    _c('delay_reason', 'Delay Justification', 'Delay justification (COB / retro-eligibility / payer error)', 2,
       strong=(r'\b(?:COB|coordination\s+of\s+benefits|primary\s+(?:payer|insurance)|retro(?:active)?\s+eligibility|'
               r'eligibility\s+(?:was\s+)?(?:updated|retro\w*)|payer\s+error|good\s+cause)\b',),
       optional=True, negatable=False, effort='medium', owner='Biller / Appeals',
       specific='Explain any qualifying delay (primary-payer EOB wait, retro eligibility, payer error).',
       why='Good-cause exceptions can extend the filing window when properly documented.'),
    _c('filing_limit', 'Contract Basis', 'Payer filing limit compared to timeline', 1,
       strong=(r'\b(?:filing\s+(?:limit|window|deadline)|timely\s+filing\s+(?:limit|window))\b[^.\n]{0,80}\b\d+\s*days\b',
               r'\bwithin\s+\d+\s*days\b'),
       weak=(r'\b(?:filing\s+limit|timely\s+filing)\b',), negatable=False,
       effort='low', owner='Biller / Appeals',
       specific='Quote the contract filing limit (in days) and show submission fell inside it.',
       why='Showing the arithmetic makes the reviewer\'s decision trivial.'),
    _attest(weight=1, critical=False, label='Billing manager attestation'),
]

_MISSING_INFO = [
    _c('denial_detail', 'Denial Understanding', 'Specific missing item / remark code identified', 2,
       strong=(r'\b(?:RARC|remark\s+code)\b[^.\n]{0,20}(?-i:\b[A-Z]{1,2}\d{1,3}\b)', r'(?-i:\b(?:MA|M|N)\d{1,3}\b)'),
       weak=(r'\b(?:missing|incomplete|requested|needed)\b',), negatable=False,
       effort='low', owner='Biller / Appeals',
       specific='Name the exact missing element and the RARC/remark code the payer cited.',
       why='Fixing the wrong gap wastes an appeal level — match the response to the payer\'s remark.'),
    _c('packet', 'Document Packet', 'Requested documents attached / enclosed', 3,
       strong=(r'\b(?:attach\w*|enclos\w+|included|submitt?ed|provided|resubmitt?ed)\b[^.\n]{0,60}\b(?:report|note|record|'
               r'superbill|invoice|summary|form|result|log|signature)\b',),
       weak=(r'\b(?:attach\w*|enclos\w+)\b',),
       critical=True, effort='medium', owner='Biller / Appeals',
       specific='List each document being attached in response to the payer\'s request.',
       why='The denial is only cured when the specific requested items accompany the corrected claim.'),
    _c('clinical_record', 'Clinical Record', 'Operative / procedure / progress note present', 2,
       strong=(r'\b(?:operative|procedure|progress|consult\w*|visit|clinic)\s+(?:report|note)\b',),
       weak=(r'\b(?:note|report)\b',), effort='low', owner='Clinician',
       specific='Reference the dated procedure/progress note that supports the billed service.',
       why='The clinical record is the source of truth the payer cross-checks against the claim.'),
    _c('coding', 'Coding Accuracy', 'CPT/HCPCS and ICD-10 verified against record', 2,
       strong=(r'\b(?:CPT|HCPCS)\s*[:#]?\s*[A-Z]?\d{4,5}\b', _ICD_DEC),
       weak=(r'\b(?:CPT|ICD\w*|HCPCS|code\w*)\b',), negatable=False,
       effort='low', owner='Biller / Appeals',
       specific='Confirm CPT/HCPCS and ICD-10 codes against the itemized superbill.',
       why='Coding mismatches are a frequent hidden cause behind generic "missing information" denials.'),
    _c('eligibility', 'Eligibility & ID', 'Member eligibility verified', 1,
       strong=(r'\b(?:eligib\w+|271|member\s+id|subscriber\s+id|coverage)\b[^.\n]{0,50}\b(?:verified|active|confirmed|effective)\b',),
       weak=(r'\b(?:eligib\w+|member)\b',), effort='low', owner='Front desk / Biller',
       specific='Record the eligibility verification (271 response date and active-coverage status).',
       why='Eligibility gaps masquerade as missing-information denials.'),
    _attest(),
]

_DUPLICATE = [
    _c('distinct_service', 'Distinctness', 'Services shown as separate / distinct', 3,
       strong=(r'\b(?:separate|distinct|different|second|independent|unrelated)\s+(?:session|encounter|site|lesion|'
               r'anatomic\w*|procedure|visit|incision|organ|time|service)\b',
               r'\b(?:left|right|bilateral)\b[^.\n]{0,40}\b(?:and|vs\.?|versus)\b[^.\n]{0,40}\b(?:left|right)\b'),
       weak=(r'\b(?:second|separate|bilateral)\b',), critical=True, effort='low', owner='Clinician',
       specific='State explicitly how the services differ (site, session, lesion, laterality).',
       why='A duplicate denial is overturned by proving the two lines are different services.'),
    _c('modifier', 'Coding', 'Distinct-service modifier justified', 3,
       strong=(r'\bmodifier\s*-?\s*(?:59|X[EPSU]|76|77|91|25|LT|RT)\b', r'(?-i:-(?:59|XE|XP|XS|XU|76|77)\b)'),
       weak=(r'\bmodifier\b',), critical=True, negatable=False, effort='low', owner='Biller / Appeals',
       specific='Name the modifier (59/XE/XP/XS/XU/76/77) being added and why it applies.',
       why='Without a distinct-service modifier, payer edits treat the second line as a duplicate.'),
    _c('timing_site', 'Distinctness', 'Time-of-day or anatomic site differentiators', 2,
       strong=(r'\b\d{1,2}:\d{2}\s*(?:am|pm)?\b', r'\b(?:AM|PM)\b', r'\blesion\s*#?\d', r'\bsite\s*[A-Z\d]\b'),
       weak=(r'\b(?:date\s+of\s+service|DOS)\b',), negatable=False, effort='low', owner='Clinician',
       specific='Add encounter times or lesion/site identifiers that differ between the two lines.',
       why='Concrete differentiators are easy for a reviewer to verify.'),
    _c('claim_history', 'Claim History', 'Prior claim / EOB comparison referenced', 2,
       strong=(r'\b(?:original|prior|previous|first)\s+(?:claim|EOB|remittance|ERA|payment)\b[^.\n]{0,60}(?:\d|#|number|paid|processed)',),
       weak=(r'\b(?:prior\s+claim|previous)\b',), effort='medium', owner='Biller / Appeals',
       specific='Cite the original claim number/EOB and show how this line differs.',
       why='Side-by-side comparison to the paid claim resolves most duplicate disputes quickly.'),
    _c('doc_entries', 'Documentation', 'Separate documentation entries per service', 2,
       strong=(r'\b(?:operative|procedure|progress)\s+(?:report|note)\b',), weak=(r'\bnote\b',),
       effort='low', owner='Clinician',
       specific='Reference the individual note entries documenting each service.',
       why='Distinct documentation entries corroborate that services were not billed twice.'),
    _attest(weight=1, critical=False),
]

_BUNDLED = [
    _c('separate_service', 'Distinctness', 'Separately identifiable service explained', 3,
       strong=(r'\b(?:separately\s+identifiable|significant(?:ly)?,?\s+separate|distinct|independent|above\s+and\s+beyond|'
               r'beyond\s+the\s+(?:usual|typical)|unrelated\s+to|new\s+problem|different\s+(?:problem|diagnosis|complaint))\b',),
       weak=(r'\b(?:separate|additional)\b',), critical=True, effort='low', owner='Clinician',
       specific='Explain what made the service separately identifiable from the primary procedure.',
       why='NCCI bundling is overridden only when the service is clearly separate and documented as such.'),
    _c('modifier', 'Coding', 'Modifier 25 / 59 (or X-modifier) justified', 3,
       strong=(r'\bmodifier\s*-?\s*(?:25|59|X[EPSU])\b', r'(?-i:-(?:25|59|XE|XP|XS|XU)\b)'),
       weak=(r'\bmodifier\b',), critical=True, negatable=False, effort='low', owner='Biller / Appeals',
       specific='State the modifier applied and the documentation that supports it.',
       why='The modifier is the mechanism that bypasses the NCCI procedure-to-procedure edit.'),
    _c('dx_linkage', 'Coding', 'Distinct diagnosis linked to each service', 2,
       strong=(r'\b(?:diagnosis\s+pointer|dx\s+pointer|linked\s+to\s+(?:ICD|diagnosis))\b',
               _ICD_DEC + r'[^.\n]{0,60}' + _ICD_DEC),
       weak=(_ICD_DEC,), negatable=False, effort='low', owner='Biller / Appeals',
       specific='Link a distinct ICD-10 diagnosis to each billed service line.',
       why='Different diagnoses per line are strong evidence the services were independent.'),
    _c('complexity', 'Complexity', 'Time / complexity of separate service documented', 2,
       strong=(r'\b\d{1,3}\s*(?:min|minutes)\b', r'\b(?:MDM|medical\s+decision[- ]making|moderate\s+complexity|'
               r'high\s+complexity|time\s+spent)\b'),
       weak=(r'\b(?:complex\w*|time)\b',), negatable=False, effort='low', owner='Clinician',
       specific='Document time spent and/or medical decision-making complexity for the separate service.',
       why='E/M-with-procedure disputes are decided on the documented work above the procedure itself.'),
    _c('ncci', 'Policy Basis', 'NCCI edit reviewed', 1,
       strong=(r'\b(?:NCCI|correct\s+coding\s+initiative|PTP\s+edit|CCI\s+edit)\b',),
       weak=(r'\b(?:bundl\w+|edit)\b',), negatable=False, effort='low', owner='Biller / Appeals',
       specific='Reference the specific NCCI PTP edit and the modifier indicator allowing bypass.',
       why='Citing the edit shows the claim was audited against the rule that triggered the denial.'),
    _attest(),
]

_INCORRECT_PAYER = [
    _c('coverage_id', 'Coverage Proof', 'Correct payer / active coverage identified', 3,
       strong=(r'\b(?:primary|secondary|active|current)\s+(?:payer|payor|insurance|coverage|plan|policy)\b[^.\n]{0,60}'
               r'\b(?:is|:|with|through)\b',),
       weak=(r'\b(?:payer|insurance)\b',), critical=True, effort='low', owner='Front desk / Biller',
       specific='Name the correct primary/secondary payer and policy that should have been billed.',
       why='Re-routing to the right payer is the entire cure for a CO-109 denial.'),
    _c('eligibility_271', 'Coverage Proof', 'Real-time eligibility (270/271) verification', 3,
       strong=(r'\b(?:271|270/271|eligibility\s+(?:check|verification|response|inquiry))\b',),
       weak=(r'\beligib\w+\b',), critical=True, effort='low', owner='Front desk / Biller',
       specific='Attach the 271 eligibility response with date of verification.',
       why='A dated 271 is objective proof of who covered the patient on the date of service.'),
    _c('coverage_dates', 'Coverage Proof', 'Effective / termination dates documented', 2,
       strong=(r'\b(?:effective|termination|termed|term\s+date|coverage\s+(?:start|end))\b[^.\n]{0,40}' + _DATE,),
       weak=(r'\b(?:effective|termed)\b',), effort='low', owner='Front desk / Biller',
       specific='Record coverage effective and termination dates for each policy.',
       why='Date-of-service must fall inside the coverage span of the payer being billed.'),
    _c('cob_order', 'COB Order', 'Coordination-of-benefits order determined', 2,
       strong=(r'\b(?:birthday\s+rule|COB|coordination\s+of\s+benefits|order\s+of\s+benefits|primary\s+policyholder|subscriber)\b',),
       weak=(r'\b(?:spouse|dependent)\b',), negatable=False, effort='medium', owner='Biller / Appeals',
       specific='Document how primary vs. secondary order was determined (birthday rule, policyholder, etc.).',
       why='Payers decide COB disputes on the order-of-benefits rules — show your work.'),
    _c('primary_eob', 'COB Order', 'Primary payer EOB attached (if billing secondary)', 2,
       strong=(r'\bprimary\b[^.\n]{0,40}\b(?:EOB|remit\w*|ERA|835|paid|denied)\b',),
       optional=True, negatable=False, effort='medium', owner='Biller / Appeals',
       specific='Attach the primary payer EOB/ERA when billing secondary.',
       why='Secondary payers pay on the primary\'s adjudication — no EOB, no payment.'),
    _c('patient_stmt', 'Patient Attestation', 'Patient COB statement / questionnaire', 1,
       strong=(r'\b(?:questionnaire|patient\s+(?:confirmed|stated|attested|signed)|signed\s+statement)\b',),
       negatable=False, effort='medium', owner='Front desk / Biller',
       specific='Attach the patient-signed COB questionnaire or statement.',
       why='A signed patient statement resolves ambiguity over other active coverage.'),
]

_NON_COVERED = [
    _c('plan_basis', 'Plan Basis', 'Plan document / benefit basis cited', 3,
       strong=(r'\b(?:SPD|summary\s+plan\s+description|benefit\s+(?:schedule|booklet|exception)|plan\s+(?:document|exclusion)|'
               r'certificate\s+of\s+coverage|EOC)\b',),
       weak=(r'\b(?:benefit|exclusion)\b',), critical=True, effort='medium', owner='Biller / Appeals',
       specific='Quote the plan language (SPD/EOC) and show the service is not actually excluded or qualifies for exception.',
       why='Non-covered denials are won on plan language, not clinical argument alone.'),
    _c('predetermination', 'Plan Basis', 'Prior authorization / predetermination on file', 2,
       strong=(r'\b(?:predetermination|pre-?auth\w*|authorization)\b[^.\n]{0,30}\b(?:approved|number|#|ref\w*)\b',),
       weak=(r'\b(?:predetermination|pre-?auth\w*|authorization)\b',), effort='medium', owner='Front desk / Biller',
       specific='Attach any prior authorization or predetermination approval for the service.',
       why='A prior approval is strong evidence the plan considered the service covered.'),
    _c('exception_basis', 'Clinical Justification', 'Medical exception / necessity justification', 3,
       strong=(r'\b(?:medically\s+necessary|exception|no\s+(?:effective\s+)?alternative|standard\s+of\s+care|'
               r'failed\s+(?:standard|conventional)|off-?label)\b',),
       weak=(r'\b(?:necessary|alternative)\b',), critical=True, effort='low', owner='Clinician',
       specific='Document why no covered alternative is clinically appropriate for this patient.',
       why='Plan exceptions require a clinician-signed statement that alternatives are inadequate.'),
    _c('literature', 'Evidence Base', 'Peer-reviewed evidence cited', 2,
       strong=(r'\b(?:peer[- ]reviewed|PMID\s*\d+|doi:|randomi[sz]ed|clinical\s+trial|meta-?analysis|FDA)\b',),
       weak=(r'\b(?:literature|evidence|study|journal)\b',), effort='high', owner='Clinician',
       specific='Cite 1–3 peer-reviewed studies (PMID/DOI) supporting efficacy for this indication.',
       why='Experimental/investigational exclusions are overturned with published efficacy evidence.'),
    _c('patient_notice', 'Patient Notice', 'Advance notice / financial responsibility documented', 1,
       strong=(r'\b(?:ABN|advance\s+beneficiary|waiver|financial\s+responsibility)\b',),
       optional=True, negatable=False, effort='low', owner='Front desk / Biller',
       specific='Attach signed ABN/waiver if one was obtained before service.',
       why='A signed notice shifts liability and can independently resolve the dispute.'),
    _attest(),
]

_GENERIC = [
    _MEDICAL_NECESSITY[0], _MEDICAL_NECESSITY[1], _MEDICAL_NECESSITY[4],
    _MEDICAL_NECESSITY[7], _attest(),
]

CRITERIA_BY_CATEGORY: Dict[str, List[Criterion]] = {
    'Medical necessity': _MEDICAL_NECESSITY,
    'Authorization missing': _AUTHORIZATION,
    'Timely filing': _TIMELY_FILING,
    'Missing information': _MISSING_INFO,
    'Missing documentation': _MISSING_INFO,
    'Duplicate claim': _DUPLICATE,
    'Bundled service': _BUNDLED,
    'Incorrect payer': _INCORRECT_PAYER,
    'Non-covered charge': _NON_COVERED,
}

# --------------------------------------------------------------------------------------
# Synthetic demo notes (deliberately incomplete so the radar has real gaps to find)
# --------------------------------------------------------------------------------------
SAMPLE_NOTES: Dict[str, str] = {
    'Medical necessity': (
        'Patient presents with 7 weeks of progressive low back pain radiating to the left leg, rated 7/10. '
        'Assessment: lumbar radiculopathy (M54.16). Completed 4 weeks of physical therapy and NSAIDs with minimal improvement. '
        'Exam shows positive straight leg raise on the left and 4/5 left dorsiflexion strength. '
        'Pain limits walking and prolonged sitting. MRI of the lumbar spine is requested to guide management.'
    ),
    'Authorization missing': (
        'Patient presented to the clinic with acute knee instability after a fall. Authorization was requested by phone. '
        'Procedure CPT 29881 performed the same day. The service was medically necessary due to a locked knee.'
    ),
    'Timely filing': (
        'Claim was submitted electronically through the clearinghouse on 2026-06-12. '
        'Billing staff believes the payer received it but no acceptance report has been located. '
        'The primary payer EOB delayed secondary billing.'
    ),
    'Missing information': (
        'Payer requested the operative report. Operative note is on file in the chart. '
        'CPT 27447 and ICD-10 M17.11 were billed. Eligibility was verified active at check-in.'
    ),
    'Duplicate claim': (
        'Excision performed on two different lesions during the same visit. '
        'Lesion #1 on the left forearm and lesion #2 on the right shoulder. Procedure note documents both.'
    ),
    'Bundled service': (
        'Established patient seen for new problem of hypertension and also received a joint injection. '
        'The E/M visit was significant and separately identifiable from the injection. Time spent 25 minutes.'
    ),
    'Incorrect payer': (
        'Patient reports a new employer plan. The previous insurance was terminated. '
        'Billing staff has not yet confirmed which payer is primary.'
    ),
    'Non-covered charge': (
        'Patient has failed standard treatment and no effective alternative exists. '
        'The therapy is medically necessary per the treating physician.'
    ),
}
_GENERIC_SAMPLE = (
    'Patient presents with chronic symptoms for 3 months. Assessment: documented condition (E11.9). '
    'Treatment plan recommended to guide management.'
)


def sample_note(category: str) -> str:
    return SAMPLE_NOTES.get(category, _GENERIC_SAMPLE)


# --------------------------------------------------------------------------------------
# PHI guard
# --------------------------------------------------------------------------------------
_PHI_PATTERNS: List[Tuple[str, Pattern]] = [
    ('SSN', re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
    ('phone number', re.compile(r'\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b')),
    ('email address', re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')),
    ('date of birth', re.compile(r'\b(?:DOB|date\s+of\s+birth|born)\b[^.\n]{0,12}\d', re.I)),
    ('medical record number', re.compile(r'\b(?:MRN|medical\s+record\s+(?:number|no\.?|#))\b[^.\n]{0,6}\d', re.I)),
]


def phi_findings(text: str) -> List[str]:
    """Return labels of PHI-like patterns found. The radar accepts de-identified text only."""
    return [label for label, pat in _PHI_PATTERNS if pat.search(text or '')]


# --------------------------------------------------------------------------------------
# Evaluation helpers
# --------------------------------------------------------------------------------------
def _split_sentences(text: str) -> List[str]:
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9(])|\n+|;\s+', text.strip())
    return [re.sub(r'\s+', ' ', p).strip()[:500] for p in parts if p and p.strip()]


def _first_match(patterns: Tuple[Pattern, ...], sentence: str) -> Optional[re.Match]:
    best = None
    for pat in patterns:
        m = pat.search(sentence)
        if m and (best is None or m.start() < best.start()):
            best = m
    return best


def _excerpt(sentence: str, m: re.Match) -> Dict[str, Any]:
    start, end = m.start(), min(m.end(), m.start() + 120)
    b0, a1 = max(0, start - 70), min(len(sentence), end + 70)
    return {
        'before': ('…' if b0 > 0 else '') + sentence[b0:start],
        'match': sentence[start:end],
        'after': sentence[end:a1] + ('…' if a1 < len(sentence) else ''),
    }


def _to_number(token: str) -> float:
    t = token.lower()
    return float(_NUM_WORDS[t]) if t in _NUM_WORDS else float(t)


def _durations_weeks(text: str) -> List[float]:
    out: List[float] = []
    for m in _DUR_RE.finditer(text):
        n = _to_number(m.group(1))
        unit = m.group(3).lower()
        if unit.startswith(('day',)):
            w = n / 7
        elif unit.startswith(('wk', 'week')):
            w = n
        elif unit.startswith(('mo',)):
            w = n * 4.345
        else:
            w = n * 52
        out.append(w)
    return out


def _evaluate(c: Criterion, sentences: List[str], required_weeks: int) -> Dict[str, Any]:
    strong_hits, weak_hits, neg_hits = [], [], []
    for idx, s in enumerate(sentences):
        m = _first_match(c.strong, s)
        kind = 'strong'
        if not m:
            m = _first_match(c.weak, s)
            kind = 'weak'
        if not m:
            continue
        negated = bool(c.negatable and _NEG.search(s[max(0, m.start() - 45):m.start()]))
        hit = (idx, s, m)
        if negated:
            neg_hits.append(hit)
        elif kind == 'strong':
            strong_hits.append(hit)
        else:
            weak_hits.append(hit)

    status, detail, metrics = 'MISSING', 'Not found in the note.', {}
    evidence: List[Dict[str, Any]] = []
    contradiction = None

    if strong_hits:
        status, detail = 'MET', 'Documented.'
        evidence = [_excerpt(s, m) for _, s, m in strong_hits[:2]]
        if c.needs_duration:
            found = _durations_weeks(' '.join(s for _, s, _ in strong_hits))
            weeks = max(found) if found else 0.0
            metrics = {'documented_weeks': round(weeks, 1), 'required_weeks': required_weeks}
            if not found:
                status, detail = 'PARTIAL', 'Therapy outcome is documented, but no duration is stated.'
            elif weeks < required_weeks:
                status = 'PARTIAL'
                detail = (f'{weeks:g} weeks documented; policy template expects at least {required_weeks}.')
            else:
                detail = f'{weeks:g} weeks documented (policy template expects at least {required_weeks}).'
    elif weak_hits:
        status, detail = 'PARTIAL', 'Mentioned, but not specific enough to satisfy the criterion.'
        evidence = [_excerpt(s, m) for _, s, m in weak_hits[:2]]
    elif neg_hits:
        _, s, m = neg_hits[0]
        contradiction = _excerpt(s, m)
        detail = 'The note states the opposite — resolve it or document why the criterion does not apply.'

    return {
        'id': c.id, 'axis': c.axis, 'label': c.label, 'weight': c.weight,
        'critical': c.critical, 'optional': c.optional, 'status': status,
        'detail': detail, 'evidence': evidence, 'contradiction': contradiction,
        'metrics': metrics, 'effort': c.effort, 'owner': c.owner,
        'action': c.specific, 'why': c.why,
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _in_scope(item: Dict[str, Any]) -> bool:
    """Optional criteria only count when the chart actually satisfies them."""
    return not (item['optional'] and item['status'] == 'MISSING')


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def analyze_evidence(claim: Any, category: str, note_text: str, conservative_weeks: int = 6) -> Dict[str, Any]:
    """Audit `note_text` against the criteria template for `category`."""
    text = (note_text or '').strip()
    if len(text) < 40:
        raise ValueError('Clinical text is too short to audit (minimum 40 characters).')

    criteria = CRITERIA_BY_CATEGORY.get(category, _GENERIC)
    sentences = _split_sentences(text)
    items = [_evaluate(c, sentences, conservative_weeks) for c in criteria]

    scoped = [i for i in items if _in_scope(i)]
    total_w = sum(i['weight'] for i in scoped) or 1
    earned = sum(i['weight'] * _STATUS_VALUE[i['status']] for i in scoped)
    coverage = round(100 * earned / total_w)

    axes: List[Dict[str, Any]] = []
    for axis in dict.fromkeys(i['axis'] for i in items):
        in_axis = [i for i in scoped if i['axis'] == axis]
        possible = sum(i['weight'] for i in in_axis)
        if not possible:
            continue
        got = sum(i['weight'] * _STATUS_VALUE[i['status']] for i in in_axis)
        axes.append({
            'axis': axis,
            'score': round(100 * got / possible),
            'met': sum(1 for i in in_axis if i['status'] == 'MET'),
            'total': len(in_axis),
        })

    critical_missing = [i for i in scoped if i['critical'] and i['status'] == 'MISSING']
    critical_partial = [i for i in scoped if i['critical'] and i['status'] == 'PARTIAL']
    if coverage >= 85 and not critical_missing:
        verdict = 'READY'
    elif coverage >= 55:
        verdict = 'STRENGTHEN'
    else:
        verdict = 'AT RISK'

    benchmark = BENCHMARK_REVERSAL_RATES.get(category, 50)
    amount = float(getattr(claim, 'denied_amount', 0) or 0)
    adjusted = round(_clamp(benchmark * (0.55 + 0.65 * coverage / 100), 3, 95))
    potential = round(_clamp(benchmark * 1.2, 3, 95))

    gaps = []
    for i in scoped:
        if i['status'] == 'MET':
            continue
        shortfall = 1.0 - _STATUS_VALUE[i['status']]
        gain_pts = round(benchmark * 0.65 * (i['weight'] / total_w) * shortfall, 1)
        gaps.append({
            **{k: i[k] for k in ('id', 'label', 'axis', 'status', 'critical', 'effort', 'owner', 'action', 'why', 'detail')},
            'gain_pts': gain_pts,
            'gain_usd': round(amount * gain_pts / 100, 2),
        })
    gaps.sort(key=lambda g: (
        not (g['critical'] and g['status'] == 'MISSING'),
        -g['gain_pts'],
        _EFFORT_ORDER.get(g['effort'], 1),
    ))
    for rank, g in enumerate(gaps, start=1):
        g['rank'] = rank

    counts = {s: sum(1 for i in scoped if i['status'] == s) for s in ('MET', 'PARTIAL', 'MISSING')}
    return {
        'method': METHOD,
        'category': category,
        'policy_basis': (f'Template criteria for "{category}" denials '
                         f'(generic payer-policy pattern for {getattr(claim, "payer", "payer")}).'),
        'coverage': coverage,
        'verdict': verdict,
        'counts': counts,
        'axes': axes,
        'criteria': items,
        'blockers': [{'id': i['id'], 'label': i['label'], 'action': i['action']} for i in critical_missing],
        'critical_partial': [i['id'] for i in critical_partial],
        'gap_plan': gaps,
        'win_likelihood': {
            'benchmark': benchmark,
            'current': adjusted,
            'potential': potential,
            'expected_usd_now': round(amount * adjusted / 100, 2),
            'expected_usd_potential': round(amount * potential / 100, 2),
        },
        'conservative_weeks_required': conservative_weeks,
        'note_stats': {'characters': len(text), 'sentences': len(sentences)},
        'note_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'disclaimer': DISCLAIMER,
    }
