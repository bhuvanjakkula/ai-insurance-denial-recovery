# AI Insurance Denial Recovery — Clinic MVP

**Runnable, local-only demonstration** for US clinic revenue-cycle teams to triage denied claims, prioritize work, review evidence checklists, prepare conservative appeal drafts, and track manual outcomes.

**Scope / safety:** USE SYNTHETIC OR FULLY DE-IDENTIFIED DATA ONLY. This prototype has **not** been assessed for HIPAA compliance, and is not approved for storing/transmitting PHI. No payer, EHR, clearinghouse, LLM, or live insurer connection exists. No claims or appeals are submitted automatically. Rules do not determine coverage or appeal eligibility. The “AI” product name refers to the future roadmap; this implementation deliberately uses explainable deterministic rules.

## Start with Docker

```bash
cp .env.example .env
# Set DEMO_API_KEY to a non-default long random value in .env
# docker compose reads .env variables
docker compose up --build
```

Visit http://localhost:8000 and enter your `DEMO_API_KEY` under **Demo API key**. Import `sample_data/synthetic_claims.csv` using the Add / Import screen. The Docker port is bound to localhost only.

## Start using Python

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export DEMO_API_KEY='my-local-only-secret'  # Windows PowerShell: $env:DEMO_API_KEY='my-local-only-secret'
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and enter the same API key. API documentation: http://127.0.0.1:8000/docs

## Tests

```bash
pytest -q
```

## Features

- Browser dashboard and denied-amount / active queue metrics
- Single claim creation and validated bulk CSV intake
- Explainable rules for sample CARC reason codes 16, 18, 29, 50, 96, 109, 197 (with common CO- prefixed variants)
- Date-sensitive *operational* priority score, not estimated recovery probability
- Evidence checklist and careful human-reviewed appeal draft
- Manual workflow states and appeal approval record
- Basic action audit log and CSV export
- Local API key and tenant filtering (demo-grade only)
- Automated API tests, GitHub Actions CI, Dockerfile / Compose

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET / POST | `/api/claims` | List/create claims |
| GET | `/api/claims/{id}` | Claim + triage |
| PATCH | `/api/claims/{id}/status` | Update status |
| POST | `/api/claims/{id}/appeals` | Generate draft |
| GET | `/api/appeals/{id}` | View draft |
| PATCH | `/api/appeals/{id}/approval` | Record manual approval |
| POST | `/api/import/csv` | Import CSV |
| GET | `/api/export/claims.csv` | Export CSV |
| GET | `/api/metrics` | Revenue queue overview |
| GET | `/api/audit` | Latest audit events |

All `/api/*` endpoints require an `X-API-Key` header. A local demo convenience mechanism, **not** suitable as production clinical authentication.

## Example curl

```bash
curl -H 'X-API-Key: my-local-only-secret' http://127.0.0.1:8000/api/metrics
curl -X POST -H 'X-API-Key: my-local-only-secret' \
  -F 'file=@sample_data/synthetic_claims.csv' http://127.0.0.1:8000/api/import/csv
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a Mermaid diagram, data entities, and production-hardening plan.

## Clinical / legal notes

Denial code semantics depend on adjustment group, claim context, remark code, policy and payer. Do not infer patient liability solely from a denial code. Always verify current payer deadlines, plan documents, procedure/diagnosis documentation and appeal venue. The software offers **drafts**, not legal or clinical advice.

Official references: [CMS — Health Care Payment and Remittance Advice](https://www.cms.gov/medicare/coding-billing/electronic-billing/health-care-payment-remittance-advice) · [HHS HIPAA Security Rule](https://www.hhs.gov/hipaa/for-professionals/security/index.html).
