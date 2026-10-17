# Corgi Claims

Recommends payout amounts for security-deposit-alternative claims. For each claim it reads the documents the
property manager sent (lease, ledger, move-out statement, invoices, photos…), extracts every charge and payment,
decides which charges the program covers, and returns a recommended amount with a per-charge justification and a
route: pay automatically or send to a human.

Monorepo with a Django API, a React + Vite frontend, Postgres, and an RQ worker on Redis, all run with Docker Compose.

## Contents

- [Quick start with the prepared data](#quick-start-with-the-prepared-data)
- [Getting started](#getting-started)
- [Using the app](#using-the-app)
- [Loading the data](#loading-the-data)
- [How it works](#how-it-works)
- [Business rules](#business-rules)
- [API](#api)
- [Evaluation](#evaluation)
- [Running the tests](#running-the-tests)
- [Design decisions](#design-decisions)
- [Limitations and next steps](#limitations-and-next-steps)
- [Reference](#reference)

## Quick start with the prepared data

The demo package skips the slow part of the setup: it holds a database with the 1,244 claims already imported
from the spreadsheet and their 5,675 documents already registered (text extracted, Word and HEIC files
converted), but nothing processed yet: no recommendations, extractions or LLM calls. You start making requests
right away instead of running `import_claims` and `register_documents` yourself (the latter takes a long time).

> **Confidential.** The demo package and the claim documents contain tenants' personal data (names, addresses,
> emails). Never commit them, publish them or share them outside the private link. `data/`, `*.dump` and
> `claims-demo*.zip` are gitignored for that reason.

1. **Requirements:** Docker Desktop (with Docker Compose v2). Then create your `.env`:

   ```sh
   cp .env.example .env   # change SECRET_KEY; GEMINI_API_KEY comes in step 5
   ```

2. **Start the stack.** The backend applies the migrations when it starts.

   ```sh
   docker compose up --build
   ```

3. **Get the demo package.** Ask for access to the private link and save the file as
   `data/demo/claims-demo.zip`.

4. **Import it.**

   ```sh
   docker compose exec backend python manage.py import_demo /data/demo/claims-demo.zip
   ```

   It checks the package and the Postgres version, restores the database, extracts `_converted/` and compares
   the row counts with the package's manifest. If the database already has claims it stops without changing
   anything; add `--force` to replace it. On Windows with Git Bash, prefix the command with
   `MSYS_NO_PATHCONV=1` so `/data/...` isn't rewritten into a Windows path.

5. **Get what processing needs:** the original documents and a Gemini key. The package doesn't include the
   originals, and the pipeline sends them to Gemini.

   - Download the claim documents folder from the company's Google Drive into `data/claims/`, keeping its
     structure: `data/claims/<tracking_id>/...`. Drive splits downloads over 2 GB into several ZIPs
     (`...-001.zip`, `...-002.zip`, …). Each one holds a different part of the same folder tree, so extract
     all of them into the same place and let the folders merge:

     ```sh
     # macOS / Linux
     for z in ~/Downloads/claims-*.zip; do unzip -o -q "$z" -d data/; done
     ```

     ```powershell
     # Windows (PowerShell)
     Get-ChildItem $HOME\Downloads\claims-*.zip | ForEach-Object { Expand-Archive $_ -DestinationPath data -Force }
     ```

     Then check that the tracking folders sit directly under `data/claims/`. If the ZIPs add an extra
     top-level folder, move its contents up. `import_demo` tells you how many originals are still missing.

   - Put your key in `.env` as `GEMINI_API_KEY=...`, then run `docker compose up -d backend worker` so the
     containers pick it up.

6. **Analyze.** Open http://localhost:5173, paste tracking numbers and press **Analyze**. The **Metrics**
   tab fills in as claims finish. A claim that isn't in the package (a new folder in `data/claims/`) has to be
   registered first:

   ```sh
   docker compose exec backend python manage.py register_documents --ids 1245
   ```

To build a new package from your own database (for example after importing a new spreadsheet), see
[Sharing the data](#sharing-the-data).

## Getting started

Prerequisites: Docker Desktop (with Docker Compose v2) and a Gemini API key.

```sh
cp .env.example .env   # then fill in GEMINI_API_KEY (and change SECRET_KEY)
docker compose up --build
docker compose exec backend python manage.py createsuperuser   # for /admin/ and /django-rq/
```

Put the claim documents and the spreadsheet in `./data/`. They're gitignored and mounted read-only at `/data`
in the backend and worker containers; only `data/_converted/` is writable. The database stores document paths
relative to that folder (`claims/123/lease.docx`, `_converted/123/lease.docx.pdf`), so it works on any machine.

```
data/
├── Security Deposit Claims.xlsx
└── claims/
    ├── 1/          # one folder per tracking number
    │   └── 305 Coosawatchie St move out calculation - hall.pdf
    ├── 10/
    └── ...
```

| Service         | URL                                              |
| --------------- | ------------------------------------------------ |
| Frontend        | http://localhost:5173                            |
| API             | http://localhost:8000/api/                       |
| API docs        | http://localhost:8000/api/docs                   |
| Health          | http://localhost:8000/api/health/                |
| Admin           | http://localhost:8000/admin/                     |
| Queue dashboard | http://localhost:8000/django-rq/ (staff login)   |
| Postgres        | `localhost:5433` (credentials in `.env`)         |

The frontend proxies `/api` to the backend, so the browser only talks to `localhost:5173`.

## Using the app

Open http://localhost:5173, paste up to 15 tracking numbers (separated by commas, spaces or new lines) and
press **Analyze**. The table fills in immediately with what's already known (unknown IDs show as
*Not found*, finished claims show their result) and the rest update live as the worker processes them. Click a
row for the full breakdown: recommended amount and cap, justification, flags, every charge with its
eligibility and reason, payments and credits, the LLM's findings and the documents. Check
**Force reprocessing** to recompute claims that are already finished or failed.

The **Metrics** tab (http://localhost:5173/#metrics) shows the [evaluation](#evaluation) in three cards: the
AI's average error (MAE), the error of the simple rule `min(Amount of Claim, Max Benefit)`, and how much less
(or more) error the AI has, with its bias in plain words. Below, every evaluated claim sorted by error, with the
signed difference AI − human (red when the AI pays less, amber when it pays more). Click a row for its details.

## Loading the data

Run this once inside the backend container. It's idempotent: running it again updates instead of duplicating.

```sh
# Spreadsheet -> Claim (+ ClaimOutcome). Picks a fixed 40-claim test set (seed 42).
docker compose exec backend python manage.py import_claims "/data/Security Deposit Claims.xlsx"
```

Then process claims, either from the API (`POST /api/claims/analyze`) or from the command line:

```sh
docker compose exec backend python manage.py process_claims --ids 1,10,100          # enqueue for the worker
docker compose exec backend python manage.py process_claims --ids 1 --sync          # run inline, for debugging
docker compose exec backend python manage.py process_claims --ids 1 --sync --force-from-step 3
```

`--force-from-step N` ignores cached results from step N on (1 classify, 2 lease, 3 claim, 4 rules).

To check the Gemini setup with a single real call:

```sh
docker compose exec backend python manage.py llm_smoke_test "/data/claims/1/305 Coosawatchie St move out calculation - hall.pdf"
```

### Sharing the data

`export_demo` packs the current database and `data/_converted/` into the ZIP that
[the quick start](#quick-start-with-the-prepared-data) imports:

```sh
docker compose exec backend python manage.py export_demo   # -> data/demo/claims-demo.zip
```

```
claims-demo.zip
├── manifest.json   export date, git commit, Postgres version, row counts, tracking numbers with results,
│                   rules and prompt versions, size of _converted/
├── db.dump         pg_dump -Fc of the whole database (session data excluded)
└── _converted/     copy of data/_converted/
```

- **Whole database:** the dump includes migrations, sequences and users, so `pg_restore` leaves it consistent.
  It carries whatever the database holds: to share a package without results, export before processing.
- **Versions:** `pg_dump` and `pg_restore` 18 are installed in the backend image to match the `db` service.
  If you change the Postgres image, change `postgresql-client-18` in `backend/Dockerfile` too.
- **Git commit:** there's no git inside the container, so the manifest takes the commit from the optional
  `GIT_COMMIT` variable in `.env`.
- **Mount:** `data/demo/` is mounted writable in the backend, since the rest of `/data` is read-only.

The package contains personal data: share it only through a private link.

## How it works

The guiding split: **the LLM reads, classifies and judges; the code adds, validates and caps.** No amount the
user sees is computed by the model.

```
Claim ──► 1. classify ──► 2. lease (Prompt A) ──► 3. claim (Prompt B) ──► 4. rules ──► Recommendation
              │                  │                       │                     │
         extension →        lease terms:            every charge,          FIFO payments,
         rules →            fees, limits,           payment, credit,       eligibility, cap,
         LLM (Prompt C)     coverage                eligibility + reason   flags, route
```

Each step updates `Claim.processing_status`
(`queued → classifying → analyzing_lease → analyzing_claim → computing → done | needs_review`), which is what
the live stream reports.

1. **Classify** (`claims/pipeline/classify.py`). From cheapest to most expensive: file extension (images →
   `photo`), local rules on the file name and the text of the first two pages, and only then one batched LLM
   call (Prompt C) for whatever is still `unknown`.
2. **Lease** (`claims/pipeline/lease.py`, Prompt A). Documents typed `lease` or `lease_mixed` are turned into
   a short JSON of the terms that matter for the payout (fees and their limits, coverage). If the model says the
   document isn't a lease, it's reclassified and the claim continues with the program's default rules.
3. **Claim** (`claims/pipeline/claim.py`, Prompt B). The rest of the documents go to the model with the claim's
   fields and the lease terms. It returns every charge, payment, credit and concession, a category for each
   charge, an eligibility judgment with its reason and the lease section that supports it, the printed total,
   flags and a confidence level. Only `Claim` fields fill the prompt: the human decision (`ClaimOutcome`) never
   reaches the model.
4. **Rules** (`claims/pipeline/rules.py`). Pure Python on `Decimal`, no LLM and no database, so it can be
   re-run instantly over stored extractions (`recompute_recommendations`).

If the rules route a claim to `retry_stronger_model`, step 3 is repeated once with `GEMINI_MODEL_RETRY` and the
rules run again. If it still isn't resolved, it goes to review.

Every extraction is cached by a hash of its documents, model and prompt version, so reprocessing a claim doesn't
spend quota unless something changed or `force` is used.

## Business rules

Implemented in `claims/pipeline/rules.py` (`RULES_VERSION = "v1"`):

- **Payments are applied FIFO.** Payments, credits and concessions pay off the oldest charges first; undated
  charges go last.
- **Eligibility.** A per-category override wins over the model: `program_fee` and `normal_wear_and_tear` are
  never covered. For everything else (late, legal, admin and convenience fees included) the model's judgment,
  grounded in the lease, is kept. A charge the model didn't judge is excluded.
- **Amount.** `recommended = clamp(eligible balance − deposit held, 0, max_benefit)`. `cap_applied` records
  whether the cap was hit.
- **Flags** computed by code, on top of the model's own:

  | Flag                      | Meaning                                                                  |
  | ------------------------- | ------------------------------------------------------------------------ |
  | `totals_mismatch`         | Charges − payments differ from the printed total by more than $1.00      |
  | `claim_amount_mismatch`   | The claimed amount doesn't match the documented total (or is < $10)      |
  | `fee_exceeds_lease_terms` | A fee is above what the lease allows                                     |
  | `coverage_mismatch`       | The lease's coverage differs from the claim's `max_benefit`              |
  | `amount_not_in_text`      | An extracted amount doesn't appear in its document's text (hallucination check) |
  | `rent_mismatch`           | A full-month rent charge differs from the claim's monthly rent           |
  | `missing_max_benefit`     | The claim has no `max_benefit`                                           |

- **Route.**
  - `auto` (status `done`): high confidence and no critical flag (`totals_mismatch`, `illegible_document`,
    `photos_only`, `missing_documentation`, `missing_max_benefit`).
  - `retry_stronger_model`: `totals_mismatch` with low or medium confidence.
  - `review` (status `needs_review`): everything else.

The recommendation stores the per-charge breakdown (eligible or not, source of the decision, reason, amount
paid, balance) and a plain-language explanation.

## API

Interactive docs at http://localhost:8000/api/docs.

| Method | Path                                | Description                                                      |
| ------ | ----------------------------------- | ---------------------------------------------------------------- |
| `POST` | `/api/claims/analyze`               | Enqueue up to 15 tracking numbers. Returns `202` immediately.    |
| `GET`  | `/api/claims/{tracking_number}`     | Claim data, documents and the latest recommendation in full.     |
| `GET`  | `/api/claims/stream?ids=1,10`       | Live status via Server-Sent Events.                              |
| `GET`  | `/api/metrics`, `/api/metrics/claims` | Evaluation against the human decision (see below).             |
| `GET`  | `/api/health/`                      | Django and Postgres check.                                       |

```sh
curl -X POST localhost:8000/api/claims/analyze \
  -H 'Content-Type: application/json' \
  -d '{"tracking_numbers": ["1", "10", "100"], "force": false}'
```

`analyze` doesn't reprocess claims that are already queued or finished; it returns their current state or
summary. Pass `force: true` to reprocess. A `failed` claim is never retried automatically.

The stream sends `event: claim` with a snapshot of every ID on connect and then only the ones that change
(status, result or queue position),
`event: complete` once all are final, and `event: timeout` after 15 minutes (the client can reconnect). It needs
an ASGI server, which is why the backend runs on uvicorn.

Final statuses: `done`, `needs_review`, `failed`, `quota_exhausted`, `not_found`.

## Evaluation

How close the AI gets to the human decision (`ClaimOutcome.approved_benefit`), compared with a simple rule.
`claims/evaluation/metrics.py` is the only module that reads `ClaimOutcome`.

```sh
docker compose exec backend python manage.py evaluate                      # current rules version
docker compose exec backend python manage.py evaluate --rules-version v1
# IA: MAE $663.00 | Regla simple: MAE $2,114.54 | 68.6% menos error | Bias IA: -$523.33 | n=6
```

| Endpoint                                    | Returns                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------ |
| `GET /api/metrics?rules_version=v1`         | `n`, `ai_mae`, `ai_bias`, `baseline_mae` and `improvement_pct`           |
| `GET /api/metrics/claims?rules_version=v1`  | One row per claim, largest error first: human, AI, `difference`, `abs_error`, route, flags |

Amounts are JSON numbers here (the rest of the API sends money as strings). With no evaluated claims,
`/api/metrics` still answers 200, with `n: 0` and the metrics as `null`.

**Evaluated set:** claims with a `Recommendation` for the requested rules version and a non-null
`approved_benefit`. The AI and the simple rule are measured on exactly the same claims, so claims without
`claim_amount` are left out (the rule can't predict them).

| What            | Definition                                                                         |
| --------------- | ---------------------------------------------------------------------------------- |
| Simple rule     | `min(claim_amount, max_benefit)`: pay what was claimed, up to the cap               |
| MAE             | mean of `|prediction − human|`                                                     |
| Bias            | mean of `AI − human`. Negative: the AI pays less than the human                     |
| Improvement     | `(rule MAE − AI MAE) / rule MAE × 100`, 1 decimal. Negative when the AI is worse; `null` when the rule's MAE is 0 |
| Difference      | per claim, `AI − human`                                                            |

## Running the tests

```sh
docker compose exec backend python manage.py test claims
```

Tests never call Gemini. They use the fake client (`claims/llm/fake.py`) with the canned responses in
`claims/tests/fixtures/llm/`, including invalid JSON, per-minute and daily 429s, and 5xx errors.

To run the whole app without spending quota, set `GEMINI_FAKE=1` in `.env`.

## Design decisions

- **Code owns the money.** LLMs are good at reading messy ledgers and bad at arithmetic. Keeping the sums,
  the FIFO, the cap and the routing in deterministic code makes every amount reproducible and testable, and
  lets the rules change without new LLM calls.
- **Cheapest classifier first.** Most documents are typed by extension or file name; the LLM only sees the
  leftovers, in a single batched call per claim.
- **Route instead of guessing.** When the extraction disagrees with itself (printed total, claim amount,
  lease terms, text of the document), the claim goes to a stronger model or to a human instead of producing a
  confident wrong number.
- **One worker on purpose.** The bottleneck is the Gemini quota, not CPU; more workers only produce 429s. The
  client enforces a minimum interval per model (RPM + 10% margin) and stops at 95% of the daily limit
  (`quota_exhausted`) instead of letting the API fail. The quota day resets at midnight Pacific time.
- **One gateway to the LLM.** `claims/llm/client.py` is the only module that touches the SDK. Every call
  passes through quota → rate limit → API with retries → Pydantic validation (one retry on invalid output) →
  an `LLMCall` row, so cost and failures are auditable.
- **Ground truth stays out of the prompt.** `ClaimOutcome` (the approved benefit and the human decision) is
  stored separately and only used for evaluation, never to build prompts.

## Limitations and next steps

- **Free-tier quota** limits throughput to a few hundred claims per day.
- **Unsupported files** (zip, msg, txt…) are reported by `register_documents` but not read.
- **Dev setup only.** Both containers run with hot reload (`uvicorn --reload`, `vite dev`); there's no
  production build or CI.

## Reference

### Environment variables

| Variable                 | Description                                                       |
| ------------------------ | ----------------------------------------------------------------- |
| `POSTGRES_*`             | Database connection (shared by the `db` container and Django)     |
| `SECRET_KEY`, `DEBUG`    | Django                                                            |
| `ALLOWED_HOSTS`          | Django allowed hosts                                              |
| `CORS_ALLOWED_ORIGINS`   | Origins allowed to call the API                                   |
| `GEMINI_API_KEY`         | Gemini key                                                        |
| `GEMINI_FAKE`            | `1` = canned responses from JSON files, no API calls              |
| `GEMINI_MODEL_CLASSIFY`  | Default `gemini-3.1-flash-lite`                                   |
| `GEMINI_MODEL_EXTRACT`   | Lease and claim extraction. Default `gemini-3.5-flash-lite`       |
| `GEMINI_MODEL_RETRY`     | Hard cases. Default `gemini-3.5-flash`                            |
| `REDIS_URL`              | Job queue                                                         |
| `DATA_ROOT`              | Root of the documents inside the containers. Default `/data`      |
| `GIT_COMMIT`             | Optional. Commit recorded in the demo package manifest            |

Rate limits per model are in `GEMINI_RATE_LIMITS` in `backend/config/settings.py`.

### Management commands

| Command                       | What it does                                                         |
| ----------------------------- | -------------------------------------------------------------------- |
| `import_claims`               | Spreadsheet → claims. `--dry-run`, `--sheet`, `--test-size`, `--seed`, `--reset-test-set` |
| `register_documents`          | Files → documents. `--ids` / `--all`, `--workers`                    |
| `classify_documents`          | Classify pending documents. `--ids` / `--all`, `--no-llm`            |
| `process_claims`              | Full pipeline. `--ids`, `--sync`, `--force-from-step`                |
| `recompute_recommendations`   | Re-run the rules over stored extractions, no LLM. `--ids` / `--all`  |
| `llm_smoke_test`              | One real Gemini call to check the setup                              |
| `evaluate`                    | AI vs. the simple rule against the human decision, in one line. `--rules-version` |
| `relocate_converted`          | One-off: moves conversions to `_converted/<original path>.<ext>`. `--dry-run` |
| `export_demo`                 | Demo package (DB dump + `_converted/`) for `import_demo`. `--output` |
| `import_demo`                 | Restore a demo package: DB + `_converted/`. `--force` to replace existing results |

### Project layout

```
backend/
├── config/            settings, URLs, NinjaAPI (health)
└── claims/
    ├── api.py         analyze, detail and metrics endpoints
    ├── sse.py         live status stream
    ├── evaluation/    metrics vs. the human decision (the only reader of ClaimOutcome)
    ├── jobs.py        RQ queue
    ├── paths.py       DATA_ROOT-relative document paths (resolve_data_path)
    ├── demo.py        demo package helpers (export_demo, import_demo)
    ├── models.py      Claim, ClaimOutcome, ClaimDocument, LeaseExtraction, ClaimExtraction, Recommendation, LLMCall
    ├── excel/         spreadsheet parsing and cleaning
    ├── documents/     file registration and conversion
    ├── llm/           Gemini client, rate limit, schemas, fake client
    ├── pipeline/      classify, lease, claim, rules, orchestrator
    ├── prompts/       classify.md (C), lease.md (A), claim.md (B)
    └── tests/
frontend/src/
├── App.tsx            Analyze and Metrics tabs (tab kept in the URL hash)
├── api/               types mirroring the API + fetch helpers
├── hooks/             useClaimStream (EventSource lifecycle)
├── components/        AnalyzeForm, ResultsTable, ClaimDetailPanel, MetricsView, StatusBadge
└── labels.ts          status, flag, category and document-type labels; formatting
```

### Day-to-day

- The worker doesn't reload code. After editing the pipeline: `docker compose restart worker`.
- Python dependencies: add to `backend/requirements.txt`, then `docker compose up --build backend worker`.
- Node dependencies: `docker compose exec frontend npm install <pkg>`, then `docker compose up --build -V frontend`
  (`-V` recreates the anonymous `node_modules` volume so it picks up the new packages).
- Migrations: `docker compose exec backend python manage.py makemigrations && docker compose exec backend python manage.py migrate`.
