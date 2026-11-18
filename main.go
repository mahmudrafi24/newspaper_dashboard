package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		log.Fatal(err)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: market-ledger {serve|export|deploy} [--port 8765]")
	}
	command := args[0]
	if command != "serve" && command != "export" && command != "deploy" {
		return fmt.Errorf("unknown command %q", command)
	}
	options := flag.NewFlagSet(command, flag.ContinueOnError)
	port := options.Int("port", 8765, "loopback editor port")
	if err := options.Parse(args[1:]); err != nil {
		return err
	}
	if *port < 1 || *port > 65535 || options.NArg() != 0 {
		return errors.New("invalid port or unexpected arguments")
	}

	root, err := os.Getwd()
	if err != nil {
		return err
	}
	bot := filepath.Join(filepath.Dir(root), "news_bot")
	dataDir, err := filepath.Abs(envOr("TECH_NEWS_DATA_DIR", filepath.Join(bot, "data")))
	if err != nil {
		return err
	}
	db, err := openStore(dataDir)
	if err != nil {
		return err
	}
	defer db.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	app := &application{
		store: db, site: filepath.Join(bot, "site"), root: root,
		port: strconv.Itoa(*port), csrf: newCSRF(),
	}
	switch command {
	case "export":
		return app.export(ctx, filepath.Join(root, "public"), false)
	case "deploy":
		return app.deploy(ctx)
	}

	address := net.JoinHostPort("127.0.0.1", app.port)
	server := &http.Server{
		Addr: address, Handler: app.routes(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 3*time.Minute + 15*time.Second, IdleTimeout: 60 * time.Second,
		MaxHeaderBytes: 32 << 10,
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	defer listener.Close()
	log.Printf("Dashboard at http://%s/admin", address)
	stopped := make(chan error, 1)
	go func() { stopped <- server.Serve(listener) }()
	select {
	case err := <-stopped:
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(shutdown)
	}
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
