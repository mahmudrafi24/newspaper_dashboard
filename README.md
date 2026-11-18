# Market Ledger editorial dashboard

The dashboard is a Go HTTP service on Oracle. It reads the SQLite queue populated
by the [news bot](https://github.com/mahmudrafi24/news_bot). The editor at
`/admin` lets an admin preview, edit, reject, approve, and unpublish stories.
Cloudflare serves the public static site separately.

## Architecture

- **Go backend.** Uses the standard HTTP server and escaped HTML templates. The
  existing dashboard JavaScript provides the in-place preview and checks the
  queue every 30 seconds. Each status view shows 25 stories per page.
- **Shared SQLite queue.** Uses the collector's existing `markets.sqlite`
  database and story states. The service creates an index for ordered queue
  reads and uses WAL, a bounded connection pool, and a busy timeout so the
  collector and dashboard can access the same local database.
- **Approval and upload.** New stories stay in **Pending review**. After approval,
  the service creates a temporary export containing only approved and already
  published stories. Story edits and uploads share a file lock. The service
  records **Published** after Wrangler succeeds. A failed upload leaves approval
  saved for retry. No private editor files are included in the upload.
- **Scaling.** Cloudflare serves public traffic independently of the admin
  server. Keep the collector and Go dashboard on a single Oracle VM with the
  SQLite database on local disk. For multiple admin replicas, migrate both
  services to a shared database such as PostgreSQL, move deployment locking and
  retry state into that database, and use shared session or CSRF state.

## Build and run on Oracle

Use Linux, Go 1.24 or later, and GCC. The SQLite driver uses CGO and bundles
SQLite. Build on the target architecture, including Oracle ARM64 where applicable.

Clone the repositories side by side as `~/news_bot` and
`~/newspaper_dashboard`. Keep the existing `markets.sqlite` file. If the
collector uses a custom `TECH_NEWS_DATA_DIR`, set the same absolute directory
in `~/.config/tech-news/shared.env` for both services.

```sh
cd ~/newspaper_dashboard
mkdir -p bin
CGO_ENABLED=1 go test ./...
CGO_ENABLED=1 go build -trimpath -o bin/market-ledger .
./bin/market-ledger serve
```

The server binds to `127.0.0.1:8765`. Set a different port with
`serve --port PORT`. The commands use the dashboard checkout as their working
directory so they can find its assets and the adjacent `news_bot/site` directory.
To run under systemd, stop the foreground process and install the new service unit.

```sh
mkdir -p ~/.config/systemd/user
cp systemd/tech-news-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable tech-news-dashboard.service
systemctl --user restart tech-news-dashboard.service
curl -fsS http://127.0.0.1:8765/healthz
```

To open the editor privately, forward the same port over SSH from your
computer with `ssh -N -L 8765:127.0.0.1:8765 opc@ORACLE_HOST`, then visit
`http://127.0.0.1:8765/admin`. For a permanent link, use a dedicated Cloudflare
Access-protected hostname. Set the tunnel origin to `http://127.0.0.1:8765` and
the HTTP Host Header to `127.0.0.1:8765`. Keep the Access policy limited to
admins and enable Access token validation on the tunnel. The loopback port
also remains accessible to authorized local or SSH users.

## Cloudflare publishing

Install and authorize Wrangler on Oracle. Configure
`~/.config/tech-news/pages.env` for the dashboard service, with file permissions
`600`. Set `WRANGLER_BIN` to its absolute binary path and choose one target.

| Deployment | Environment setting |
| --- | --- |
| Existing Cloudflare Worker | `CLOUDFLARE_WORKER_NAME=notasmuchhyped` |
| Cloudflare Pages Direct Upload | `CLOUDFLARE_PAGES_PROJECT=<project>` |

For token authentication, also set `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN` in that private file. Restart the service after updating
it. Use **Approve & publish** in the dashboard. The **Awaiting upload** queue
shows saved approvals and pending removals. Use **Retry site update** if needed.

From this checkout, `./bin/market-ledger export` writes the currently published
site to `public/`. With the deployment environment loaded,
`./bin/market-ledger deploy` explicitly uploads a snapshot. The public frontend
lives in `news_bot/site`. Use `npx wrangler deploy` for its Workers starter
build, or `sh build-pages.sh` with output directory `public` for Pages.
When Oracle handles production publishing, disable automatic production
builds of the static starter so a source push cannot replace approved stories.

## Migration and troubleshooting

Build the Go binary before switching the service. The previous Python service
unit can be restored from the parent revision if a rollback is needed. The
Go service uses the same story records and does not rewrite their state on
startup.

- If the editor fails to start, run
  `journalctl --user -u tech-news-dashboard.service -n 50 --no-pager`.
- If `/healthz` fails, confirm the service user can read and write the SQLite
  directory and that both processes use the same database path.
- If upload fails, check the dashboard service journal for Wrangler's output.
  Fix the deployment configuration and use **Retry site update**.
- To review the migration in GitHub, see the commit titled
  **Migrate the editorial dashboard backend to Go**. It includes the Go
  service, its templates, integration test, systemd unit, and this README.
