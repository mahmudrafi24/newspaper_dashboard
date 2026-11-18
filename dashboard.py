import argparse
from contextlib import closing
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
sys.path.insert(0, str(ROOT.parent / "news_bot"))
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


def public_page(db):
    stories = db.execute("""
        SELECT * FROM stories WHERE state='published' ORDER BY reported DESC, id
    """).fetchall()
    template = (ROOT / "site" / "index.html").read_text()
    if not stories:
        return template
    desks = list(dict.fromkeys(story["desk"] for story in stories))
    links = "".join(f'<a href="#{e(desk.lower())}">{e(desk)}</a>' for desk in desks)
    sections = [f'<nav class="desks" aria-label="Browse market desks">{links}</nav>']
    for desk in desks:
        cards = []
        for story in stories:
            if story["desk"] != desk:
                continue
            cards.append(f"""<article><p class="meta">{e(story['source'])} · {e(story['reported'][:10])}</p>
<h3><a href="{e(story['url'])}">{e(story['headline'])}</a></h3>
<p>{e(story['summary'])}</p><a class="source" href="{e(story['url'])}">Read on Google News ↗</a></article>""")
        sections.append(f'<section id="{e(desk.lower())}"><h2>{e(desk)}</h2><div class="grid">{"".join(cards)}</div></section>')
    before, _, rest = template.partition("<!-- stories -->")
    _, _, after = rest.partition("<!-- /stories -->")
    return before + "\n".join(sections) + after


def revision(db):
    return str(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM runs").fetchone()[0])


def dashboard(db, deploy_failed=False):
    current = revision(db)
    run = db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    status = f"{run['at'][:16].replace('T', ' ')} UTC · {run['message']}" if run else "Waiting for the first research run"
    retry = ""
    if deploy_failed:
        retry = f"""<p role="alert">Saved on Oracle. The public site update failed.</p>
<form method="post" action="/admin"><input type="hidden" name="csrf" value="{CSRF}">
<button name="action" value="retry">Retry site update</button></form>"""
    cards = []
    for story in db.execute("SELECT * FROM stories ORDER BY state != 'draft', reported DESC"):
        drafts = story["state"] == "draft"
        button = "Publish" if drafts else "Move to drafts"
        name = e(story["headline"])
        summary = e(story["summary"])
        cards.append(f"""<article><p class="meta">{e(story['state'].title())} · {e(story['desk'])} · {e(story['source'])} · {e(story['reported'][:10])}</p>
<p><a href="{e(story['url'])}">{e(story['original'])} ↗</a></p>
<form method="post" action="/admin">
<input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="id" value="{e(story['id'])}">
<label>Headline<input name="headline" value="{name}" required maxlength="115"></label>
<label>Summary<textarea name="summary" required maxlength="350">{summary}</textarea></label>
<button name="action" value="save">Save edits</button>
<button class="primary" name="action" value="{'publish' if drafts else 'draft'}">{button}</button></form></article>""")
    content = "".join(cards) or '<p class="empty">New drafts will appear here after an hourly run succeeds.</p>'
    return page("Editorial dashboard", f'<h1>Editorial dashboard</h1><p class="status" data-revision="{current}">{e(status)}</p><p id="refresh-notice" role="status" hidden><a href="/admin">Research finished. Refresh the queue.</a></p>{retry}{content}', True)


def export(db):
    PUBLIC.mkdir(exist_ok=True)
    for name, content in (("index.html", public_page(db)), ("style.css", (ROOT / "site" / "style.css").read_text())):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=PUBLIC, delete=False) as output:
            output.write(content)
        os.chmod(output.name, 0o644)
        os.replace(output.name, PUBLIC / name)


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
            self.respond((ROOT / "site" / "style.css").read_text(), "text/css; charset=utf-8")
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
            if not (1 <= len(headline) <= 115 and 1 <= len(summary) <= 350 and action in ("save", "publish", "draft")):
                raise ValueError
            key = form["id"]
        except (ValueError, KeyError):
            self.send_error(400)
            return

        with closing(database()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            story = db.execute("SELECT state, published FROM stories WHERE id=?", (key,)).fetchone()
            if story is None:
                self.send_error(404)
                return
            state = {"publish": "published", "draft": "draft"}.get(action, story["state"])
            published = (story["published"] or now().isoformat()) if state == "published" else None
            db.execute("UPDATE stories SET headline=?, summary=?, state=?, published=? WHERE id=?",
                       (headline, summary, state, published, key))
        self.update_public(deploy=state == "published" or story["state"] == "published")

    def update_public(self, deploy):
        location = "/admin"
        try:
            refresh_public(deploy=deploy)
        except (OSError, subprocess.SubprocessError) as error:
            self.log_error("Public site update failed: %s", error)
            location = "/admin?deploy=failed"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


def refresh_public(deploy=False):
    with closing(database()) as db, db, (DATA / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        export(db)
        if deploy:
            subprocess.run(deploy_command(), cwd=ROOT, check=True, timeout=180)


def deploy_command():
    project = os.environ.get("CLOUDFLARE_PAGES_PROJECT")
    worker = os.environ.get("CLOUDFLARE_WORKER_NAME")
    wrangler = os.environ.get("WRANGLER_BIN", "wrangler")
    if worker and not project:
        return [wrangler, "deploy", "--name", worker, "--assets", str(PUBLIC)]
    if project and not worker:
        return [wrangler, "pages", "deploy", str(PUBLIC), "--project-name=" + project, "--branch=main"]
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
        if args.command == "deploy":
            try:
                deploy_command()
            except OSError as error:
                parser.error(str(error))
        refresh_public(deploy=args.command == "deploy")


if __name__ == "__main__":
    main()
