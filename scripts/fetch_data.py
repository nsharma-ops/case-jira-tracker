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
SNAPSHOT_FILE = ROOT / "data" / "sf_cases_snapshot.json"
REFRESH_STATUS_FILE = ROOT / "data" / "refresh_status.json"

JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "tractionrec.atlassian.net")
SF_INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "").rstrip("/")
SF_ACCESS_TOKEN = os.environ.get("SF_ACCESS_TOKEN", "")
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "https://login.salesforce.com").rstrip("/")

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
SF_CASE_ID_RE = re.compile(r"\b(500[A-Za-z0-9]{12,18})\b")
SF_CASE_URL_RE = re.compile(r"/Case/([A-Za-z0-9]{15,18})/")
CASE_NUMBER_RE = re.compile(r"\b(0\d{7,})\b")


def load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def adf_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        chunks: list[str] = []
        for block in value.get("content", []):
            for item in block.get("content", []):
                if item.get("type") == "text":
                    chunks.append(item.get("text", ""))
        return " ".join(chunks).strip()
    return str(value)


def parse_case_entries_from_text(cases_text: str) -> list[dict]:
    entries: list[dict] = []
    if not cases_text:
        return entries

    for part in cases_text.split(";"):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"(500[A-Za-z0-9]{15})\s*-\s*(.+)", part)
        if not match:
            continue
        case_id, rest = match.group(1), match.group(2).strip()
        client = re.sub(
            r"\s+\d+\s*-\s*(Low|Moderate|High)\b.*$",
            "",
            rest,
            flags=re.IGNORECASE,
        ).strip()
        entries.append({
            "caseId": case_id,
            "client": client or "—",
            "linkSource": "jira_cases_field",
        })
    return entries


