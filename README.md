# Tech Brief dashboard and public site

Editorial dashboard for the [Tech Brief bot](https://github.com/mahmudrafi24/news_bot). Review the original Google News link, edit the headline and summary, and publish or unpublish. The dashboard checks for new research every 30 seconds and prompts you to refresh so edits in progress are preserved.

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

## Cloudflare Pages

Create a Pages Direct Upload project with production branch `main`. Install Wrangler on Oracle and authorize its use for the Pages project. Put `CLOUDFLARE_PAGES_PROJECT` and the absolute `WRANGLER_BIN` path in `~/.config/tech-news/pages.env`. For token authentication, also set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` there. Restrict the file to the service user and restart the dashboard service after configuration changes.

Run `python3 dashboard.py deploy` with those variables exported to make the initial deployment. The dashboard deploys an updated copy whenever you publish, edit a published story, or move a published story back to drafts. Retry a failed upload using **Retry site update**. Only the published static site in `public/` is uploaded. The dashboard and SQLite files stay on Oracle.

To render a copy locally, run `python3 dashboard.py export`. Approved stories link out to the corresponding Google News story.

### If Cloudflare shows `npx wrangler deploy`

That command belongs to Workers Builds. To use Cloudflare Pages, select **Workers & Pages → Create application → Pages**. For the live newsroom, use **Direct Upload** and deploy from Oracle as described above.

To bootstrap a Pages project from GitHub instead, select **Pages → Connect to Git** and choose this repository. Set production branch to `main`, framework preset to **None**, build command to `sh build-pages.sh`, and build output directory to `public`. This build produces the public empty-state page and requires neither the bot repository nor a database. After the first build, turn off automatic production deployments before connecting Oracle so subsequent Git builds cannot replace the published stories. Oracle can still deploy to that Pages project with Wrangler.
