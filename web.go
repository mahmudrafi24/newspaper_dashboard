package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

//go:embed templates/*.html
var templates embed.FS

var pages = template.Must(template.ParseFS(templates, "templates/*.html"))

type application struct {
	store                 *store
	site, root, port, csrf string
}

func newCSRF() string {
	var token [32]byte
	if _, err := rand.Read(token[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(token[:])
}

func (app *application) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", app.home)
	mux.HandleFunc("GET /admin", app.editor)
	mux.HandleFunc("POST /admin", app.edit)
	mux.HandleFunc("GET /admin/revision", app.revision)
	mux.HandleFunc("GET /healthz", app.health)
	for _, file := range []string{"editor.css", "dashboard.js"} {
		mux.HandleFunc("GET /"+file, app.asset(filepath.Join(app.root, file)))
	}
	for _, file := range publicAssets {
		path := filepath.Join(app.site, file)
		mux.HandleFunc("GET /"+file, app.asset(path))
		if base, ok := strings.CutSuffix(file, ".html"); ok {
			mux.HandleFunc("GET /"+base, app.asset(path))
			mux.HandleFunc("GET /"+base+"/{$}", app.asset(path))
		}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != "127.0.0.1:"+app.port && r.Host != "localhost:"+app.port {
			http.Error(w, "Forbidden", http.StatusForbidden)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
		mux.ServeHTTP(w, r)
	})
}

func (app *application) asset(path string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, path) }
}

func (app *application) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := app.store.PingContext(ctx); err != nil {
		http.Error(w, "Database unavailable", http.StatusServiceUnavailable)
		return
	}
	fmt.Fprintln(w, "ok")
}

func (app *application) revision(w http.ResponseWriter, r *http.Request) {
	revision, _, err := app.store.activity(r.Context())
	if err != nil {
		fail(w, err)
		return
	}
	fmt.Fprint(w, revision)
}

func (app *application) home(w http.ResponseWriter, r *http.Request) {
	body, err := app.publicPage(r.Context(), false)
	if err != nil {
		fail(w, err)
		return
	}
	writeHTML(w, body)
}

func writeHTML(w http.ResponseWriter, body []byte) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(body)
}

func fail(w http.ResponseWriter, err error) {
	log.Printf("dashboard: %v", err)
	http.Error(w, "Dashboard request failed", http.StatusInternalServerError)
}

type editorTab struct {
	Key, Label, First, Second string
	Count                     int
	Active                    bool
}

var editorTabs = []editorTab{
	{Key: "review", Label: "Pending review", First: "draft", Second: "draft"},
	{Key: "upload", Label: "Awaiting upload", First: "approved", Second: "withdrawing"},
	{Key: "published", Label: "Published", First: "published", Second: "published"},
	{Key: "rejected", Label: "Rejected", First: "rejected", Second: "rejected"},
}

type action struct{ Value, Label string }

type editorStory struct {
	story
	Label    string
	Editable bool
	Actions  []action
}

func editorCard(item story) editorStory {
	card := editorStory{story: item}
	switch item.State {
	case "draft":
		card.Label, card.Editable = "Pending review", true
		card.Actions = []action{{"publish", "Approve & publish"}, {"save", "Save draft"}, {"reject", "Reject"}}
	case "approved":
		card.Label = "Approved · Awaiting upload"
		if !item.Published.Valid {
			card.Actions = []action{{"draft", "Return to review"}}
		}
	case "published":
		card.Label, card.Editable = "Published", true
		card.Actions = []action{{"publish", "Approve & publish edits"}, {"draft", "Unpublish"}}
	case "withdrawing":
		card.Label, card.Editable = "Removal pending", true
		card.Actions = []action{{"publish", "Approve & republish"}}
	case "rejected":
		card.Label = "Rejected"
		card.Actions = []action{{"draft", "Return to review"}}
	}
	return card
}

func (app *application) editor(w http.ResponseWriter, r *http.Request) {
	ctx, query := r.Context(), r.URL.Query()
	revision, status, err := app.store.activity(ctx)
	if err != nil {
		fail(w, err)
		return
	}
	counts, err := app.store.counts(ctx)
	if err != nil {
		fail(w, err)
		return
	}
	tabs := append([]editorTab(nil), editorTabs...)
	selected := 0
	for i, tab := range tabs {
		tabs[i].Count = counts[tab.First]
		if tab.Second != tab.First {
			tabs[i].Count += counts[tab.Second]
		}
		if tab.Key == query.Get("view") {
			selected = i
		}
	}
	tabs[selected].Active = true
	tab := tabs[selected]
	const pageSize = 25
	last := max(1, (tab.Count+pageSize-1)/pageSize)
	pageNumber, _ := strconv.Atoi(query.Get("page"))
	pageNumber = min(max(1, pageNumber), last)
	stories, err := app.store.stories(ctx, tab.First, tab.Second, pageSize, (pageNumber-1)*pageSize)
	if err != nil {
		fail(w, err)
		return
	}
	cards := make([]editorStory, 0, len(stories))
	for _, item := range stories {
		cards = append(cards, editorCard(item))
	}
	data := struct {
		CSRF, Status, View, Label       string
		Revision                        int64
		Tabs                            []editorTab
		Cards                           []editorStory
		Pending, Failed                 bool
		Previous, Next, Page, Last      int
	}{
		CSRF: app.csrf, Status: status, View: tab.Key, Label: tab.Label,
		Revision: revision, Tabs: tabs, Cards: cards, Page: pageNumber, Last: last,
		Pending: counts["approved"]+counts["withdrawing"] > 0, Failed: query.Get("deploy") == "failed",
		Previous: pageNumber - 1,
	}
	if pageNumber < last {
		data.Next = pageNumber + 1
	}
	var body bytes.Buffer
	if err := pages.ExecuteTemplate(&body, "editor.html", data); err != nil {
		fail(w, err)
		return
	}
	writeHTML(w, body.Bytes())
}

func (app *application) edit(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 8192)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "Invalid form", http.StatusBadRequest)
		return
	}
	form := r.PostForm
	if subtle.ConstantTimeCompare([]byte(form.Get("csrf")), []byte(app.csrf)) != 1 {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}

	action := form.Get("action")
	upload := action == "retry"
	if !upload {
		switch action {
		case "publish", "save", "draft", "reject":
		default:
			http.Error(w, "Invalid action", http.StatusBadRequest)
			return
		}
		headline, summary := strings.TrimSpace(form.Get("headline")), strings.TrimSpace(form.Get("summary"))
		if action == "save" || action == "publish" {
			if headline == "" || summary == "" || utf8.RuneCountInString(headline) > 115 || utf8.RuneCountInString(summary) > 350 {
				http.Error(w, "Invalid headline or summary", http.StatusBadRequest)
				return
			}
		}
		var err error
		upload, err = app.store.edit(r.Context(), form.Get("id"), action, headline, summary)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			http.NotFound(w, r)
			return
		case errors.Is(err, errTransition):
			http.Error(w, err.Error(), http.StatusConflict)
			return
		case err != nil:
			fail(w, err)
			return
		}
	}
	location := "/admin"
	if upload {
		ctx, cancel := context.WithTimeout(r.Context(), 185*time.Second)
		defer cancel()
		if err := app.deploy(ctx); err != nil {
			log.Printf("Cloudflare upload failed: %v", err)
			location = "/admin?view=upload&deploy=failed"
		} else if action == "publish" {
			location = "/admin?view=published"
		}
	}
	http.Redirect(w, r, location, http.StatusSeeOther)
}
