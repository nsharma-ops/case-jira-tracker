# TractionRec Case–Jira Status Tracker

Internal dashboard showing Salesforce Case status alongside linked Jira issue status. Highlights mismatches (e.g. Jira Done but Case still open) so Support, Product, and Engineering stay aligned.

## Live URL

Once deployed: `https://<org-or-username>.github.io/case-jira-tracker/`

Share this URL internally via Confluence and Slack so anyone at Traction Rec can view it without logging in.

---

## How it works

```
GitHub Actions (hourly)
  → scripts/fetch_data.py
      → Salesforce: pulls Cases (last 365 days)
      → Resolves Jira keys (custom field → parse → overrides)
      → Jira: batch-fetches issue status by key
  → writes data/cases.json
  → commits to repo
GitHub Pages serves index.html + data/cases.json
```

---

## Case ↔ Jira linking strategy

Traction Rec Cases use these Salesforce fields (in priority order):

| Label | API name | Purpose |
|-------|----------|---------|
| Jira Ticket Link | `Jira_Ticket_Link__c` | Primary field for TOD bug/enhancement tickets |
| Jira Ticket | `Jira_Ticket__c` | Older/internal name for the same link |
| Jira Ticket Number | `Jira_Ticket_Number__c` | Used by n8n status sync |
| Jira Ticket Status | `Jira_Ticket_Status__c` | SF-side Jira status (synced via n8n; shown for drift detection) |

Configured in `config/filters.json`. Fallback: parse keys from Subject/Description. Manual overrides in `config/overrides.json`.

---

## Setup

### 1. Create a GitHub repo

```bash
cd case-jira-tracker
git init
git add .
git commit -m "Initial case-jira tracker"
git remote add origin https://github.com/<org>/case-jira-tracker.git
git push -u origin main
```

### 2. Enable GitHub Pages

Repo **Settings → Pages → Source: Deploy from a branch → Branch: `main` / `root`**

### 3. Add GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Value |
|--------|-------|
| `JIRA_EMAIL` | Your Atlassian email (e.g. `you@tractionrec.com`) |
| `JIRA_API_TOKEN` | API token from https://id.atlassian.com/manage-profile/security/api-tokens |
| `SF_INSTANCE_URL` | Salesforce org URL (e.g. `https://tractionrec.my.salesforce.com`) |
| `SF_CLIENT_ID` | Connected App Consumer Key (preferred) |
| `SF_CLIENT_SECRET` | Connected App Consumer Secret (preferred) |
| `SF_LOGIN_URL` | Optional — `https://login.salesforce.com` or your My Domain login URL |
| `SF_ACCESS_TOKEN` | Optional fallback if not using Connected App |

#### Getting a Salesforce access token

```bash
sfdx force:org:display -u <alias> --json | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['accessToken'])"
```

For production reliability, migrate to a Connected App client credentials flow so tokens do not expire hourly.

### 4. Trigger the first data refresh

**Actions → Refresh Case-Jira Data → Run workflow**

After the first run, `data/cases.json` will contain live data and the dashboard will update automatically every hour.

### 5. Share internally

- Create a Confluence page: **Case–Jira Status Tracker** with the GitHub Pages URL
- Pin in a relevant Slack channel (#support, #product-ops, etc.)

---

## Local development

```bash
pip install requests
python scripts/fetch_data.py   # requires env vars above
python -m http.server 8080     # open http://localhost:8080
```

Sample data ships in `data/cases.json` for UI preview before credentials are configured.

---

## Configuration

| File | Purpose |
|------|---------|
| `config/filters.json` | SOQL window, Jira field name, status groupings |
| `config/overrides.json` | Manual Case → Jira key mappings |

### Alignment logic

| Salesforce Case | Jira Issue | Result |
|-----------------|------------|--------|
| Open | Not in closed statuses | `aligned_open` |
| Closed | In closed statuses | `aligned_closed` |
| Open | Closed/Done | `mismatch` |
| Closed | Still active | `mismatch` |
| No Jira key | — | `unlinked` |
| Key not found in Jira | — | `jira_not_found` |

Adjust closed/open status lists in `config/filters.json` to match your Jira workflows.

---

## Project structure

```
case-jira-tracker/
├── index.html
├── scripts/fetch_data.py
├── data/cases.json
├── config/
│   ├── filters.json
│   └── overrides.json
├── .github/workflows/refresh.yml
└── README.md
```
