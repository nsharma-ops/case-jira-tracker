"""
bootstrap_from_jira.py
Build an initial Salesforce snapshot from recent Jira issues when SF API
credentials are unavailable. Replace with a real SF CSV export when ready.

Usage:
  python scripts/bootstrap_from_jira.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SNAPSHOT_FILE = ROOT / "data" / "sf_cases_snapshot.json"

JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "tractionrec.atlassian.net")


def load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def bootstrap_cases(jql: str, max_results: int) -> list[dict]:
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        print("⚠  Jira credentials not set — cannot bootstrap")
        return []

    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    payload = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "issuetype", "created", "updated", "priority"],
    }
    resp = requests.post(url, auth=auth, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    cases = []
    for issue in data.get("issues", []):
        fields = issue.get("fields", {})
        cases.append({
            "caseId": "",
            "caseNumber": "",
            "client": "—",
            "subject": fields.get("summary", ""),
            "type": (fields.get("issuetype") or {}).get("name", ""),
            "caseStatus": "Open",
            "isClosed": False,
            "createdDate": (fields.get("created") or "")[:10],
            "closedDate": None,
            "jiraKey": issue["key"],
            "linkSource": "jira_bootstrap",
            "sfJiraTicketStatus": (fields.get("status") or {}).get("name"),
        })

    print(f"✓ Bootstrapped {len(cases)} cases from Jira")
    return cases


def main() -> None:
    filters = load_json(CONFIG_DIR / "filters.json")
    bootstrap_cfg = filters.get("jira_bootstrap", {})
    jql = bootstrap_cfg.get(
        "jql",
        'project = TOD029 AND updated >= -180d ORDER BY updated DESC',
    )
    max_results = bootstrap_cfg.get("max_results", 75)

    existing = load_json(SNAPSHOT_FILE)
    if existing.get("cases") and not bootstrap_cfg.get("force", False):
        print(f"✓ Snapshot already has {len(existing['cases'])} cases — skipping bootstrap")
        return

    cases = bootstrap_cases(jql, max_results)
    if not cases:
        return

    snapshot = {
        "snapshot_date": datetime.now(timezone.utc).isoformat(),
        "source": "jira_bootstrap",
        "note": "Placeholder snapshot from Jira. Replace by running import_sf_export.py with a Salesforce CSV export.",
        "cases": cases,
    }

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"✓ Wrote {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()
