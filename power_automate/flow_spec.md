# Power Automate — monthly Board Pack pipeline (design spec)

**Status: design specification, not an importable flow.** A Power Automate export (`.zip`/solution) can't be authored
or tested outside the service, and an untested export is worse than none. Every dynamic-content expression below is
standard Workflow Definition Language; connector action names are as of writing and Microsoft/Qlik rename things —
confirm in the designer. The data feed it reads (`fin.v_board_kpis`) **was executed and tested**.

## Licensing / connectivity constraints you will hit (verify before committing to this design)

| Need | Constraint |
|---|---|
| Call Qlik / NPrinting REST APIs | The generic **HTTP** action is a **Premium** connector. |
| Qlik is on-prem (client-managed) | The on-premises data gateway serves specific connectors (and custom connectors); do not assume plain HTTP reaches an on-prem Qlik server through it. Typical solutions: a **custom connector** over the gateway, or an Azure-hosted relay/API in front of QRS/NPrinting. |
| Read KPIs from PostgreSQL | **PostgreSQL** connector (Premium; supports the gateway for on-prem databases). |
| Qlik Cloud | Direct HTTPS to `https://<tenant>.<region>.qlikcloud.com/api/v1/...` with an API key / OAuth client; no gateway needed. |
| PDF generation | Qlik NPrinting (Windows server product) **or** Qlik Cloud's reporting service. Pick one; they are different APIs. |

## Flow: `BoardPack-Monthly`

### Trigger — Recurrence
- Frequency **Month**, Interval **1**, Start time `2026-12-01T06:00:00Z`, Time zone **UTC**.

> **Business-risk warning (not a technical one).** 06:00 UTC on the **1st** is when the *previous month is least
> likely to be closed*. Real general ledgers close on working-day 3–10. A board pack sent on day 1 reports unclosed
> books and a stale risk snapshot. Either (a) move the schedule to the finance close-complete date, or (b) keep day 1
> **but gate on the checks below** and let the flow stop itself. Option (b) is implemented here.

### Step 0 — Gate (added; not in the original brief, but without it the flow can mail wrong numbers)
Action: PostgreSQL **Get rows** on `fin.v_board_kpis` (1 row). Name it `Get_KPIs`.

Condition `Gate_OK` (expression mode):
```
@and(
  equals(first(body('Get_KPIs')?['value'])?['dq_failures'], 0),
  lessOrEquals(first(body('Get_KPIs')?['value'])?['el_months_stale'], 1)
)
```
- `dq_failures` = count of failing checks in `fin.v_dq_audit` (unbalanced journals, orphan keys, balance sheet not balancing…).
- `el_months_stale` = months between the reporting period and the latest expected-loss snapshot. In the shipped sample
  data this is **33**, so the gate *correctly refuses to send* until the scorecard is re-run.
- **No branch:** jump to the failure handler (below) with message "Gate failed: dq_failures=…, el_months_stale=…".

### Scope `Try` (Yes branch of the gate)

**Action 1 — Data refresh.**
Qlik Cloud: `POST /api/v1/reloads` body `{"appId":"<APP_ID>"}`, header `Authorization: Bearer <API_KEY>` → store `id`.
Client-managed: QRS `POST /qrs/app/<APP_ID>/reload` with `Xrfkey` header+query and certificate/header auth, or
`POST /qrs/task/<TASK_ID>/start/synchronous`.
Poll with **Do until** (`@equals(variables('reloadStatus'),'SUCCEEDED')` or `FAILED`), inside it: *Delay* 1 minute →
`GET /reloads/{id}` → *Set variable* `reloadStatus` = `@body('Poll')?['status']`. Limits: count 60, timeout `PT1H`.
Add a Condition after the loop: if status ≠ `SUCCEEDED` → `Terminate` with status **Failed** (this triggers the Catch scope).

