# Media monitor

Checks public news and social feeds every 30 minutes at three levels: the hospice itself, named neighbouring hospices, and UK hospice sector news (including hospice stories in Civil Society and Third Sector). New mentions of the hospice send a phone alert through ntfy; everything goes on a page with a filter for each level, in `docs/index.html`.

Set-up on GitHub:

1. Repository settings, Secrets and variables, Actions: add `NTFY_TOPIC` (the ntfy channel name). `LOCAL_HOSPICES` (names separated by |; add @place,place after a name that other hospices share) and `SECTOR_TERMS` (search words separated by |) turn on the other two levels. Optionally `EXTRA_FEEDS`: Google Alerts RSS links, separated by spaces.
2. Settings, Pages: deploy from the `main` branch, `/docs` folder.
3. Actions tab: enable workflows, then run 'Media monitor' once by hand. The first run records what is already out there without sending alerts.

To stop it, disable the workflow in the Actions tab.
