import argparse
from contextlib import closing, contextmanager
import fcntl
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
BOT_ROOT = ROOT.parent / "news_bot"
SITE = BOT_ROOT / "site"
sys.path.insert(0, str(BOT_ROOT))
from story_store import DATA, database, now

PUBLIC = ROOT / "public"
CSRF = secrets.token_urlsafe(32)


def e(value):
    return html.escape(str(value), quote=True)


def page(title, body, editor=False):
    nav = '<a href="/admin">Dashboard</a>' if editor else ""
    script = '<script src="/dashboard.js" defer></script>' if editor else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · Market Ledger</title><link rel="stylesheet" href="/style.css">{script}</head>
<body><header><div class="wrap mast"><a class="logo" href="/">Market<span> Ledger</span></a>{nav}</div></header>
<main class="wrap">{body}</main><footer class="wrap">Market Ledger · Trading and market news</footer></body></html>"""


def public_page(db, include_approved=False):
    visible = ("published", "approved" if include_approved else "withdrawing")
    stories = db.execute("""
        SELECT * FROM stories WHERE state IN (?, ?)
        ORDER BY julianday(reported) DESC, id
    """, visible).fetchall()
    template = (SITE / "index.html").read_text()
    if not stories:
        return template
    lead, *remaining = stories
    desks = {}
    for story in remaining:
        desks.setdefault(story["desk"], []).append(story)
    links = "".join(f'<a href="#{e(desk.lower())}">{e(desk)}</a>' for desk in desks)
    sections = ['<h2 class="section-label">Latest market news</h2>', story_card(lead, lead=True)]
    if links:
        sections.append(f'<nav class="desks" aria-label="Browse market desks">{links}</nav>')
    for desk, desk_stories in desks.items():
        cards = "".join(story_card(story) for story in desk_stories)
        sections.append(f'<section id="{e(desk.lower())}"><h2>{e(desk)}</h2><div class="grid">{cards}</div></section>')
    before, _, rest = template.partition("<!-- stories -->")
    _, _, after = rest.partition("<!-- /stories -->")
    return before + "\n".join(sections) + after


def story_card(story, lead=False):
    style = ' class="story-lead"' if lead else ""
    label = f"{e(story['desk'])} · " if lead else ""
    return f"""<article{style}><p class="meta">{label}{e(story['source'])} · {e(story['reported'][:10])}</p>
<h3><a href="{e(story['url'])}">{e(story['headline'])}</a></h3>
<p>{e(story['summary'])}</p><a class="source" href="{e(story['url'])}">Read on Google News ↗</a></article>"""


def revision(db):
    return str(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM runs").fetchone()[0])


def dashboard(db, deploy_failed=False):
    current = revision(db)
    run = db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    status = f"{run['at'][:16].replace('T', ' ')} UTC · {run['message']}" if run else "Waiting for the first research run"
    retry = ""
    pending_upload = db.execute(
        "SELECT 1 FROM stories WHERE state IN ('approved', 'withdrawing') LIMIT 1"
    ).fetchone()
    if deploy_failed or pending_upload:
        retry = f"""<p role="alert">A public site update is pending. Retry if the last upload failed.</p>
<form method="post" action="/admin"><input type="hidden" name="csrf" value="{CSRF}">
<button name="action" value="retry">Retry site update</button></form>"""
    cards = []
    labels = {
        "draft": "Pending review", "approved": "Approved · Awaiting upload",
        "published": "Published", "withdrawing": "Removal pending", "rejected": "Rejected",
    }
    for story in db.execute("SELECT * FROM stories ORDER BY state != 'draft', reported DESC"):
        state = story["state"]
        buttons = {
            "draft": '<button name="action" value="save">Save draft</button>'
                     '<button class="primary" name="action" value="publish">Approve &amp; publish</button>'
                     '<button name="action" value="reject">Reject</button>',
            "approved": '<button name="action" value="draft">Return to review</button>' if story["published"] is None else "",
            "published": '<button class="primary" name="action" value="publish">Approve &amp; publish edits</button>'
                         '<button name="action" value="draft">Unpublish</button>',
            "withdrawing": '<button class="primary" name="action" value="publish">Approve &amp; republish</button>',
            "rejected": '<button name="action" value="draft">Return to review</button>',
        }[state]
        name = e(story["headline"])
        summary = e(story["summary"])
        cards.append(f"""<article><p class="meta">{e(labels[state])} · {e(story['desk'])} · {e(story['source'])} · {e(story['reported'][:10])}</p>
