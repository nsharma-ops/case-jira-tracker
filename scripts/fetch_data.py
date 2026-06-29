"""
fetch_data.py
Fetches Salesforce Cases and linked Jira issue statuses.
Writes to data/cases.json for the GitHub Pages dashboard.

Required environment variables (set as GitHub Secrets):
  JIRA_EMAIL, JIRA_API_TOKEN, JIRA_DOMAIN
  SF_INSTANCE_URL + one of:
    - SF_CLIENT_ID + SF_CLIENT_SECRET (+ optional SF_LOGIN_URL)  [preferred]
    - SF_ACCESS_TOKEN
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
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "https://login.salesforce.com").rstrip("/")

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


def sf_link_fields(config: dict) -> list[str]:
    sf_cfg = config["filters"].get("salesforce", {})
    fields = sf_cfg.get("jira_link_fields") or []
    if not fields and sf_cfg.get("jira_field"):
        fields = [sf_cfg["jira_field"]]
    return fields


def sf_status_field(config: dict) -> str | None:
    return config["filters"].get("salesforce", {}).get("jira_status_field")


def get_salesforce_auth() -> tuple[str, str] | None:
    if SF_CLIENT_ID and SF_CLIENT_SECRET:
        resp = requests.post(
            f"{SF_LOGIN_URL}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": SF_CLIENT_ID,
                "client_secret": SF_CLIENT_SECRET,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], data.get("instance_url", SF_INSTANCE_URL).rstrip("/")

    if SF_ACCESS_TOKEN and SF_INSTANCE_URL:
        return SF_ACCESS_TOKEN, SF_INSTANCE_URL

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


def resolve_jira_key(
    record: dict,
    overrides: dict,
    jira_link_fields: list[str],
) -> tuple[str | None, str]:
    case_number = record.get("CaseNumber", "")
    case_id = record.get("Id", "")

    for key in (case_number, case_id):
        if key and key in overrides:
            return normalize_jira_key(overrides[key]), "override"

    for field in jira_link_fields:
        field_value = record.get(field)
        if field_value is None:
            continue
        key = normalize_jira_key(str(field_value))
        if key:
            return key, field

    for source in ("Subject", "Description"):
        key = extract_jira_key(record.get(source))
        if key:
            return key, "parsed"

    return None, "unlinked"


def build_soql(config: dict) -> str:
    sf_cfg = config["filters"].get("salesforce", {})
    days_back = sf_cfg.get("case_days_back", 365)
    limit = sf_cfg.get("limit", 2000)
    link_fields = sf_link_fields(config)
    status_field = sf_status_field(config)

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
    for field in link_fields:
        if field not in fields:
            fields.append(field)
    if status_field and status_field not in fields:
        fields.append(status_field)

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


def run_soql(soql: str, access_token: str, instance_url: str) -> list[dict]:
    url = f"{instance_url}/services/data/v60.0/query"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict | None = {"q": soql}
    records: list[dict] = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        next_url = data.get("nextRecordsUrl")
        url = f"{instance_url}{next_url}" if next_url else None
        params = None

    return records


def fetch_sf_cases(config: dict) -> list[dict] | None:
    auth = get_salesforce_auth()
    if not auth:
        print("⚠  Salesforce credentials not set — skipping SF fetch")
        return None

    access_token, instance_url = auth
    link_fields = sf_link_fields(config)
    status_field = sf_status_field(config)
    soql = build_soql(config)

    try:
        records = run_soql(soql, access_token, instance_url)
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 400:
            print("⚠  SOQL failed with configured Jira fields; retrying with base fields only")
            config["filters"]["salesforce"]["jira_link_fields"] = []
            config["filters"]["salesforce"]["jira_status_field"] = None
            soql = build_soql(config)
            records = run_soql(soql, access_token, instance_url)
            link_fields = []
            status_field = None
        else:
            raise

    cases = []
    overrides = config["overrides"]
    for record in records:
        client = (record.get("Account") or {}).get("Name")
        if not client:
            continue

        jira_key, link_source = resolve_jira_key(record, overrides, link_fields)
        sf_jira_status = record.get(status_field) if status_field else None
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
            "sfJiraTicketStatus": sf_jira_status or None,
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


def statuses_differ(sf_status: str | None, jira_status: str | None) -> bool:
    if not sf_status or not jira_status:
        return False
    return sf_status.strip().lower() != jira_status.strip().lower()


def compute_alignment(case: dict, jira_status: str | None, config: dict) -> str:
    if not case.get("jiraKey"):
        return "unlinked"
    if not jira_status:
        return "jira_not_found"

    sf_jira_status = case.get("sfJiraTicketStatus")
    if statuses_differ(sf_jira_status, jira_status):
        return "sf_jira_status_drift"

    case_closed = case.get("isClosed") is True
    jira_closed = is_jira_closed(jira_status, config)

    if case_closed and jira_closed:
        return "aligned_closed"
    if not case_closed and not jira_closed:
        return "aligned_open"
    return "mismatch"


def merge_cases(
    cases: list[dict],
    jira_issues: dict[str, dict],
    config: dict,
    existing_cases: list[dict] | None = None,
) -> list[dict]:
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
    mismatches = [c for c in cases if c.get("alignment") in ("mismatch", "sf_jira_status_drift")]
    unlinked_open = [c for c in cases if not c.get("jiraKey") and not c.get("isClosed")]
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

    sf_cfg = config["filters"].get("salesforce", {})
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "link_strategy": {
            "primary": sf_cfg.get("jira_link_fields", []),
            "sf_status_field": sf_cfg.get("jira_status_field"),
            "fallback": "Parse Jira keys from Case Subject/Description",
            "overrides": "config/overrides.json",
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
