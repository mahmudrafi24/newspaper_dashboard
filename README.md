# Market Ledger dashboard and public site

Editorial dashboard for the [Market Ledger bot](https://github.com/mahmudrafi24/news_bot). Review the original Google News link, edit the headline and summary, and approve, reject, or unpublish stories. The dashboard checks for new research every 30 seconds and prompts you to refresh so edits in progress are preserved.

## Oracle setup

Clone the two repositories side by side as `~/news_bot` and `~/newspaper_dashboard`, then follow the bot's setup. Both processes run under the same Oracle user and use the same SQLite queue. This dashboard requires Python 3.9 or later.

```sh
cd ~/newspaper_dashboard
mkdir -p ~/.config/systemd/user
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tech-news-dashboard.service
```

From your computer, run `ssh -N -L 8765:127.0.0.1:8765 opc@ORACLE_HOST` and open `http://127.0.0.1:8765/admin`. Keep the dashboard on loopback. Access is limited to users who can access the Oracle account or reach its loopback interface.

## Public frontend repository

The public frontend is maintained in [news_bot](https://github.com/mahmudrafi24/news_bot).
Connect that repository to Cloudflare for new frontend builds. Oracle exports
from its sibling `news_bot/site` directory. This repository retains its static
snapshot for existing Git-connected Cloudflare builds during the transition.

## Cloudflare Pages

Create a Pages Direct Upload project with production branch `main`. Install Wrangler on Oracle and authorize its use for the Pages project. Put `CLOUDFLARE_PAGES_PROJECT` and the absolute `WRANGLER_BIN` path in `~/.config/tech-news/pages.env`. For token authentication, also set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` there. Restrict the file to the service user and restart the dashboard service after configuration changes.

Run `python3 dashboard.py deploy` with those variables exported to make the initial deployment. Hourly drafts stay private until you choose **Approve & publish**. Published edits also require approval. The dashboard uploads a static snapshot and records a story as published after Wrangler succeeds. A failed upload preserves the approval and can be retried with **Retry site update**. The dashboard and SQLite files stay on Oracle.

To render a copy locally, run `python3 dashboard.py export`. Approved stories link out to the corresponding Google News story.

### If Cloudflare shows `npx wrangler deploy`

That command belongs to Workers Builds. To use Cloudflare Pages, select **Workers & Pages → Create application → Pages**. For the live newsroom, use **Direct Upload** and deploy from Oracle as described above.

To bootstrap a Pages project from GitHub instead, select **Pages → Connect to Git** and choose `news_bot`. Set production branch to `main`, framework preset to **None**, build command to `sh build-pages.sh`, and build output directory to `public`. This build produces the public empty-state page and requires neither the bot repository nor a database. After the first build, turn off automatic production deployments before connecting Oracle so subsequent Git builds cannot replace the published stories. Oracle can still deploy to that Pages project with Wrangler.

### Use the existing Cloudflare Worker

The included Wrangler config builds the empty-state starter for the existing
`newspaper-dashboard` Worker. Once Oracle controls publishing, disable automatic
production deployments from GitHub in Workers Builds so a source push cannot
replace the live edition with the starter. Keep `/admin` behind Cloudflare Access.

For the current public Worker, set `CLOUDFLARE_WORKER_NAME=notasmuchhyped` in
`~/.config/tech-news/pages.env` and leave `CLOUDFLARE_PAGES_PROJECT` unset. Set
`WRANGLER_BIN` to the absolute path to Wrangler. Use Wrangler authentication
or set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in that same private
file with permission to deploy the Worker. Restart the dashboard service after
changing the file. Publishing uploads the static export to the Worker.

The Market Ledger edition uses a new `markets.sqlite` queue. The earlier tech
edition's `news.sqlite` stays on disk. Use your Access-protected tunnel
hostname for `/admin` and keep it separate from the public site.
