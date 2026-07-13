# Kimbal Slack usage dashboard

A dependency-free static dashboard built from the Slack analytics exports dated **8 July 2026**. It contains only aggregated metrics and channel-level data; member names, email addresses, and raw exports are intentionally excluded.

## Local preview

Open `index.html` directly in a browser, or run:

```powershell
npx serve .
```

## Deploy to Azure Static Web Apps

1. Create a GitHub repository and push this folder to its `main` branch.
2. Create an Azure Static Web App linked to the repository (or use an existing one).
3. Add its deployment token to the GitHub repository secret named `AZURE_STATIC_WEB_APPS_API_TOKEN`.
4. Push to `main`. The included GitHub Actions workflow deploys the repository root with no build step.

## Metric notes

- **MAU / adoption:** members with at least one active day in the export's prior-30-day window, divided by all 712 listed accounts.
- **DAU (average):** total active member-days ÷ 30. It is an average, not a daily time series.
- **Stickiness:** average DAU ÷ MAU.
- The available exports are a point-in-time aggregate. They cannot support daily trend, WAU, retention, or cohort calculations.
