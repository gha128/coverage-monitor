# Media monitor

Checks public news and social feeds every 30 minutes for mentions of the hospice, sends a phone alert through ntfy for anything new, and keeps a page of everything found in `docs/index.html`.

Set-up on GitHub:

1. Repository settings, Secrets and variables, Actions: add `NTFY_TOPIC` (the ntfy channel name). Optionally `EXTRA_FEEDS`: Google Alerts RSS links, separated by spaces.
2. Settings, Pages: deploy from the `main` branch, `/docs` folder.
3. Actions tab: enable workflows, then run 'Media monitor' once by hand. The first run records what is already out there without sending alerts.

To stop it, disable the workflow in the Actions tab.
