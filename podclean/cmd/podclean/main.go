// Command podclean serves a podcast feed with the advertising cut out of its episodes.
//
// Everything it needs is in the environment, read at use. It starts by listening: no
// request goes out to anybody until somebody asks for a feed.
package main

import (
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"podclean/internal/config"
	"podclean/internal/episode"
	"podclean/internal/outside"
	"podclean/internal/store"
	"podclean/internal/web"
)

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	watchParent(config.ParentPID())

	files := store.Store{Root: config.StoreRoot()}
	if err := os.MkdirAll(files.Root, 0o755); err != nil {
		log.Fatalf("cannot use %s as the store: %v", files.Root, err)
	}
	client := &outside.Client{
		TranscribeBaseURL: config.TranscribeBaseURL(),
		LLMBaseURL:        config.LLMBaseURL(),
		APIKey:            config.APIKey(),
	}
	server := &web.Server{
		Store:   files,
		Outside: client,
		Producer: &episode.Producer{
			Store: files, Outside: client,
			Spec: config.LLMSpec(), MaxBytes: config.TranscribeMaxBytes(),
		},
		BaseURL: config.BaseURL(),
	}

	address := net.JoinHostPort(config.Host(), config.Port())
	log.Printf("podclean listening on %s, calling itself %s, storing in %s",
		address, config.BaseURL(), files.Root)
	listening := &http.Server{
		Addr:    address,
		Handler: server.Handler(),
		// An episode takes three to five minutes to produce and the connection is held
		// the whole time, so there is no write deadline: cutting one off at a timeout
		// would send a listener half a file, which is the one thing that must not happen.
		ReadHeaderTimeout: 30 * time.Second,
	}
	log.Fatal(listening.ListenAndServe())
}

// watchParent halts when the process named by PODCLEANER_PARENT_PID goes away.
//
// `./run serve` is a `docker run`, and killing that client does not stop the container it
// started. Without this, every test run and every restart leaves a server behind holding
// a port and an episode's worth of memory on a host that has four gigabytes of it.
func watchParent(pid string) {
	if pid == "" {
		return
	}
	go func() {
		for {
			if _, err := os.Stat("/proc/" + pid); err != nil {
				log.Printf("the process that started this one (%s) is gone", pid)
				os.Exit(0)
			}
			time.Sleep(time.Second)
		}
	}()
}
