# Kimbal Slack Adoption Dashboard

Drop Slack admin exports into `slack-exports/`, run the build script, push — the
dashboard at Azure Static Web Apps updates automatically.

## Weekly refresh (the whole job)

1. Slack Admin → Analytics → export **Member Analytics** CSV
   (plus **Channel Analytics** CSV and the org **Channel directory** XLSX if you want
   the channel sections refreshed).
2. Drop the files into `slack-exports/` — do NOT delete old member exports,
   each one becomes a point on the trend chart.
3. Run:
       python build_slack_dashboard.py --push
   (or `--watch --push` to leave it running and auto-deploy whenever files land)
4. GitHub Action deploys `index.html` to Azure SWA in ~30 seconds.

## Recognised files

| Pattern | Used for |
|---|---|
| `*Member_Analytics*Jul_8__2026*.csv` | KPIs, funnel, platforms, one trend point per export date |
| `*Channel_Analytics*.csv` (workspace) | Public channel table |
| `*Private_Limited_Channel_Analytics*.xlsx` | Public/private split, channel creation wave |

Dates are parsed from the filename (`Jul_8__2026`), so keep Slack's default names.

## Config

Edit the constants at the top of `build_slack_dashboard.py`:
`LAUNCH_DATE`, `CUTOVER_DATE`, `BENCH` (benchmark targets).

## First-time Azure setup

1. Push this repo to GitHub (private).
2. Azure Portal → Create → Static Web App → Free plan → connect the repo
   (or Standard plan to enforce the Entra ID login in `staticwebapp.config.json`).
3. If you created the SWA without the GitHub wizard, add the deployment token
   as repo secret `AZURE_STATIC_WEB_APPS_API_TOKEN`.

Note: `staticwebapp.config.json` requires the **Standard** plan for Entra ID
auth enforcement. On the Free plan, remove the file or the routes block.
