"""
fetch_data.py
Fetches Salesforce Cases and linked Jira issue statuses.
Writes to data/cases.json for the GitHub Pages dashboard.

Required environment variables (set as GitHub Secrets):
  JIRA_EMAIL        - Atlassian account email
  JIRA_API_TOKEN    - Jira API token
  JIRA_DOMAIN       - e.g. tractionrec.atlassian.net
  SF_INSTANCE_URL   - e.g. https://tractionrec.my.salesforce.com
  SF_ACCESS_TOKEN   - Salesforce access token
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OUTPUT_FILE = ROOT / "data" / "cases.json"

JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "tractionrec.atlassian.net")
SF_INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "").rstrip("/")
SF_ACCESS_TOKEN = os.environ.get("SF_ACCESS_TOKEN", "")

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_config() -> dict:
    filters = load_json(CONFIG_DIR / "filters.json")
    overrides = load_json(CONFIG_DIR / "overrides.json")
    return {
        "filters": filters,
        "overrides": overrides.get("overrides", {}),
    }


def resolve_jira_field(config: dict) -> str | None:
    sf_cfg = config["filters"].get("salesforce", {})
    explicit = sf_cfg.get("jira_field")
    if explicit:
        return explicit
    return None


def extract_jira_key(text: str | None) -> str | None:
    if not text:
        return None
    match = JIRA_KEY_RE.search(text)
    return match.group(1) if match else None


def normalize_jira_key(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if JIRA_KEY_RE.fullmatch(value):
        return value
    return extract_jira_key(value)


def resolve_jira_key(record: dict, overrides: dict, jira_field: str | None) -> tuple[str | None, str]:
    case_number = record.get("CaseNumber", "")
    case_id = record.get("Id", "")

    for key in (case_number, case_id):
        if key and key in overrides:
            return normalize_jira_key(overrides[key]), "override"

    if jira_field:
        field_value = record.get(jira_field)
        key = normalize_jira_key(field_value if isinstance(field_value, str) else None)
        if key:
            return key, "custom_field"

    for source in ("Subject", "Description"):
        key = extract_jira_key(record.get(source))
        if key:
            return key, "parsed"

    return None, "unlinked"


def build_soql(config: dict, jira_field: str | None) -> str:
    sf_cfg = config["filters"].get("salesforce", {})
    days_back = sf_cfg.get("case_days_back", 365)
    limit = sf_cfg.get("limit", 2000)

    fields = [
        "Id",
        "CaseNumber",
        "Subject",
        "Account.Name",
        "Type",
        "Status",
        "IsClosed",
        "CreatedDate",
        "ClosedDate",
        "Description",
    ]
    if jira_field:
        fields.append(jira_field)

    field_list = ", ".join(fields)
    return (
        f"SELECT {field_list} "
        f"FROM Case "
        f"WHERE CreatedDate = LAST_N_DAYS:{days_back} "
        f"AND Account.Name != null "
        f"AND Subject != null "
        f"ORDER BY CreatedDate DESC "
        f"LIMIT {limit}"
    )


def run_soql(soql: str) -> list[dict]:
    url = f"{SF_INSTANCE_URL}/services/data/v60.0/query"
    headers = {"Authorization": f"Bearer {SF_ACCESS_TOKEN}"}
    params: dict | None = {"q": soql}
    records: list[dict] = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        next_url = data.get("nextRecordsUrl")
        url = f"{SF_INSTANCE_URL}{next_url}" if next_url else None
        params = None

    return records


def fetch_sf_cases(config: dict) -> list[dict] | None:
    if not SF_INSTANCE_URL or not SF_ACCESS_TOKEN:
        print("⚠  Salesforce credentials not set — skipping SF fetch")
        return None

    jira_field = resolve_jira_field(config)
    soql = build_soql(config, jira_field)

    try:
        records = run_soql(soql)
    except requests.HTTPError as err:
        if jira_field and err.response is not None and err.response.status_code == 400:
            print(f"⚠  SOQL failed with jira field {jira_field}; retrying without custom field")
            soql = build_soql(config, None)
            records = run_soql(soql)
            jira_field = None
        else:
            raise

    cases = []
    overrides = config["overrides"]
    for record in records:
        client = (record.get("Account") or {}).get("Name")
        if not client:
            continue

        jira_key, link_source = resolve_jira_key(record, overrides, jira_field)
        cases.append({
            "caseId": record.get("Id", ""),
            "caseNumber": record.get("CaseNumber", ""),
            "client": client,
            "subject": record.get("Subject", ""),
            "type": record.get("Type", ""),
            "caseStatus": record.get("Status", ""),
            "isClosed": record.get("IsClosed") is True,
            "createdDate": (record.get("CreatedDate") or "")[:10],
            "closedDate": (record.get("ClosedDate") or "")[:10] or None,
            "jiraKey": jira_key,
            "linkSource": link_source if jira_key else "unlinked",
        })

    print(f"✓ Fetched {len(cases)} cases from Salesforce")
    return cases


def chunk_list(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_jira_issues(keys: list[str], config: dict) -> dict[str, dict]:
    if not keys:
        return {}
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        print("⚠  Jira credentials not set — skipping Jira fetch")
        return {}

    batch_size = config["filters"].get("jira", {}).get("batch_size", 50)
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search"
    issues: dict[str, dict] = {}

    for batch in chunk_list(sorted(set(keys)), batch_size):
        jql = f"key in ({', '.join(batch)})"
        params = {
            "jql": jql,
            "maxResults": len(batch),
            "fields": "summary,status,assignee,updated,priority",
        }
        resp = requests.get(url, auth=auth, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            assignee = fields.get("assignee") or {}
            issues[issue["key"]] = {
                "jiraStatus": (fields.get("status") or {}).get("name", ""),
                "jiraSummary": fields.get("summary", ""),
                "jiraAssignee": assignee.get("displayName", ""),
                "jiraUpdated": (fields.get("updated") or "")[:10],
                "jiraPriority": (fields.get("priority") or {}).get("name", ""),
                "jiraUrl": f"https://{JIRA_DOMAIN}/browse/{issue['key']}",
            }

    print(f"✓ Fetched {len(issues)} Jira issues")
    return issues


def is_jira_closed(status: str, config: dict) -> bool:
    closed = {s.lower() for s in config["filters"].get("jira_closed_statuses", [])}
    open_statuses = {s.lower() for s in config["filters"].get("jira_open_statuses", [])}
    normalized = status.lower()
    if normalized in closed:
        return True
    if normalized in open_statuses:
        return False
    return normalized in {"done", "closed", "resolved", "released", "complete", "cancelled"}


def compute_alignment(case: dict, jira_status: str | None, config: dict) -> str:
    if not case.get("jiraKey"):
        return "unlinked"
    if not jira_status:
        return "jira_not_found"

    case_closed = case.get("isClosed") is True
    jira_closed = is_jira_closed(jira_status, config)

    if case_closed and jira_closed:
        return "aligned_closed"
    if not case_closed and not jira_closed:
        return "aligned_open"
    return "mismatch"


def merge_cases(cases: list[dict], jira_issues: dict[str, dict], config: dict, existing_cases: list[dict] | None = None) -> list[dict]:
    existing_by_key = {}
    for row in existing_cases or []:
        for key in (row.get("caseId"), row.get("caseNumber")):
            if key:
                existing_by_key[key] = row

    merged = []
    for case in cases:
        jira_key = case.get("jiraKey")
        jira = jira_issues.get(jira_key, {}) if jira_key else {}
        if jira_key and not jira:
            prior = existing_by_key.get(case.get("caseId")) or existing_by_key.get(case.get("caseNumber"))
            if prior and prior.get("jiraKey") == jira_key and prior.get("jiraStatus"):
                jira = {
                    "jiraStatus": prior.get("jiraStatus"),
                    "jiraSummary": prior.get("jiraSummary"),
                    "jiraAssignee": prior.get("jiraAssignee"),
                    "jiraUpdated": prior.get("jiraUpdated"),
                    "jiraPriority": prior.get("jiraPriority"),
                    "jiraUrl": prior.get("jiraUrl"),
                }

        jira_status = jira.get("jiraStatus")
        row = {
            **case,
            "jiraStatus": jira_status,
            "jiraSummary": jira.get("jiraSummary"),
            "jiraAssignee": jira.get("jiraAssignee"),
            "jiraUpdated": jira.get("jiraUpdated"),
            "jiraPriority": jira.get("jiraPriority"),
            "jiraUrl": jira.get("jiraUrl"),
            "alignment": compute_alignment(case, jira_status, config),
        }
        merged.append(row)
    return merged


def compute_stats(cases: list[dict]) -> dict:
    linked = [c for c in cases if c.get("jiraKey")]
    mismatches = [c for c in cases if c.get("alignment") == "mismatch"]
    unlinked_open = [
        c for c in cases
        if not c.get("jiraKey") and not c.get("isClosed")
    ]
    return {
        "total_cases": len(cases),
        "linked": len(linked),
        "unlinked": len(cases) - len(linked),
        "status_mismatch": len(mismatches),
        "open_unlinked": len(unlinked_open),
        "linked_pct": round(len(linked) / len(cases) * 100) if cases else 0,
    }


def main() -> None:
    config = load_config()

    existing: dict = {}
    try:
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
    except Exception:
        pass

    cases = fetch_sf_cases(config)
    if cases is None:
        cases = existing.get("cases", [])

    jira_keys = [c["jiraKey"] for c in cases if c.get("jiraKey")]
    jira_issues = fetch_jira_issues(jira_keys, config) if jira_keys else {}

    merged = merge_cases(cases, jira_issues, config, existing.get("cases"))
    stats = compute_stats(merged)

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "link_strategy": {
            "primary": "Set salesforce.jira_field in config/filters.json after discovery",
            "fallback": "Parse Jira keys from Case Subject/Description",
            "overrides": "config/overrides.json for manual links",
        },
        "stats": stats,
        "cases": merged,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(
        f"✓ Wrote {OUTPUT_FILE} "
        f"({stats['total_cases']} cases, {stats['linked']} linked, "
        f"{stats['status_mismatch']} mismatches)"
    )


if __name__ == "__main__":
    main()
