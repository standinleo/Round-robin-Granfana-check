# MS Workflows Webhook Keep-Alive (PTAC-198)

Automated keep-alive pings for Microsoft Power Automate / Teams Workflows
webhooks used as Grafana alert contact points.

## Why this exists

Power Automate suspends flows after **90 consecutive days of inactivity**.
When that happens, the Teams Workflows webhook silently stops delivering —
Grafana alerts go nowhere and nobody notices until an incident is missed.

This job pings every Power Automate webhook once a month with a small
Adaptive Card message so the flows always register activity.

> ⚠️ The original ticket said "every 180 days". That is too late — flows
> are already suspended by day 90. The schedule here is **monthly**.

## How it works

```
┌─────────────┐   1. GET contact points    ┌──────────────┐
│  CronJob /   │ ─────────────────────────▶ │ Grafana PROD │
│  GH Action   │ ◀───────────────────────── │  API         │
│ keep_alive.py│   2. filter Power Automate └──────────────┘
│              │      webhook URLs
│              │   3. POST Adaptive Card     ┌──────────────┐
│              │ ─────────────────────────▶ │ Power Automate│──▶ Teams
└─────────────┘      (one by one, 5s apart) └──────────────┘
```

1. Calls `GET /api/v1/provisioning/contact-points` on Grafana.
2. Keeps only `type: webhook` contact points whose URL contains
   `logic.azure.com`, `powerplatform.com`, or `powerautomate`.
3. POSTs a small Adaptive Card keep-alive to each, sequentially
   (round-robin) with a delay between sends.
4. Exits non-zero if any ping fails, so the Job shows as failed and can
   be alerted on.

## Files

| File | Purpose |
|---|---|
| `keep_alive.py` | The keep-alive script (Python 3.11+, stdlib only — no pip installs) |
| `cronjob.yaml` | Kubernetes CronJob + instructions for the script ConfigMap |
| `github-actions-keepalive.yml` | Alternative: run on a GitHub Actions schedule instead of in-cluster |

## Configuration (environment variables)

| Variable | Required | Default | Description |
|---|---|---|---|
| `GRAFANA_URL` | ✅ | — | Base URL of Grafana PROD, e.g. `https://grafana.example.com` |
| `GRAFANA_TOKEN` | ✅ | — | Grafana service-account token (see step 1) |
| `PING_DELAY_SECONDS` | | `5` | Delay between webhook pings |
| `DRY_RUN` | | `false` | `true` = list targets, send nothing |
| `EXTRA_WEBHOOK_URLS` | | — | Comma-separated webhook URLs not managed in Grafana |

## Setup — Kubernetes (recommended)

### Step 1 — Create a Grafana service account token

In Grafana PROD: **Administration → Service accounts → Add service account**.

- Name: `webhook-keepalive`
- Permission: only `alerting.provisioning:read` (or the *Viewer* role if
  fine-grained access control is not enabled). Do **not** reuse an admin token.
- Generate a token and copy it once.

### Step 2 — Verify locally with a dry run

```bash
export GRAFANA_URL=https://grafana.example.com
export GRAFANA_TOKEN=<token>
DRY_RUN=true python3 keep_alive.py
```

Check the log output lists exactly the Power Automate webhooks you expect
— and nothing else (Slack, PagerDuty, etc. must be skipped).

### Step 3 — Send one real test ping

```bash
python3 keep_alive.py
```

Confirm the keep-alive card appears in the target Teams channels and the
script exits `0`. The card is small, subtle, and says "no action needed".

### Step 4 — Create the secret and ConfigMap

```bash
kubectl -n monitoring create secret generic grafana-keepalive \
  --from-literal=GRAFANA_URL=https://grafana.example.com \
  --from-literal=GRAFANA_TOKEN=<token>

kubectl -n monitoring create configmap msworkflows-keepalive-script \
  --from-file=keep_alive.py --dry-run=client -o yaml | kubectl apply -f -
```

### Step 5 — Deploy the CronJob

```bash
kubectl apply -f cronjob.yaml
```

Schedule is `0 9 1 * *` — 09:00 UTC on the 1st of every month.

### Step 6 — Trigger a manual run to validate in-cluster

```bash
kubectl -n monitoring create job keepalive-manual-test \
  --from=cronjob/msworkflows-webhook-keepalive
kubectl -n monitoring logs job/keepalive-manual-test -f
```

### Step 7 — Alert on failures

A failed run likely means a connector is **already suspended** and a human
must re-save the flow in Power Automate. Add a Prometheus rule such as:

```yaml
- alert: WebhookKeepAliveFailed
  expr: kube_job_status_failed{job_name=~"msworkflows-webhook-keepalive.*"} > 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "MS Workflows webhook keep-alive failed — connector may be suspended"
```

## Setup — GitHub Actions (alternative)

If you'd rather not run this in-cluster:

1. Commit `keep_alive.py` to `scripts/` in a repo.
2. Commit `github-actions-keepalive.yml` to `.github/workflows/`.
3. Add `GRAFANA_URL` and `GRAFANA_TOKEN` as **repository secrets**
   (Settings → Secrets and variables → Actions). The GitHub runner must be
   able to reach your Grafana instance — if Grafana is internal-only, use
   a self-hosted runner or stick with the Kubernetes option.
4. Test via **Actions → msworkflows-webhook-keepalive → Run workflow**
   with `dry_run = true`, then run again with `dry_run = false`.

## Operations runbook

**A ping failed (HTTP error in logs)**
The flow is probably suspended or deleted. Open Power Automate → find the
flow → re-save / re-enable it, then re-run the job manually (Step 6).

**A new Teams webhook was added in Grafana**
Nothing to do — the script discovers contact points dynamically on each run.

**A webhook exists outside Grafana contact points**
Add it to `EXTRA_WEBHOOK_URLS` in the secret/env, comma-separated.

**Rotating the Grafana token**
Recreate the secret with the new token and delete the old service-account
token in Grafana. No redeploy needed — the next Job picks it up.

**Changing the message text**
Edit `keep_alive_payload()` in `keep_alive.py`, then re-apply the
ConfigMap (Step 4) — the next run uses the new script automatically.

## Acceptance criteria (maps to PTAC-198)

- [x] Scheduled job queries Grafana PROD API for activated Power Automate webhooks
- [x] Sends keep-alive message to each webhook (round-robin with spacing)
- [x] Interval keeps flows under the 90-day inactivity limit (monthly, not 180 days)
- [x] Failures are visible (non-zero exit → failed Job → alert rule)
