# LINE Bot Serverless Cloud Run Path

This is the free-tier-oriented path for keeping the LINE webhook reachable when
the Mac is off. It is not the same reliability model as the existing VM runbook.

## Decision

Use **Google Cloud Run service** for the webhook first:

- It can run the existing FastAPI/Docker app with small changes.
- It scales to zero, so the Mac does not need to stay on.
- It has a monthly free tier for request-based services.
- Cloud Scheduler can later trigger selected `/jobs/*` endpoints.

Do not use Cloudflare Workers or Vercel for the full bot as-is. This app is a
large Python/FastAPI service with SQLite state, subprocess jobs, media handling,
and several optional heavy dependencies.

## Free-Tier Caveats

Cloud Run can be effectively free for this workload, but it is not a hard
"cannot charge money" product:

- Google Cloud requires a billing account.
- Cloud Run request-based services currently include free monthly request, CPU,
  and memory usage, then charge after that.
- Cloud Scheduler currently gives 3 jobs per billing account free, then charges
  per job.
- Cloud Build and Artifact Registry are separate products; image storage/builds
  can become paid if usage exceeds their own free tiers.

Set a billing budget alert before cutover.

## Phase 1 Scope

Phase 1 target:

- `GET /health` works on Cloud Run.
- `POST /callback` is reachable by LINE.
- LINE signatures are still validated by `main.py`.
- Bot is deployed with `BOT_MUTED=true` first.
- After LINE webhook test passes, cut over by changing LINE Developer Console
  webhook URL to `<CLOUD_RUN_URL>/callback`, then set `BOT_MUTED=false`.

Out of scope for phase 1:

- Guaranteed persistent SQLite memory.
- Guaranteed reminders after instance recycle.
- Full local image generation / local LLM.
- Migrating all Mac launchd jobs.

Reason: Cloud Run container filesystem is ephemeral. The existing
`line_bot.db`, local JSON state, and generated files must move to a durable
cloud store before claiming full stateful parity.

## Code Readiness Already Added

- `Dockerfile` listens on `${PORT:-8080}`, which matches Cloud Run's runtime
  contract.
- `jobs_config.py` no longer hardcodes `/Users/andrew/Desktop/...` for LINE bot
  Python jobs.
- `jobs_router.py` can allow Cloud Scheduler-style public HTTP calls only when
  `JOBS_ALLOW_PUBLIC_HTTP=1`; token checks still apply.
- `JOBS_SUBPROCESS_INHERIT_ENV=1` lets scheduled subprocesses see Cloud Run
  runtime secrets when jobs are intentionally enabled.

## Environment

Use `.env.serverless.example` as the checklist for Cloud Run env vars. Do not
upload `.env` directly.

`.gcloudignore` is part of the deploy safety boundary. It prevents `.env`,
SQLite DB files, token caches, logs, and virtualenv folders from being uploaded
by `gcloud run deploy --source .`.

Initial phase 1 values:

```text
BOT_MUTED=true
SQLITE_PATH=/tmp/line_bot.db
LOCAL_LLM_PREWARM_DISABLED=1
JOBS_ROUTES_ENABLED=0
JOBS_ALLOW_PUBLIC_HTTP=0
JOBS_SUBPROCESS_INHERIT_ENV=1
```

After deploy, set:

```text
LINE_BOT_PUBLIC_BASE_URL=<Cloud Run service URL without trailing slash>
```

## Build And Deploy

Run these from `line_bot/` after choosing a GCP project and region:

```bash
.venv/bin/python cloud_run_plan.py deploy-commands --project <PROJECT_ID>
```

That helper prints the non-mutating command plan below and checks that
`.gcloudignore` contains the required runtime-data exclusions.

Manual command shape:

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

gcloud run deploy line-bot \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 1 \
  --min-instances 0 \
  --max-instances 1 \
  --timeout 300 \
  --set-env-vars BOT_MUTED=true,SQLITE_PATH=/tmp/line_bot.db,LOCAL_LLM_PREWARM_DISABLED=1,JOBS_ROUTES_ENABLED=0,JOBS_ALLOW_PUBLIC_HTTP=0,JOBS_SUBPROCESS_INHERIT_ENV=1
```

Then set non-sensitive env vars directly:

```text
LINE_CHANNEL_ID
GEMINI_MODEL
GEMINI_LIGHT_MODEL
ALLOWED_GROUP_IDS
FAMILY_GROUP_ID
```

Set sensitive values through the Cloud Run console or Secret Manager:

```text
LINE_CHANNEL_SECRET
LINE_CHANNEL_ACCESS_TOKEN
GEMINI_API_KEY
```

To generate Secret Manager command templates without putting real values in the
shell history:

```bash
.venv/bin/python cloud_run_plan.py secret-commands
```

Add `--include-optional` only if `GROQ_API_KEY` is actually configured.

## Verification Before LINE Cutover

```bash
SERVICE_URL="$(gcloud run services describe line-bot --region asia-east1 --format 'value(status.url)')"
curl -fsS "$SERVICE_URL/health"
```

After `LINE_BOT_PUBLIC_BASE_URL` is set to the service URL:

```bash
.venv/bin/python preflight_cloud.py \
  --public-base-url "$SERVICE_URL" \
  --require-public-url \
  --live-line
```

`--live-line` calls LINE APIs. Use it only when you intend to test the real
channel.

## Optional Phase 2: Cloud Scheduler Jobs

Only enable this after phase 1 is stable and after deciding which jobs should
move off Mac launchd.

Required env changes:

```text
JOBS_ROUTES_ENABLED=1
JOBS_ALLOW_PUBLIC_HTTP=1
JOBS_SUBPROCESS_INHERIT_ENV=1
JOBS_MASTER_TOKEN=<32+ random bytes/hex>
```

Generate a per-job token:

```bash
JOBS_MASTER_TOKEN=<MASTER_TOKEN> .venv/bin/python cloud_run_plan.py job-token line-bot-event-reminder
```

Generate the scheduler command:

```bash
JOBS_MASTER_TOKEN=<MASTER_TOKEN> .venv/bin/python cloud_run_plan.py scheduler-command line-bot-event-reminder
```

Create one scheduler job:

```bash
gcloud scheduler jobs create http line-bot-event-reminder \
  --location asia-east1 \
  --schedule "0 7 * * *" \
  --time-zone "Asia/Taipei" \
  --uri "$SERVICE_URL/jobs/line-bot-event-reminder" \
  --http-method POST \
  --headers "X-Job-Token=<PER_JOB_TOKEN>"
```

Keep the number of Cloud Scheduler jobs at 3 or fewer if the goal is to stay in
the free tier.

## State Migration Still Needed

Before claiming full parity with the Mac bot, choose a durable state backend:

- Current decision: keep SQLite for the local/Mac bot because reminder and
  family-event volume is small. Do not migrate to Postgres just for capacity.
- Lowest-code temporary path: accept ephemeral `/tmp/line_bot.db` for webhook
  replies only.
- Better free-tier path: migrate conversation/reminder state to a managed
  Postgres/SQLite-compatible service with a free plan, then update `memory.py`,
  `calendar_db.py`, `food_db.py`, and `finance_view_db.py`.
- Cloud-native path: Firestore or Cloud SQL. This is cleaner operationally but
  may not stay free depending on usage.

Do not run Mac launchd push jobs and Cloud Scheduler push jobs for the same LINE
channel at the same time.
