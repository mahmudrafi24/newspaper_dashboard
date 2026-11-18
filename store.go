package main

import (
	"context"
	"database/sql"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

type store struct {
	*sql.DB
	dir string
}

type story struct {
	ID, Original, Source, URL, Reported, Headline, Summary, Desk, State string
	Published                                                        sql.NullString
}

func (s story) Date() string {
	date, _, _ := strings.Cut(s.Reported, "T")
	return date
}

const storyColumns = "id, original, source, url, reported, headline, summary, desk, state, published"

func openStore(dir string) (*store, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	file := url.URL{Scheme: "file", Path: filepath.Join(dir, "markets.sqlite")}
	file.RawQuery = "_busy_timeout=5000&_journal_mode=WAL"
	db, err := sql.Open("sqlite3", file.String())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS stories (
			id TEXT PRIMARY KEY, original TEXT NOT NULL, source TEXT NOT NULL,
			url TEXT NOT NULL, reported TEXT NOT NULL, headline TEXT NOT NULL,
			summary TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft', published TEXT,
			desk TEXT NOT NULL
		);
		CREATE TABLE IF NOT EXISTS runs (at TEXT NOT NULL, message TEXT NOT NULL);
		CREATE INDEX IF NOT EXISTS stories_state_reported ON stories(state, julianday(reported) DESC, id);
	`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &store{db, dir}, nil
}

func scanStory(row interface{ Scan(...any) error }) (story, error) {
	var s story
	err := row.Scan(&s.ID, &s.Original, &s.Source, &s.URL, &s.Reported, &s.Headline, &s.Summary, &s.Desk, &s.State, &s.Published)
	return s, err
}

func (s *store) stories(ctx context.Context, first, second string, limit, offset int) ([]story, error) {
	rows, err := s.QueryContext(ctx, "SELECT "+storyColumns+` FROM stories
		WHERE state IN (?, ?) ORDER BY julianday(reported) DESC, id LIMIT ? OFFSET ?`, first, second, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var stories []story
	for rows.Next() {
		item, err := scanStory(rows)
		if err != nil {
			return nil, err
		}
		stories = append(stories, item)
	}
	return stories, rows.Err()
}

func (s *store) activity(ctx context.Context) (int64, string, error) {
	var id int64
	var at, message string
	err := s.QueryRowContext(ctx, "SELECT rowid, at, message FROM runs ORDER BY rowid DESC LIMIT 1").Scan(&id, &at, &message)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, "Waiting for the first research run", nil
	}
	if err != nil {
		return 0, "", err
	}
	if timestamp, err := time.Parse(time.RFC3339Nano, at); err == nil {
		at = timestamp.UTC().Format("2006-01-02 15:04 UTC")
	}
	return id, at + " · " + message, nil
}

func (s *store) counts(ctx context.Context) (map[string]int, error) {
	rows, err := s.QueryContext(ctx, "SELECT state, COUNT(*) FROM stories GROUP BY state")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	counts := make(map[string]int)
	for rows.Next() {
		var state string
		var count int
		if err := rows.Scan(&state, &count); err != nil {
			return nil, err
		}
		counts[state] = count
	}
	return counts, rows.Err()
}

// flock is shared with the previous Python deployment on the Oracle host.
func (s *store) lock(ctx context.Context) (func(), error) {
	file, err := os.OpenFile(filepath.Join(s.dir, "deploy.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	tick := time.NewTicker(50 * time.Millisecond)
	defer tick.Stop()
	for {
		err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return func() { file.Close() }, nil
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) {
			file.Close()
			return nil, err
		}
		select {
		case <-ctx.Done():
			file.Close()
			return nil, ctx.Err()
		case <-tick.C:
		}
	}
}

var errTransition = errors.New("story status changed; reload the dashboard")

var transitions = map[string]map[string]string{
	"draft":       {"save": "draft", "reject": "rejected", "publish": "approved"},
	"approved":    {"draft": "draft"},
	"rejected":    {"draft": "draft"},
	"published":   {"publish": "approved", "draft": "withdrawing"},
	"withdrawing": {"publish": "approved"},
}

func (s *store) edit(ctx context.Context, id, action, headline, summary string) (bool, error) {
	unlock, err := s.lock(ctx)
	if err != nil {
		return false, err
	}
	defer unlock()

	item, err := scanStory(s.QueryRowContext(ctx, "SELECT "+storyColumns+" FROM stories WHERE id=?", id))
	if err != nil {
		return false, err
	}
	next := transitions[item.State][action]
	if next == "" || (item.State == "approved" && action == "draft" && item.Published.Valid) {
		return false, errTransition
	}
	if action != "save" && action != "publish" {
		headline, summary = item.Headline, item.Summary
	}
	upload := next == "approved" || next == "withdrawing"
	published := sql.NullString{}
	if upload {
		published = item.Published
	}
	_, err = s.ExecContext(ctx, "UPDATE stories SET headline=?, summary=?, state=?, published=? WHERE id=?",
		headline, summary, next, published, id)
	return upload, err
}

func (s *store) finishDeployment(ctx context.Context) error {
	tx, err := s.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "UPDATE stories SET state='published', published=COALESCE(published, ?) WHERE state='approved'",
		time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE stories SET state='draft', published=NULL WHERE state='withdrawing'"); err != nil {
		return err
	}
	return tx.Commit()
}
