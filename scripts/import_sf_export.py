"""
import_sf_export.py
Convert a Salesforce Case report CSV export into data/sf_cases_snapshot.json.

Usage:
  python scripts/import_sf_export.py path/to/cases_export.csv

Export from Salesforce:
  Reports → New Report → Cases → add columns below → Export (Details Only, CSV)
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_FILE = ROOT / "data" / "sf_cases_snapshot.json"

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

COLUMN_ALIASES: dict[str, list[str]] = {
    "caseId": ["Case ID", "Id", "case id"],
    "caseNumber": ["Case Number", "CaseNumber", "case number"],
    "client": ["Account Name", "Account: Account Name", "Account.Name", "Client"],
    "subject": ["Subject", "subject"],
    "type": ["Type", "Case Type", "type"],
    "caseStatus": ["Status", "Case Status", "status"],
    "isClosed": ["Closed", "Is Closed", "IsClosed", "is closed"],
    "createdDate": ["Date/Time Opened", "Created Date", "CreatedDate", "created date"],
    "closedDate": ["Date/Time Closed", "Closed Date", "ClosedDate", "closed date"],
    "jiraTicketLink": ["Jira Ticket Link", "Jira_Ticket_Link__c", "jira ticket link"],
    "jiraTicket": ["Jira Ticket", "Jira_Ticket__c", "jira ticket"],
    "jiraTicketNumber": ["Jira Ticket Number", "Jira_Ticket_Number__c", "jira ticket number"],
    "sfJiraTicketStatus": ["Jira Ticket Status", "Jira_Ticket_Status__c", "jira ticket status"],
    "description": ["Description", "description"],
}


def normalize_header(name: str) -> str:
    return name.strip().lower()


def map_columns(headers: list[str]) -> dict[str, int]:
    normalized = {normalize_header(h): i for i, h in enumerate(headers)}
    mapping: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            idx = normalized.get(alias.lower())
            if idx is not None:
                mapping[field] = idx
                break
    return mapping


def cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "closed"}


def parse_date(value: str) -> str:
    if not value:
        return ""
    return value[:10]


def extract_jira_key(*values: str) -> str | None:
    for value in values:
        if not value:
            continue
        value = value.strip()
        if JIRA_KEY_RE.fullmatch(value):
            return value
        match = JIRA_KEY_RE.search(value)
        if match:
            return match.group(1)
    return None


def row_to_case(row: list[str], mapping: dict[str, int]) -> dict | None:
    subject = cell(row, mapping, "subject")
    if not subject:
        return None

    jira_key = extract_jira_key(
        cell(row, mapping, "jiraTicketLink"),
        cell(row, mapping, "jiraTicket"),
        cell(row, mapping, "jiraTicketNumber"),
        subject,
        cell(row, mapping, "description"),
    )

    closed_raw = cell(row, mapping, "isClosed")
    is_closed = parse_bool(closed_raw) if closed_raw else cell(row, mapping, "caseStatus").lower() in {
        "closed", "resolved", "completed",
    }

    link_source = "unlinked"
    if jira_key:
        if cell(row, mapping, "jiraTicketLink") and jira_key in cell(row, mapping, "jiraTicketLink"):
            link_source = "Jira_Ticket_Link__c"
        elif cell(row, mapping, "jiraTicketNumber") and jira_key in cell(row, mapping, "jiraTicketNumber"):
            link_source = "Jira_Ticket_Number__c"
        elif cell(row, mapping, "jiraTicket") and jira_key in cell(row, mapping, "jiraTicket"):
            link_source = "Jira_Ticket__c"
        else:
            link_source = "parsed"

    return {
        "caseId": cell(row, mapping, "caseId"),
        "caseNumber": cell(row, mapping, "caseNumber"),
        "client": cell(row, mapping, "client") or "—",
        "subject": subject,
        "type": cell(row, mapping, "type"),
        "caseStatus": cell(row, mapping, "caseStatus"),
        "isClosed": is_closed,
        "createdDate": parse_date(cell(row, mapping, "createdDate")),
        "closedDate": parse_date(cell(row, mapping, "closedDate")) or None,
        "jiraKey": jira_key,
        "linkSource": link_source if jira_key else "unlinked",
        "sfJiraTicketStatus": cell(row, mapping, "sfJiraTicketStatus") or None,
    }


def import_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV file is empty")
        mapping = map_columns(headers)
        if "subject" not in mapping:
            raise ValueError(f"Could not find Subject column. Found: {headers}")

        cases = []
        for row in reader:
            case = row_to_case(row, mapping)
            if case:
                cases.append(case)
        return cases


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_sf_export.py <cases_export.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    cases = import_csv(csv_path)

    snapshot = {
        "snapshot_date": datetime.now(timezone.utc).isoformat(),
        "source": f"salesforce_csv:{csv_path.name}",
        "cases": cases,
    }

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)

    linked = sum(1 for c in cases if c.get("jiraKey"))
    print(f"✓ Wrote {SNAPSHOT_FILE} ({len(cases)} cases, {linked} with Jira keys)")


if __name__ == "__main__":
    main()
