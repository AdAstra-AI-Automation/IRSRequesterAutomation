# New-site SharePoint sync

After a successful Connect Add or Modify destination create at a new IRS site,
the backend appends Site Name, the four-digit TE ID, and known State to the end
of `IRS Site Listing updated.xlsx`, sheet `Master Site and State`.

- Existing rows are never filled or replaced; unknown states stay blank.
- If either site name or TEID already exists, skip the append. The final result
  asks the operator to review the workbook and safely add the entry manually if
  appropriate. Numeric and zero-padded text TEIDs compare equally.
- Connect creation remains successful if SharePoint skips or fails.
- Review, failed creates, existing-site operations, and demo accounts do not append.
- Repeated requester rows for one new site reuse its sync result within a batch.

## Configuration

Set `GRAPH_TENANT`, `GRAPH_CLIENT_ID`, `GRAPH_USERNAME`, and `GRAPH_PASSWORD` in
the server's ignored `LIVE-PROD/.env`. Never commit credentials. The service
account needs workbook/site access and delegated Graph file-write permission.
`GRAPH_MASTER_SHEET_SYNC=true` enables sync by default; `false` disables appends.

Use one app process and avoid concurrent manual or other-instance appends.
The blank-cell check before writing is not a transaction against other coauthors.
There is no persistent retry queue or automatic retry of an uncertain write.
Check the sheet before retrying SharePoint sync; do not recreate a requester
solely to retry the spreadsheet step.

## Validation

From the repository root:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s LIVE-PROD/tests -v
```

Read-only access check from `LIVE-PROD` on the server:

```bash
sudo docker compose exec -T api python -c "from qa_irs_pin.sharepoint_sync import check_access; print(check_access())"
```

An `Accessible` result verifies the workbook and required columns. Test writes
can use an Excel session with `persistChanges: false`; do not save dummy test
locations in the production workbook. Connect behavior is tested using mocks.