<p><a href="{e(story['url'])}">{e(story['original'])} ↗</a></p>
<form method="post" action="/admin">
<input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="id" value="{e(story['id'])}">
<label>Headline<input name="headline" value="{name}" required maxlength="115"></label>
<label>Summary<textarea name="summary" required maxlength="350">{summary}</textarea></label>
{buttons}</form></article>""")
    content = "".join(cards) or '<p class="empty">New drafts will appear here after an hourly run succeeds.</p>'
    return page("Editorial dashboard", f'<h1>Editorial dashboard</h1><p class="status" data-revision="{current}">{e(status)}</p><p id="refresh-notice" role="status" hidden><a href="/admin">Research finished. Refresh the queue.</a></p>{retry}{content}', True)


def export(db, directory, include_approved=False):
    directory.mkdir(exist_ok=True)
    assets = {
        "index.html": public_page(db, include_approved=include_approved),
        "style.css": (SITE / "style.css").read_text(),
        "share.js": (SITE / "share.js").read_text(),
        "market-brief-2026-11-18.html": (SITE / "market-brief-2026-11-18.html").read_text(),
        "october-market-review.html": (SITE / "october-market-review.html").read_text(),
    }
    for name, content in assets.items():
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
            output.write(content)
        os.chmod(output.name, 0o644)
        os.replace(output.name, directory / name)


class Handler(BaseHTTPRequestHandler):
    def respond(self, content, content_type="text/html; charset=utf-8"):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        return self.headers.get("Host") in {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}

    def do_GET(self):
        if not self.allowed_host():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path == "/style.css":
            self.respond((SITE / "style.css").read_text(), "text/css; charset=utf-8")
        elif path == "/share.js":
            self.respond((SITE / "share.js").read_text(), "text/javascript; charset=utf-8")
        elif path.rstrip("/").removesuffix(".html") in ("/october-market-review", "/market-brief-2026-11-18"):
            edition = path.strip("/").removesuffix(".html")
            self.respond((SITE / (edition + ".html")).read_text())
        elif path == "/dashboard.js":
            self.respond((ROOT / "dashboard.js").read_text(), "text/javascript; charset=utf-8")
        elif path == "/admin/revision":
            with closing(database()) as db:
                self.respond(revision(db), "text/plain; charset=utf-8")
        elif path in ("/", "/admin"):
            with closing(database()) as db, db:
                self.respond(dashboard(db, urlsplit(self.path).query == "deploy=failed") if path == "/admin" else public_page(db))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/admin" or not self.allowed_host():
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError
            form = {key: values[0] for key, values in parse_qs(
                self.rfile.read(size).decode(), max_num_fields=5
            ).items()}
            if not secrets.compare_digest(form.get("csrf", ""), CSRF):
                self.send_error(403)
                return
            action = form["action"]
            if action == "retry":
                self.update_public(deploy=True)
                return
            headline, summary = form["headline"].strip(), form["summary"].strip()
            if not (1 <= len(headline) <= 115 and 1 <= len(summary) <= 350 and action in ("save", "publish", "draft", "reject")):
                raise ValueError
            key = form["id"]
        except (ValueError, KeyError):
            self.send_error(400)
            return

        try:
            needs_upload = edit_story(key, action, headline, summary)
        except LookupError:
            self.send_error(404)
            return
        except ValueError:
            self.send_error(409)
            return
        self.update_public(deploy=needs_upload)

    def update_public(self, deploy):
        location = "/admin"
        if deploy:
            try:
                refresh_public(deploy=True)
            except (OSError, subprocess.SubprocessError) as error:
                self.log_error("Public site update failed: %s", error)
                location = "/admin?deploy=failed"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


@contextmanager
def publishing_lock():
    DATA.mkdir(mode=0o700, exist_ok=True)
    with (DATA / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def edit_story(key, action, headline, summary):
    with publishing_lock(), closing(database()) as db, db:
        story = db.execute("SELECT * FROM stories WHERE id=?", (key,)).fetchone()
        if story is None:
            raise LookupError(key)
        state = story["state"]
        transitions = {
            ("draft", "save"): "draft",
            ("draft", "reject"): "rejected",
            ("draft", "publish"): "approved",
            ("approved", "draft"): "draft",
            ("rejected", "draft"): "draft",
            ("published", "publish"): "approved",
            ("published", "draft"): "withdrawing",
            ("withdrawing", "publish"): "approved",
        }
        next_state = transitions.get((state, action))
        if next_state is None or (state == "approved" and action == "draft" and story["published"] is not None):
            raise ValueError("Story state changed. Refresh the dashboard.")
        if action not in ("save", "publish"):
            headline, summary = story["headline"], story["summary"]
        published = story["published"] if next_state in ("approved", "withdrawing") else None
        db.execute("UPDATE stories SET headline=?, summary=?, state=?, published=? WHERE id=?",
                   (headline, summary, next_state, published, key))
        return next_state in ("approved", "withdrawing")


def refresh_public(deploy=False):
    with publishing_lock(), closing(database()) as db, db:
        if deploy:
            with tempfile.TemporaryDirectory(prefix="market-ledger-upload-") as staging:
                directory = Path(staging)
                export(db, directory, include_approved=True)
                subprocess.run(deploy_command(directory), cwd=ROOT, check=True, timeout=180)
            db.execute("UPDATE stories SET state='published', published=COALESCE(published, ?) WHERE state='approved'",
                       (now().isoformat(),))
            db.execute("UPDATE stories SET state='draft', published=NULL WHERE state='withdrawing'")
        else:
            export(db, PUBLIC)


def deploy_command(directory):
    project = os.environ.get("CLOUDFLARE_PAGES_PROJECT")
    worker = os.environ.get("CLOUDFLARE_WORKER_NAME")
    wrangler = os.environ.get("WRANGLER_BIN", "wrangler")
    if worker and not project:
        return [wrangler, "deploy", "--name", worker, "--assets", str(directory)]
    if project and not worker:
        return [wrangler, "pages", "deploy", str(directory), "--project-name=" + project, "--branch=main"]
    raise OSError("Configure one Cloudflare target in pages.env")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "export", "deploy"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "serve":
        with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            print(f"Dashboard at http://127.0.0.1:{server.server_port}/admin", flush=True)
            server.serve_forever()
    else:
        deploy = args.command == "deploy"
        try:
            refresh_public(deploy=deploy)
        except (OSError, subprocess.SubprocessError) as error:
            if deploy:
                with closing(database()) as db, db:
                    db.execute("INSERT INTO runs VALUES (?, ?)", (
                        now().isoformat(), f"Cloudflare upload failed: {error}"[:350]
                    ))
            parser.exit(1, f"{error}\n")
        if deploy:
            with closing(database()) as db, db:
                db.execute("INSERT INTO runs VALUES (?, ?)", (now().isoformat(), "Cloudflare upload complete"))


if __name__ == "__main__":
    main()
