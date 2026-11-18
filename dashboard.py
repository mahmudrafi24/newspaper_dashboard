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
    nav = '<a href="https://notasmuchhyped.mahmudrafi.workers.dev/">View public site ↗</a>' if editor else ""
    assets = '<link rel="stylesheet" href="/editor.css"><script src="/dashboard.js" defer></script>' if editor else ""
    body_class = ' class="editor"' if editor else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · Market Ledger</title><link rel="stylesheet" href="/style.css">{assets}</head>
<body{body_class}><header><div class="wrap mast"><a class="logo" href="/">Market<span> Ledger</span></a>{nav}</div></header>
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


EDITOR_VIEWS = {
    "review": ("Pending review", ("draft",)),
    "upload": ("Awaiting upload", ("approved", "withdrawing")),
    "published": ("Published", ("published",)),
    "rejected": ("Rejected", ("rejected",)),
}
STORY_LABELS = {
    "draft": "Pending review",
    "approved": "Approved · Awaiting upload",
    "withdrawing": "Removal pending",
    "published": "Published",
    "rejected": "Rejected",
}
STORY_ACTIONS = {
    "draft": (("publish", "Approve & publish"), ("save", "Save draft"), ("reject", "Reject")),
    "approved": (("draft", "Return to review"),),
    "published": (("publish", "Approve & publish edits"), ("draft", "Unpublish")),
    "withdrawing": (("publish", "Approve & republish"),),
    "rejected": (("draft", "Return to review"),),
}
EMPTY_VIEW = {
    "review": "New stories will appear here after an hourly research run.",
    "upload": "There are no public site updates awaiting upload.",
    "published": "There are no published stories in the editorial queue.",
    "rejected": "No stories have been rejected.",
}


def editor_card(story):
    state = story["state"]
    headline, summary = e(story["headline"]), e(story["summary"])
    editable = state in ("draft", "published", "withdrawing")
    actions = () if state == "approved" and story["published"] is not None else STORY_ACTIONS[state]
    buttons = []
    for action, label in actions:
        style = ' class="primary"' if action == "publish" else ""
        buttons.append(f'<button type="submit" name="action" value="{action}"{style}>{e(label)}</button>')
    copy_form = f"""<label>Headline<input name="headline" value="{headline}" required maxlength="115"></label>
<label>Summary<textarea name="summary" required maxlength="350">{summary}</textarea></label>""" if editable else ""
    form = f"""<form method="post" action="/admin">
<input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="id" value="{e(story['id'])}">
{copy_form}<div class="editor-actions">{"".join(buttons)}</div></form>""" if actions else ""
    return f"""<article class="editor-card" data-editor-story>
<div class="editor-card-top"><span class="editor-badge" data-state="{state}">{e(STORY_LABELS[state])}</span>
<span>{e(story['desk'])} · <time datetime="{e(story['reported'])}">{e(story['reported'][:10])}</time></span></div>
<div class="editor-story">
<div class="editor-preview">
<p class="editor-kicker">Public preview</p>
<h3 data-preview-headline>{headline}</h3>
<p data-preview-summary>{summary}</p>
<p class="editor-source"><strong>{e(story['source'])}</strong><br>
<a href="{e(story['url'])}" target="_blank" rel="noopener noreferrer">{e(story['original'])} ↗</a></p>
</div>
{form}</div></article>"""


def dashboard(db, deploy_failed=False, view="review"):
    if view not in EDITOR_VIEWS:
        view = "review"
    current = revision(db)
    run = db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    status = f"{run['at'][:16].replace('T', ' ')} UTC · {run['message']}" if run else "Waiting for the first research run"
    counts = dict(db.execute("SELECT state, COUNT(*) FROM stories GROUP BY state"))
    tabs = []
    for key, (label, states) in EDITOR_VIEWS.items():
        active = ' aria-current="page"' if key == view else ""
        count = sum(counts.get(state, 0) for state in states)
        tabs.append(f'<a href="/admin?view={key}"{active}>{label} <span>{count}</span></a>')

    retry = ""
    if deploy_failed or counts.get("approved") or counts.get("withdrawing"):
        message = "Upload failed. The approved update is saved." if deploy_failed else "An approved update is awaiting upload."
        retry = f"""<div class="editor-upload" role="status"><p>{message}</p>
<form method="post" action="/admin"><input type="hidden" name="csrf" value="{CSRF}">
<button type="submit" name="action" value="retry">Retry site update</button></form></div>"""

    label, states = EDITOR_VIEWS[view]
    placeholders = ", ".join("?" for _ in states)
    entries = db.execute(f"SELECT * FROM stories WHERE state IN ({placeholders}) ORDER BY julianday(reported) DESC, id", states)
    content = "".join(editor_card(story) for story in entries)
    if not content:
        content = f'<p class="empty">{EMPTY_VIEW[view]}</p>'
    return page("Editorial dashboard", f"""<div class="editor-heading">
<div><h1>Editorial dashboard</h1><p>Review and approve stories before they appear on Market Ledger.</p></div>
<div class="editor-run"><strong>Latest activity</strong><p data-revision="{current}">{e(status)}</p></div>
</div>
<p id="refresh-notice" class="editor-notice" role="status" hidden><a href="/admin?view={view}">New research is ready. Refresh the queue.</a></p>
{retry}
<nav class="editor-tabs" aria-label="Story status">{"".join(tabs)}</nav>
<section aria-labelledby="queue-heading"><h2 id="queue-heading" class="editor-queue-heading">{label}</h2>
{content}</section>""", True)


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
        elif path == "/editor.css":
            self.respond((ROOT / "editor.css").read_text(), "text/css; charset=utf-8")
        elif path == "/dashboard.js":
            self.respond((ROOT / "dashboard.js").read_text(), "text/javascript; charset=utf-8")
        elif path == "/admin/revision":
            with closing(database()) as db:
                self.respond(revision(db), "text/plain; charset=utf-8")
        elif path in ("/", "/admin"):
            with closing(database()) as db, db:
                if path == "/admin":
                    query = parse_qs(urlsplit(self.path).query)
                    self.respond(dashboard(db, query.get("deploy") == ["failed"], query.get("view", ["review"])[0]))
                else:
                    self.respond(public_page(db))
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
            if action not in ("save", "publish", "draft", "reject"):
                raise ValueError
            headline, summary = form.get("headline", "").strip(), form.get("summary", "").strip()
            if action in ("save", "publish") and not (1 <= len(headline) <= 115 and 1 <= len(summary) <= 350):
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
