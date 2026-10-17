# MedRecover AI â€” Insurance Denial Recovery & Appeals Platform

[![Tests](https://github.com/clinic-rcm/ai-insurance-denial-recovery/actions/workflows/tests.yml/badge.svg)](tests)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An intelligent clinical revenue cycle management (RCM) platform built for medical clinics to triage denied insurance claims, prioritize workload by financial urgency and deadline, track evidence checklists, draft evidence-grounded appeals, and record human approvals.

---

## What We Built

- **High-Tech Clinic Dashboard & Command Center**: Responsive vanilla HTML/CSS/JS interface with dark/light themes, real-time KPI metrics, deadline countdown badges, priority meters, and worklist search/filtering.
- **Denial Intelligence Engine**: Parse real ANSI X12 835 EDI remittance advice and explanation of benefits (EOB) text, extract CARC + RARC codes, identify root-cause drivers, and compute win probability.
- **Explainable CARC & RARC Rules Engine**: Transparent classification of 30+ Claim Adjustment Reason Codes and Remittance Advice Remark Codes with statutory citations (CMS LCD/NCD, ERISA §503, ACA §2719, AMA guidelines).
- **Multi-Factor Operational Prioritizer**: 0-100 scoring model evaluating financial exposure, impending filing deadline proximity, and historical category recoverability benchmarks.
- **Evidence Gap Radar**: Audits de-identified chart notes against payer-policy criteria for the specific denial type. Negotiates negation ("no prior PT") and duration thresholds ("4 weeks documented vs >=6 required"). Produces an animated radar chart, coverage score, blocking gap roadmap, and expected dollar recovery uplift.
- **Clinical Appeal Studio & Auto-Composer**: Interactive evidence verification checklist, editable appeal drafts with Level 1 / Level 2 escalation, payer dispatch directory (portals, fax, addresses), and optional AI enhancement.
- **AI Enhancement Layer**: Integrated OpenAI / Gemini LLM reasoning for nuanced clinical appeal synthesis, with robust deterministic fallback when no LLM key is supplied.
- **Post-Quantum Cryptography & Compliance Guardrails**: NIST FIPS 204 (ML-DSA-65 / Dilithium) hardware-token attestation, zero-knowledge passwordless owner authentication, tamper-evident hash-chained audit ledger, and zero-PHI client scanner.
- **SaaS Customer Portal & Public Pricing**: Multi-tenant customer accounts with scrypt password hashing, session tokens, CSRF protection, trial expiration gates, public transparent USD pricing plans, and an owner enquiry inbox.
- **Automated Test Suite**: 61 automated unit, integration, security, and end-to-end tests covering all APIs, cryptographic proofs, accounts, and clinical workflows.

---

## Quick Start (How to Run)

### 1. Run with Python (Local)

Requires Python 3.11+.

```powershell
# Open terminal in project folder:
cd "C:\Users\bhuva\.gemini\antigravity-ide\scratch\ai-insurance-denial-recovery"

# Install dependencies:
pip install -r requirements.txt

# Run the FastAPI server:
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

1. Open your browser at [http://127.0.0.1:8000](http://127.0.0.1:8000).
2. The demo API key (`change-me-for-local-demo`) is pre-loaded. Click **Connect** or **Save**.
3. Click **âš¡ Seed 10 Claims** or import [sample_data/synthetic_claims.csv](sample_data/synthetic_claims.csv) to explore the worklist!

Interactive API Swagger documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

---

### 2. Run with Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Access the dashboard at [http://localhost:8000](http://localhost:8000).

---

## Running the Automated Tests

Run the full Pytest test suite:

```powershell
python -m pytest -v
```

All 61 tests will execute and validate:
- Health and demo authentication (`test_api.py`)
- Full claim creation, triage, appeal draft, batch operations, and human approval workflow (`test_api.py`)
- Bulk CSV import and date validation (`test_api.py`)
- CARC 50 Medical Necessity, CARC 197 Prior Auth, and CARC 29 Timely Filing classification (`test_engine.py`)
- Urgent countdown calculation and expired deadline warning flags (`test_engine.py`)
- Evidence-grounded appeal letter synthesis and Level 2 escalation (`test_engine.py`)
- Evidence Gap Radar scoring, negation, threshold, PHI guard, API, and appeal integration (`test_radar.py`)
- 835 EDI & EOB remittance parsing, CARC/RARC extraction, and win factor scoring (`test_enterprise.py`)
- Command Center revenue-at-risk analytics, root-cause heatmaps, and payer performance (`test_enterprise.py`)
- Tamper-evident hash-chained audit ledger, PHI redaction, and LLM enhancement (`test_enterprise.py`)
- NIST FIPS 204 ML-DSA-65 post-quantum zero-knowledge owner login and security headers (`test_security.py`)
- Customer signup/signin, scrypt passwords, session tokens, CSRF, and data isolation (`test_accounts.py`)
- End-to-end UI static assets, full clinical workflows, and remittance import (`test_e2e_ui_flow.py`)

---

## Evidence Gap Radar API

| Endpoint | Purpose |
|---|---|
| `POST /api/claims/{id}/evidence-radar` | Audit a de-identified note (`clinical_text`, optional `conservative_weeks`) and persist the scan. Rejects SSN / phone / email / DOB / MRN patterns. |
| `GET /api/claims/{id}/evidence-radar` | Latest scan for the claim (`{"scan": null}` if none). |
| `GET /api/claims/{id}/evidence-radar/sample` | Synthetic, intentionally incomplete note for the claim's denial category. |

Criteria are generic policy-pattern templates and win likelihood is an illustrative heuristic, so always verify against the payer's current medical policy before filing. The full note is not stored; only the scan result (which includes short matched excerpts) and a SHA-256 of the note are persisted.

---

## Architecture & Production Roadmap

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed Mermaid diagrams, the mathematical prioritization formula, and the enterprise roadmap for HIPAA compliance and EHR/Clearinghouse electronic EDI integration.

