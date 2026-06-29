# Salesforce Case export

When you have a Salesforce report export, place the CSV here and run:

```bash
python scripts/import_sf_export.py imports/your_export.csv
git add data/sf_cases_snapshot.json
git commit -m "Update Salesforce case snapshot"
git push
```

## Recommended report columns

- Case Number
- Subject
- Account Name
- Status
- Type
- Date/Time Opened
- Date/Time Closed
- Jira Ticket Link
- Jira Ticket Number
- Jira Ticket Status

Export as **Details Only** CSV from Salesforce Reports.