def extract_case_ids_from_text(*texts: str | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for case_id in SF_CASE_ID_RE.findall(text):
            if case_id not in seen:
                seen.add(case_id)
                found.append(case_id)
        for case_id in SF_CASE_URL_RE.findall(text):
            if case_id not in seen:
                seen.add(case_id)
                found.append(case_id)
    return found


def extract_case_number_map_from_adf(value: object) -> dict[str, str]:
    """Map Salesforce Case Id -> CaseNumber from hyperlinked text in Jira ADF."""
    mapping: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                text = (node.get("text") or "").strip()
                number_match = CASE_NUMBER_RE.search(text)
                if not number_match:
                    return
                for mark in node.get("marks", []):
                    if mark.get("type") != "link":
                        continue
                    href = (mark.get("attrs") or {}).get("href", "")
                    url_match = SF_CASE_URL_RE.search(href)
                    if url_match:
                        mapping[url_match.group(1)] = number_match.group(1)
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return mapping


def apply_singleton_case_number(entries: list[dict], description_text: str) -> None:
    if len(entries) != 1 or entries[0].get("caseNumber"):
        return
    numbers = CASE_NUMBER_RE.findall(description_text or "")
    unique = list(dict.fromkeys(numbers))
    if len(unique) == 1:
        entries[0]["caseNumber"] = unique[0]


def extract_case_number_map_from_issue_fields(fields: dict) -> dict[str, str]:
    mapping: dict[str, str] = {}
    mapping.update(extract_case_number_map_from_adf(fields.get("description")))
    for comment in (fields.get("comment") or {}).get("comments", []):
        mapping.update(extract_case_number_map_from_adf(comment.get("body")))
    return mapping


def extract_case_links_from_issue(fields: dict, jira_cfg: dict) -> list[dict]:
    cases_field = jira_cfg.get("cases_field", "customfield_13822")
    accounts_field = jira_cfg.get("case_accounts_field", "customfield_13823")
    sf_record_field = jira_cfg.get("salesforce_record_field", "customfield_13250")

    cases_text = adf_to_text(fields.get(cases_field))
    accounts_text = adf_to_text(fields.get(accounts_field))
    sf_record = fields.get(sf_record_field) or ""
    description = adf_to_text(fields.get("description"))
    number_map = extract_case_number_map_from_issue_fields(fields)

    entries = parse_case_entries_from_text(cases_text)
    if entries:
        for entry in entries:
            case_id = entry.get("caseId", "")
            if case_id and case_id in number_map:
                entry["caseNumber"] = number_map[case_id]
        apply_singleton_case_number(entries, description)
        return entries

    fallback_ids = extract_case_ids_from_text(str(sf_record), description)
    if fallback_ids:
        default_client = accounts_text.split(";")[0].strip() if accounts_text else "—"
        return [
            {
                "caseId": case_id,
                "caseNumber": number_map.get(case_id, ""),
                "client": default_client or "—",
                "linkSource": "jira_sf_reference",
            }
            for case_id in fallback_ids
        ]

    if accounts_text:
        return [{"caseId": "", "caseNumber": "", "client": accounts_text.split(";")[0].strip(), "linkSource": "jira_case_accounts"}]

    return [{"caseId": "", "caseNumber": "", "client": "—", "linkSource": "unlinked"}]


def fetch_sf_case_details(case_ids: list[str], auth: tuple[str, str], config: dict) -> dict[str, dict]:
    if not case_ids:
        return {}

    access_token, instance_url = auth
    status_field = sf_status_field(config) or "Jira_Ticket_Status__c"
    link_fields = sf_link_fields(config)
    jira_field = link_fields[0] if link_fields else "Jira_Ticket_Link__c"

    fields = ["Id", "CaseNumber", "Subject", "Status", "IsClosed", "Account.Name", status_field, jira_field]
    field_list = ", ".join(dict.fromkeys(fields))
    details: dict[str, dict] = {}

    for chunk in chunk_list(case_ids, 100):
        id_list = "', '".join(chunk)
        soql = (
            f"SELECT {field_list} FROM Case "
            f"WHERE Id IN ('{id_list}')"
        )
        try:
            records = run_soql(soql, access_token, instance_url)
        except requests.HTTPError as err:
            print(f"⚠  Salesforce Case lookup failed: {err}")
            return details

        for record in records:
            case_id = record.get("Id", "")
            details[case_id] = {
                "caseNumber": record.get("CaseNumber", ""),
                "subject": record.get("Subject", ""),
                "caseStatus": record.get("Status", ""),
                "isClosed": record.get("IsClosed") is True,
                "client": (record.get("Account") or {}).get("Name", ""),
                "sfJiraTicketStatus": record.get(status_field),
                "jiraKeyFromSf": record.get(jira_field),
            }

    print(f"✓ Resolved {len(details)} Salesforce Cases")
    return details


def enrich_with_salesforce_cases(cases: list[dict], config: dict) -> list[dict]:
    auth = get_salesforce_auth()
    if not auth:
        print("⚠  No Salesforce auth — using Case IDs from Jira only")
        return cases

    case_ids = list({c["caseId"] for c in cases if c.get("caseId")})
    sf_details = fetch_sf_case_details(case_ids, auth, config)

    for row in cases:
        case_id = row.get("caseId")
        if not case_id or case_id not in sf_details:
            continue
        sf = sf_details[case_id]
        row["caseNumber"] = sf.get("caseNumber") or row.get("caseNumber", "")
        row["subject"] = sf.get("subject") or row.get("subject", "")
        row["caseStatus"] = sf.get("caseStatus") or row.get("caseStatus", "")
        row["isClosed"] = sf.get("isClosed", row.get("isClosed"))
        row["client"] = sf.get("client") or row.get("client", "—")
        if sf.get("sfJiraTicketStatus"):
            row["sfJiraTicketStatus"] = sf.get("sfJiraTicketStatus")
        if not row.get("jiraKey") and sf.get("jiraKeyFromSf"):
            key = normalize_jira_key(str(sf.get("jiraKeyFromSf")))
            if key:
                row["jiraKey"] = key
                row["linkSource"] = "salesforce_api"

    return cases


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


def load_sf_snapshot() -> tuple[list[dict], dict]:
    data = load_json(SNAPSHOT_FILE)
    cases = data.get("cases", [])
    meta = {
        "snapshot_date": data.get("snapshot_date"),
        "source": data.get("source", "sf_cases_snapshot.json"),
        "note": data.get("note"),
    }
    return cases, meta


def fetch_sf_cases(config: dict) -> tuple[list[dict] | None, dict]:
    auth = get_salesforce_auth()
    if auth:
        cases = _fetch_sf_cases_live(config, auth)
        if cases is not None:
            return cases, {"source": "salesforce_api", "snapshot_date": None}

    snapshot_cases, snapshot_meta = load_sf_snapshot()
    if snapshot_cases:
        print(f"✓ Loaded {len(snapshot_cases)} cases from snapshot ({snapshot_meta.get('source')})")
        return snapshot_cases, snapshot_meta

    print("⚠  No Salesforce API credentials and no snapshot file — skipping SF load")
    return None, {}


def _fetch_sf_cases_live(config: dict, auth: tuple[str, str]) -> list[dict] | None:
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


def write_refresh_status(status: str, **extra: object) -> None:
    payload = {
        "status": status,
        "started_at": extra.get("started_at"),
        "completed_at": extra.get("completed_at"),
        "issues_scanned": extra.get("issues_scanned"),
        "message": extra.get("message", ""),
    }
    REFRESH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REFRESH_STATUS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def scan_all_jira_issues(config: dict) -> list[dict] | None:
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        print("⚠  Jira credentials not set — cannot scan Jira")
        return None

    scan_cfg = config["filters"].get("jira_scan", {})
    jira_cfg = config["filters"].get("jira", {})
    jql = scan_cfg.get(
        "jql",
        'project = TOD029 AND updated >= -365d ORDER BY updated DESC',
    )
    page_size = scan_cfg.get("page_size", 100)
    max_issues = scan_cfg.get("max_issues", 500)

    scan_fields = [
        "summary",
        "status",
        "issuetype",
        "assignee",
        "updated",
        "priority",
        "created",
        "description",
        "comment",
        jira_cfg.get("cases_field", "customfield_13822"),
        jira_cfg.get("case_accounts_field", "customfield_13823"),
        jira_cfg.get("salesforce_record_field", "customfield_13250"),
    ]

    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"
    rows: list[dict] = []
    issue_count = 0
    next_page_token: str | None = None

    while issue_count < max_issues:
        payload: dict = {
            "jql": jql,
            "maxResults": min(page_size, max_issues - issue_count),
            "fields": scan_fields,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        try:
            resp = requests.post(url, auth=auth, json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as err:
            print(f"⚠  Jira scan failed: {err}")
            return None

        batch = data.get("issues", [])
        if not batch:
            break

        for issue in batch:
            issue_count += 1
            fields = issue.get("fields", {})
            status_name = (fields.get("status") or {}).get("name", "")
            assignee = fields.get("assignee") or {}
            case_links = extract_case_links_from_issue(fields, jira_cfg)

            base = {
                "subject": fields.get("summary", ""),
                "type": (fields.get("issuetype") or {}).get("name", ""),
                "caseStatus": "Open",
                "isClosed": is_jira_closed(status_name, config),
                "createdDate": (fields.get("created") or "")[:10],
                "closedDate": None,
                "jiraKey": issue["key"],
                "sfJiraTicketStatus": status_name,
                "jiraStatus": status_name,
                "jiraSummary": fields.get("summary", ""),
                "jiraAssignee": assignee.get("displayName", ""),
                "jiraUpdated": (fields.get("updated") or "")[:10],
                "jiraPriority": (fields.get("priority") or {}).get("name", ""),
                "jiraUrl": f"https://{JIRA_DOMAIN}/browse/{issue['key']}",
            }

            for case_link in case_links:
                rows.append({
                    **base,
                    "caseId": case_link.get("caseId", ""),
                    "caseNumber": case_link.get("caseNumber", ""),
                    "client": case_link.get("client", "—"),
                    "linkSource": case_link.get("linkSource", "jira_scan"),
                })

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    rows = enrich_with_salesforce_cases(rows, config)
    print(f"✓ Scanned {issue_count} Jira issues ({len(rows)} case rows)")
    return rows


def index_sf_cases_by_jira_key(cases: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for case in cases:
        key = case.get("jiraKey")
        if key:
            index[key] = case
    return index


def enrich_scan_with_sf_data(scanned: list[dict], sf_cases: list[dict], config: dict) -> list[dict]:
    sf_by_jira = index_sf_cases_by_jira_key(sf_cases)
    enriched: list[dict] = []
    seen_jira: set[str] = set()

    for row in scanned:
        key = row.get("jiraKey")
        if not key:
            continue
        seen_jira.add(key)
        sf = sf_by_jira.get(key)
        if sf:
            merged = {
                **row,
                "caseId": sf.get("caseId", ""),
                "caseNumber": sf.get("caseNumber", ""),
                "client": sf.get("client") or row.get("client"),
                "subject": sf.get("subject") or row.get("subject"),
                "type": sf.get("type") or row.get("type"),
                "caseStatus": sf.get("caseStatus") or row.get("caseStatus"),
                "isClosed": sf.get("isClosed", row.get("isClosed")),
                "createdDate": sf.get("createdDate") or row.get("createdDate"),
                "closedDate": sf.get("closedDate"),
                "sfJiraTicketStatus": sf.get("sfJiraTicketStatus") or row.get("sfJiraTicketStatus"),
                "linkSource": sf.get("linkSource", row.get("linkSource")),
            }
        else:
            merged = {
                **row,
                "caseStatus": row.get("jiraStatus") or row.get("caseStatus"),
            }
        merged["alignment"] = compute_alignment(merged, merged.get("jiraStatus"), config)
        enriched.append(merged)

    for sf in sf_cases:
        key = sf.get("jiraKey")
        if key and key not in seen_jira:
            enriched.append({
                **sf,
                "jiraStatus": None,
                "jiraUrl": f"https://{JIRA_DOMAIN}/browse/{key}" if key else None,
                "alignment": compute_alignment(sf, None, config),
            })

    return enriched


def chunk_list(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_jira_issues(keys: list[str], config: dict) -> dict[str, dict] | None:
    if not keys:
        return {}
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        print("⚠  Jira credentials not set — skipping Jira fetch")
        return None

    batch_size = config["filters"].get("jira", {}).get("batch_size", 50)
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"
    issues: dict[str, dict] = {}

    for batch in chunk_list(sorted(set(keys)), batch_size):
        jql = f"key in ({', '.join(batch)})"
        payload = {
            "jql": jql,
            "maxResults": len(batch),
            "fields": ["summary", "status", "assignee", "updated", "priority"],
        }
        try:
            resp = requests.post(url, auth=auth, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as err:
            print(f"⚠  Jira fetch failed: {err}")
            return None

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
    started = datetime.now(timezone.utc).isoformat()
    write_refresh_status("running", started_at=started, message="Scanning Jira issues…")

    existing: dict = {}
    try:
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
    except Exception:
        pass

    sf_cases, snapshot_meta = fetch_sf_cases(config)
    if sf_cases is None:
        snapshot_cases, snapshot_meta = load_sf_snapshot()
        sf_cases = snapshot_cases or []

    scanned = scan_all_jira_issues(config)
    if scanned is not None:
        merged = []
        for row in scanned:
            row["alignment"] = compute_alignment(row, row.get("jiraStatus"), config)
            merged.append(row)
        data_mode = "jira_scan"
    elif sf_cases:
        jira_keys = [c["jiraKey"] for c in sf_cases if c.get("jiraKey")]
        jira_issues = fetch_jira_issues(jira_keys, config) or {}
        merged = merge_cases(sf_cases, jira_issues, config, existing.get("cases"))
        data_mode = snapshot_meta.get("source", "snapshot")
    else:
        merged = existing.get("cases", [])
        data_mode = "cached"

    stats = compute_stats(merged)
    completed = datetime.now(timezone.utc).isoformat()

    sf_cfg = config["filters"].get("salesforce", {})
    output = {
        "last_updated": completed,
        "data_source": {
            **snapshot_meta,
            "jira": "live_api",
            "mode": data_mode,
        },
        "stats": stats,
        "cases": merged,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    write_refresh_status(
        "idle",
        started_at=started,
        completed_at=completed,
        issues_scanned=len(merged),
        message=f"Scanned {len(merged)} issues",
    )

    print(
        f"✓ Wrote {OUTPUT_FILE} "
        f"({stats['total_cases']} cases, {stats['linked']} linked, "
        f"{stats['status_mismatch']} mismatches)"
    )


if __name__ == "__main__":
    main()
