"""
Optional LLM layer — polishes appeal drafts without ever being required.

The deterministic engine always works. When OPENAI_API_KEY or GEMINI_API_KEY (or GOOGLE_API_KEY)
is configured, `enhance_appeal` asks the model to improve tone and structure, then *validates*
the result before it is accepted:

  * the human-review banner must survive,
  * no dollar amount or claim-style identifier may appear that was not in the source draft,
  * the rewrite must not collapse to a fraction of the original length.

Text is blocked from leaving the process if it still contains PHI-like patterns. Any provider
error, unconfigured state or failed validation falls back to the deterministic draft.
Standard library only (urllib) — no extra dependencies.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from backend.radar import phi_findings

DEFAULT_MODELS = {'openai': 'gpt-4o-mini', 'gemini': 'gemini-2.0-flash'}
TIMEOUT_SECONDS = 45

SYSTEM_PROMPT = (
    'You are an expert medical-billing appeals editor. Improve the clarity, tone and structure of the '
    'draft insurance appeal letter you are given. Hard rules: (1) never invent facts, dates, dollar amounts, '
    'codes, policy numbers, guideline citations or clinical findings that are not in the draft or the facts; '
    '(2) keep the "DRAFT — HUMAN REVIEW REQUIRED" banner and the entire human review checklist unchanged; '
    '(3) keep all placeholders and signature blocks; (4) keep every enclosed-evidence line; '
    '(5) where the facts list open evidence gaps, do not assert those items as proven; '
    '(6) return the full letter as plain text only, with no commentary.'
)

_MONEY_RE = re.compile(r'\$\s?[\d,]+(?:\.\d{1,2})?')
_ID_RE = re.compile(r'\b[A-Z]{2,}(?:-[A-Z0-9]+){1,}\b')


def provider() -> Optional[str]:
    forced = (os.environ.get('LLM_PROVIDER') or '').strip().lower()
    has_openai = bool(os.environ.get('OPENAI_API_KEY'))
    has_gemini = bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))
    if forced == 'openai' and has_openai:
        return 'openai'
    if forced == 'gemini' and has_gemini:
        return 'gemini'
    if forced in ('openai', 'gemini'):
        return None
    return 'openai' if has_openai else 'gemini' if has_gemini else None


def model_name(prov: str) -> str:
    return os.environ.get('LLM_MODEL') or DEFAULT_MODELS[prov]


def status() -> Dict[str, Any]:
    prov = provider()
    return {
        'configured': prov is not None,
        'provider': prov,
        'model': model_name(prov) if prov else None,
        'note': 'Deterministic drafting is always available; the LLM only polishes validated, PHI-screened text.',
    }


def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', **headers}, method='POST')
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # nosec - fixed https endpoints
        return json.loads(resp.read().decode('utf-8'))


def _call_provider(prov: str, user_prompt: str) -> str:
    model = model_name(prov)
    if prov == 'openai':
        data = _post_json(
            'https://api.openai.com/v1/chat/completions',
            {'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']},
            {'model': model, 'temperature': 0.2,
             'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': user_prompt}]},
        )
        return data['choices'][0]['message']['content']
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    data = _post_json(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        {'x-goog-api-key': key},
        {'system_instruction': {'parts': [{'text': SYSTEM_PROMPT}]},
         'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
         'generationConfig': {'temperature': 0.2}},
    )
    return ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts'])


def build_prompt(draft: str, facts: Dict[str, Any]) -> str:
    lines = [f"Denial category: {facts.get('category', 'n/a')}", f"Payer: {facts.get('payer', 'n/a')}"]
    if facts.get('supported'):
        lines.append('Evidence supported by the chart: ' + '; '.join(facts['supported']))
    if facts.get('gaps'):
        lines.append('OPEN evidence gaps (do not claim these are proven): ' + '; '.join(facts['gaps']))
    return 'FACTS\n' + '\n'.join(lines) + '\n\nDRAFT LETTER\n' + draft


def validate_output(draft: str, output: str, extra_allowed: str = '') -> List[str]:
    problems: List[str] = []
    source = draft + ' ' + extra_allowed
    if 'HUMAN REVIEW' not in output.upper():
        problems.append('Human-review banner was removed.')
    if len(output) < 0.5 * len(draft):
        problems.append('Rewrite is much shorter than the source draft.')
    norm = lambda s: re.sub(r'[\s,$]', '', s)
    allowed_money = {norm(m) for m in _MONEY_RE.findall(source)}
    new_money = sorted({m for m in _MONEY_RE.findall(output) if norm(m) not in allowed_money})
    if new_money:
        problems.append('Introduced dollar amounts not in the draft: ' + ', '.join(new_money[:5]))
    allowed_ids = set(_ID_RE.findall(source))
    new_ids = sorted({i for i in _ID_RE.findall(output) if i not in allowed_ids})
    if new_ids:
        problems.append('Introduced identifiers not in the draft: ' + ', '.join(new_ids[:5]))
    return problems


def enhance_appeal(draft: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    """Never raises. Returns a result dict; `text` is set only when status == 'enhanced'."""
    prov = provider()
    base = {'provider': prov, 'model': model_name(prov) if prov else None, 'text': None, 'problems': []}
    if not prov:
        return {**base, 'used_llm': False, 'status': 'unconfigured',
                'message': 'No LLM key configured (OPENAI_API_KEY / GEMINI_API_KEY). Deterministic draft retained.'}
    found = phi_findings(draft)
    if found:
        return {**base, 'used_llm': False, 'status': 'blocked_phi',
                'message': 'Draft contains PHI-like patterns (' + ', '.join(found) + '); it was not sent to the model.'}
    try:
        output = _call_provider(prov, build_prompt(draft, facts)).strip()
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        return {**base, 'used_llm': False, 'status': 'error',
                'message': f'{prov} request failed ({type(e).__name__}). Deterministic draft retained.'}
    problems = validate_output(draft, output, ' '.join(facts.get('supported', []) + facts.get('gaps', [])))
    if problems:
        return {**base, 'used_llm': True, 'status': 'rejected_by_validation', 'problems': problems,
                'message': 'Model output failed safety validation and was discarded.'}
    return {**base, 'used_llm': True, 'status': 'enhanced', 'text': output,
            'message': f'Draft polished by {prov} ({model_name(prov)}) and validated. Human review is still required.'}