**Action 2 — Report generation.**
NPrinting: `POST /api/v1/tasks/<TASK_ID>/executions` → poll `GET /api/v1/tasks/executions/<id>` until completed
(confirm the path in your NPrinting version's API reference), or Qlik Cloud reporting. Output: multi-page PDF
(Sheet 1 → 3 on separate pages).

**Action 3 — Store & validate.**
SharePoint **Create file**: Folder `/Shared Documents/BoardPacks/@{formatDateTime(utcNow(),'yyyy')}`,
File name `BoardPack_@{formatDateTime(addToTime(utcNow(),-1,'Month'),'yyyy-MM')}.pdf`, content = PDF body.
(Google Drive equivalent: *Upload file*.)
Size check — Condition:
```
@and(
  greater(outputs('Create_file')?['body/Size'], 200000),
  less(outputs('Create_file')?['body/Size'], 20000000)
)
```
A near-empty PDF (< 200 KB for a multi-page pack) almost always means a sheet rendered with no data. The upper bound
keeps you under typical 25 MB mailbox/connector attachment limits (tenant-configurable; check yours).

**Action 4 — Notification.** Outlook **Send an email (V2)** (or Gmail *Send email*): To = board distribution list,
Attachments = the PDF, plus link `outputs('Create_file')?['body/{Link}']`. *Is HTML* = Yes. Body:

```html
<h3>Board Pack — @{formatDateTime(first(body('Get_KPIs')?['value'])?['reporting_period'],'MMMM yyyy')}</h3>
<table cellpadding="6" style="border-collapse:collapse;font-family:Segoe UI,Arial">
 <tr><td>Net income (pre-tax)</td>
     <td align="right"><b>@{formatNumber(first(body('Get_KPIs')?['value'])?['net_income'],'N0','en-US')}</b></td></tr>
 <tr><td>Expected loss (model, as of @{formatDateTime(first(body('Get_KPIs')?['value'])?['el_as_of_date'],'dd MMM yyyy')})</td>
     <td align="right"><b>@{formatNumber(first(body('Get_KPIs')?['value'])?['expected_loss'],'N0','en-US')}</b></td></tr>
 <tr><td>Expected loss, % of exposure</td>
     <td align="right"><b>@{formatNumber(first(body('Get_KPIs')?['value'])?['el_pct_of_ead'],'N2','en-US')}%</b></td></tr>
 <tr><td>Budget variance (favourable +)</td>
     <td align="right"><b>@{formatNumber(first(body('Get_KPIs')?['value'])?['budget_variance_pct'],'N2','en-US')}%</b></td></tr>
</table>
<p><a href="@{outputs('Create_file')?['body/{Link}']}">Open the full pack</a> (PDF also attached).</p>
<p style="color:#666;font-size:12px">Automated message. Data-quality checks passed: 0 failures.</p>
```

### Scope `Catch` — Run after: **has failed**, **has timed out** (configure under *Settings → Run after* on the Catch scope)
1. *Filter array* `From` = `@result('Try')`, condition `@equals(item()?['status'],'Failed')` → name `FailedActions`.
2. Teams **Post message in a chat or channel** (or Slack *Post message*) to the SysAdmin channel:
```
Board Pack flow FAILED
Failed step: @{first(body('FailedActions'))?['name']}
Error: @{first(body('FailedActions'))?['error']?['message']}
Run: https://make.powerautomate.com/environments/@{workflow()?['tags']?['environmentName']}/flows/@{workflow()?['name']}/runs/@{workflow()?['run']?['name']}
```
3. Same message path for the **gate-failed** branch (so a refusal to send is visible, not silent).
4. Do **not** email the board from the Catch scope.

## Known weaknesses of this design

- The PDF and the KPI email read from two paths (Qlik app vs. PostgreSQL view). If the Qlik reload partially fails
  and `Terminate` is missed, the email KPIs can disagree with the PDF. The KPI view and the Qlik expressions are
  specified to tie out (see `dashboard_spec.md`), but nothing enforces it at run time.
- Polling loops burn flow actions (each iteration counts toward your plan's action limits).
- API keys belong in **environment variables / Key Vault**, not literal values in actions.
- Run history is the only audit trail; export it if compliance requires evidence of what was sent to the board.
