package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestApprovalAndUploadRetry(t *testing.T) {
	root := t.TempDir()
	site := filepath.Join(root, "site")
	if err := os.Mkdir(site, 0755); err != nil {
		t.Fatal(err)
	}
	for _, name := range append([]string{"index.html"}, publicAssets...) {
		if err := os.WriteFile(filepath.Join(site, name), []byte("<!-- stories --><!-- /stories -->"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	db, err := openStore(filepath.Join(root, "data"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	_, err = db.Exec(`INSERT INTO stories
		(id, original, source, url, reported, headline, summary, desk)
		VALUES ('one', 'Original', 'Source', 'https://example.org/one', '2026-11-18T10:00:00Z', 'Private draft', 'Draft summary', 'Equities'),
		('two', 'Second', 'Source', 'https://example.org/two', '2026-11-18T11:00:00Z', 'Second private draft', 'Draft summary', 'Economy')`)
	if err != nil {
		t.Fatal(err)
	}

	app := &application{store: db, site: site, root: root, port: "8765", csrf: "test-token"}
	handler := app.routes()
	request := func(method, path string, form url.Values) *httptest.ResponseRecorder {
		t.Helper()
		r := httptest.NewRequest(method, "http://127.0.0.1:8765"+path, strings.NewReader(form.Encode()))
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	editor := request(http.MethodGet, "/admin", nil)
	if editor.Code != 200 || !strings.Contains(editor.Body.String(), "Private draft") {
		t.Fatalf("editor did not render drafts: %d %s", editor.Code, editor.Body.String())
	}
	if home := request(http.MethodGet, "/", nil); strings.Contains(home.Body.String(), "Private draft") {
		t.Fatal("draft appeared on public home")
	}

	t.Setenv("CLOUDFLARE_PAGES_PROJECT", "")
	t.Setenv("CLOUDFLARE_WORKER_NAME", "test-worker")
	wrangler := filepath.Join(root, "wrangler")
	t.Setenv("WRANGLER_BIN", wrangler)
	if err := os.WriteFile(wrangler, []byte("#!/bin/sh\nexit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	result := request(http.MethodPost, "/admin", url.Values{
		"csrf": {"test-token"}, "id": {"one"}, "action": {"publish"},
		"headline": {"Approved <headline>"}, "summary": {"Approved summary"},
	})
	if result.Code != http.StatusSeeOther || !strings.Contains(result.Header().Get("Location"), "deploy=failed") {
		t.Fatalf("expected upload failure redirect: %d %s", result.Code, result.Body.String())
	}
	approved, err := db.stories(context.Background(), "approved", "approved", 10, 0)
	if err != nil || len(approved) != 1 {
		t.Fatalf("approval was not retained: %v, %v", approved, err)
	}
	if home := request(http.MethodGet, "/", nil); strings.Contains(home.Body.String(), "Approved") {
		t.Fatal("failed upload leaked into current public home")
	}

	script := "#!/bin/sh\n" +
		"for arg do asset_dir=$arg; done\n" +
		"grep -q 'Approved &lt;headline&gt;' \"$asset_dir/index.html\" && " +
		"! grep -q 'Second private draft' \"$asset_dir/index.html\" && test ! -e \"$asset_dir/editor.css\"\n"
	if err := os.WriteFile(wrangler, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	retry := request(http.MethodPost, "/admin", url.Values{"csrf": {"test-token"}, "action": {"retry"}})
	if retry.Code != http.StatusSeeOther || strings.Contains(retry.Header().Get("Location"), "deploy=failed") {
		t.Fatalf("retry failed: %d %s", retry.Code, retry.Body.String())
	}
	home := request(http.MethodGet, "/", nil)
	if !strings.Contains(home.Body.String(), "Approved &lt;headline&gt;") || strings.Contains(home.Body.String(), "Second private draft") {
		t.Fatalf("unexpected public home: %s", home.Body.String())
	}
}
