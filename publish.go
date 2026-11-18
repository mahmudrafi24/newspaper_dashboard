package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

var publicAssets = []string{
	"style.css", "share.js", "market-brief-2026-11-18.html", "october-market-review.html",
}

type publicCard struct {
	Story story
	Lead  bool
}

type publicDesk struct {
	ID, Name string
	Cards    []publicCard
}

func (app *application) publicPage(ctx context.Context, includeApproved bool) ([]byte, error) {
	second := "withdrawing"
	if includeApproved {
		second = "approved"
	}
	stories, err := app.store.stories(ctx, "published", second, -1, 0)
	if err != nil {
		return nil, err
	}
	index, err := os.ReadFile(filepath.Join(app.site, "index.html"))
	if err != nil || len(stories) == 0 {
		return index, err
	}

	before, rest, ok := bytes.Cut(index, []byte("<!-- stories -->"))
	if !ok {
		return nil, errors.New("public index is missing its opening stories marker")
	}
	_, after, ok := bytes.Cut(rest, []byte("<!-- /stories -->"))
	if !ok {
		return nil, errors.New("public index is missing its closing stories marker")
	}
	desks := make([]publicDesk, 0)
	deskIndex := make(map[string]int)
	for _, item := range stories[1:] {
		i, found := deskIndex[item.Desk]
		if !found {
			i = len(desks)
			deskIndex[item.Desk] = i
			desks = append(desks, publicDesk{ID: strings.ToLower(item.Desk), Name: item.Desk})
		}
		desks[i].Cards = append(desks[i].Cards, publicCard{Story: item})
	}
	var page bytes.Buffer
	page.Write(before)
	data := struct {
		Lead  publicCard
		Desks []publicDesk
	}{publicCard{Story: stories[0], Lead: true}, desks}
	if err := pages.ExecuteTemplate(&page, "stories.html", data); err != nil {
		return nil, err
	}
	page.Write(after)
	return page.Bytes(), nil
}

func (app *application) export(ctx context.Context, directory string, includeApproved bool) error {
	if err := os.MkdirAll(directory, 0755); err != nil {
		return err
	}
	index, err := app.publicPage(ctx, includeApproved)
	if err != nil {
		return err
	}
	for _, name := range publicAssets {
		content, err := os.ReadFile(filepath.Join(app.site, name))
		if err != nil {
			return err
		}
		if err := writeAsset(directory, name, content); err != nil {
			return err
		}
	}
	return writeAsset(directory, "index.html", index)
}

func writeAsset(directory, name string, content []byte) error {
	file, err := os.CreateTemp(directory, ".asset-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if _, err := file.Write(content); err != nil {
		return err
	}
	if err := file.Chmod(0644); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), filepath.Join(directory, name))
}

func (app *application) deploy(ctx context.Context) error {
	unlock, err := app.store.lock(ctx)
	if err != nil {
		return err
	}
	defer unlock()
	directory, err := os.MkdirTemp("", "market-ledger-upload-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	args, err := app.deployArgs(directory)
	if err != nil {
		return err
	}
	if err := app.export(ctx, directory, true); err != nil {
		return err
	}

	upload, cancel := context.WithTimeout(ctx, 180*time.Second)
	defer cancel()
	command := exec.CommandContext(upload, envOr("WRANGLER_BIN", "wrangler"), args...)
	command.Dir = app.root
	command.Stdout, command.Stderr = os.Stdout, os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("Wrangler deploy: %w", err)
	}

	// Once Cloudflare succeeds, record it even if the browser has disconnected.
	finish, stop := context.WithTimeout(context.Background(), 10*time.Second)
	defer stop()
	return app.store.finishDeployment(finish)
}

func (app *application) deployArgs(directory string) ([]string, error) {
	project, worker := os.Getenv("CLOUDFLARE_PAGES_PROJECT"), os.Getenv("CLOUDFLARE_WORKER_NAME")
	switch {
	case worker != "" && project == "":
		config := filepath.Join(filepath.Dir(app.site), "wrangler.jsonc")
		return []string{"deploy", "--config", config, "--name", worker, "--assets", directory}, nil
	case project != "" && worker == "":
		return []string{"pages", "deploy", directory, "--project-name=" + project, "--branch=main"}, nil
	default:
		return nil, errors.New("configure exactly one Cloudflare deployment target")
	}
}
