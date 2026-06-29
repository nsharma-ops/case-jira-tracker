# TractionRec Case–Jira Status Tracker

Internal dashboard showing Salesforce Case status alongside linked Jira issue status.

## Live URL

**https://nsharma-ops.github.io/case-jira-tracker/**

---

## How it works (snapshot mode)

No Salesforce Connected App required.

```
data/sf_cases_snapshot.json   ← Salesforce Case data (snapshot)
        +
Jira REST API (hourly)        ← Live Jira statuses refreshed automatically
        ↓
data/cases.json               ← Dashboard data
        ↓
GitHub Pages                  ← Team-facing URL
```

1. **Salesforce side** — static snapshot (export once from a Salesforce report, or bootstrap from Jira for preview)
2. **Jira side** — refreshed hourly via GitHub Actions with your Jira API token

---

## Updating the Salesforce snapshot

### Option A — Salesforce report export (recommended)

1. In Salesforce: **Reports → New Report → Cases**
2. Add columns: Case Number, Subject, Account Name, Status, Type, Date/Time Opened, Date/Time Closed, Jira Ticket Link, Jira Ticket Number, Jira Ticket Status
3. Filter to last 365 days (or your preference)
4. **Export → Details Only → CSV**
5. Run locally:

```bash
python scripts/import_sf_export.py imports/your_export.csv
git add data/sf_cases_snapshot.json
git commit -m "Update Salesforce case snapshot"
git push
```

### Option B — Jira bootstrap (preview only)

If you don't have an SF export yet, the hourly workflow bootstraps ~75 recent TOD029 Jira issues as placeholder cases. Replace with a real SF export when ready.

---

## GitHub Secrets (Jira only)

| Secret | Required |
|--------|----------|
| `JIRA_EMAIL` | Yes |
| `JIRA_API_TOKEN` | Yes |

Salesforce secrets are optional — only needed if you later switch to live API access.

---

## Case ↔ Jira fields

| Salesforce label | API name |
|------------------|----------|
| Jira Ticket Link | `Jira_Ticket_Link__c` |
| Jira Ticket | `Jira_Ticket__c` |
| Jira Ticket Number | `Jira_Ticket_Number__c` |
| Jira Ticket Status | `Jira_Ticket_Status__c` |

Configured in `config/filters.json`.

---

## Project structure

```
case-jira-tracker/
├── index.html
├── scripts/
│   ├── fetch_data.py           # Merge snapshot + live Jira
│   ├── import_sf_export.py     # CSV → snapshot
│   └── bootstrap_from_jira.py  # Jira-only preview snapshot
├── data/
│   ├── sf_cases_snapshot.json  # Salesforce snapshot (committed)
│   └── cases.json              # Dashboard output (auto-updated)
├── imports/                    # Drop SF CSV exports here
└── .github/workflows/refresh.yml
```
