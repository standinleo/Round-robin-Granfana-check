#!/usr/bin/env python3
"""
PTAC-198: Round-robin keep-alive for MS Workflows (Power Automate) webhooks.

Power Automate flows are auto-suspended after 90 days of inactivity, after
which the Teams Workflows webhook silently stops delivering. This job:

  1. Queries the Grafana (PROD) provisioning API for all contact points.
  2. Filters webhook-type contact points whose URL targets Power Automate
     (logic.azure.com / powerplatform.com).
  3. Sends a small Adaptive Card "keep-alive" message to each, one at a
     time (round-robin) with a delay between sends.

Required environment variables:
  GRAFANA_URL    e.g. https://grafana.example.com
  GRAFANA_TOKEN  Grafana service-account token with alerting.provisioning:read

Optional:
  PING_DELAY_SECONDS  delay between webhook pings (default: 5)
  DRY_RUN             "true" to list targets without sending (default: false)
  EXTRA_WEBHOOK_URLS  comma-separated webhook URLs not managed in Grafana
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("keep-alive")

GRAFANA_URL = os.environ.get("GRAFANA_URL", "").rstrip("/")
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")
PING_DELAY = int(os.environ.get("PING_DELAY_SECONDS", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
EXTRA_URLS = [u.strip() for u in os.environ.get("EXTRA_WEBHOOK_URLS", "").split(",") if u.strip()]

# URL fragments that identify Power Automate / MS Workflows webhooks
POWER_AUTOMATE_MARKERS = (
    "logic.azure.com",          # classic Power Automate HTTP trigger
    "powerplatform.com",        # newer environment-scoped trigger URLs
    "powerautomate",
)


def http_json(url: str, method: str = "GET", payload: dict | None = None,
              headers: dict | None = None, timeout: int = 30):
    """Minimal stdlib HTTP helper returning (status, body_text)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def fetch_power_automate_webhooks() -> list[dict]:
    """Return [{'name': ..., 'url': ...}] for Power Automate contact points."""
    status, body = http_json(
        f"{GRAFANA_URL}/api/v1/provisioning/contact-points",
        headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"},
    )
    if status != 200:
        log.error("Grafana API returned %s: %s", status, body[:500])
        sys.exit(1)

    targets = []
    for cp in json.loads(body):
        url = (cp.get("settings") or {}).get("url", "")
        if cp.get("type") == "webhook" and any(m in url for m in POWER_AUTOMATE_MARKERS):
            targets.append({"name": cp.get("name", "unnamed"), "url": url})
    return targets


def keep_alive_payload(name: str) -> dict:
    """Adaptive Card payload accepted by Teams Workflows webhooks."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Small",
                            "isSubtle": True,
                            "wrap": True,
                            "text": (
                                f"\U0001F501 Automated keep-alive ping for connector "
                                f"'{name}' — {now}. This prevents the Power Automate "
                                f"flow from being suspended for inactivity. No action needed."
                            ),
                        }
                    ],
                },
            }
        ],
    }


def main() -> int:
    if not GRAFANA_URL or not GRAFANA_TOKEN:
        log.error("GRAFANA_URL and GRAFANA_TOKEN must be set")
        return 1

    targets = fetch_power_automate_webhooks()
    targets += [{"name": f"extra-{i}", "url": u} for i, u in enumerate(EXTRA_URLS)]

    if not targets:
        log.warning("No Power Automate webhooks found — nothing to do")
        return 0

    log.info("Found %d Power Automate webhook(s)", len(targets))
    failures = 0

    for i, t in enumerate(targets):
        if DRY_RUN:
            log.info("[dry-run] would ping '%s' -> %s...", t["name"], t["url"][:60])
            continue

        status, body = http_json(t["url"], method="POST",
                                 payload=keep_alive_payload(t["name"]))
        # Workflows webhooks return 200/202 on success
        if status in (200, 202):
            log.info("OK   '%s' (HTTP %s)", t["name"], status)
        else:
            failures += 1
            log.error("FAIL '%s' (HTTP %s): %s", t["name"], status, body[:300])

        if i < len(targets) - 1:
            time.sleep(PING_DELAY)  # round-robin spacing

    if failures:
        log.error("%d/%d webhook(s) failed — connector may already be suspended",
                  failures, len(targets))
        return 1

    log.info("All %d webhook(s) pinged successfully", len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
