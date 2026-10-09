// Package store is what this server keeps on disk: one directory per episode, five files
// in it, and nothing else.
//
// Every file is written to a temporary name and renamed into place, so a reader sees a
// file whole or not at all. That matters because the same episode can be asked for while
// it is being produced, and half a document is worse than no document.
package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// The five names. Three programs share them -- this server, `./run verify` and
// `./run health` -- so renaming one silently breaks a tool rather than the server.
const (
	SourceFile     = "source.json"
	AudioFile      = "audio.mp3"
	ChaptersFile   = "chapters.json"
	TranscriptFile = "transcript.vtt"
	VerdictFile    = "verdict.json"
)

// A Store is one directory with an episode directory under it per (feed, episode).
type Store struct{ Root string }

// A Source is what a feed said about one episode. Its presence is what makes the episode
// playable: no source, 404, and nothing goes out. A caller cannot make this server fetch
// a URL it was handed rather than one it read in a feed itself.
type Source struct {
	URL         string  `json:"url"`
	Feed        string  `json:"feed"`
	ChaptersURL *string `json:"chapters_url"`
	Language    string  `json:"language,omitempty"` // two letters, from the feed's channel
}

// Key is the name of one episode's directory: the first 32 hex characters of the sha256
// of the feed and the guid with a NUL between them.
//
// The NUL is what stops a feed URL that ends in an episode id from colliding with another
// pair. `tools/verify.py` derives this same key independently rather than asking the
// server for it, so that a server which put its files somewhere else is caught there
// instead of quietly verifying nothing -- which means this line cannot change alone.
func Key(feed, guid string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(feed) + "\x00" + strings.TrimSpace(guid)))
	return hex.EncodeToString(sum[:])[:32]
}

func (s Store) dir(feed, guid string) string { return filepath.Join(s.Root, Key(feed, guid)) }

// PutSource records that a feed named this episode.
func (s Store) PutSource(feed, guid string, source Source) error {
	body, err := json.Marshal(source)
	if err != nil {
		return err
	}
	return s.Put(feed, guid, SourceFile, body)
}

// Source is what a feed said about this episode, if any feed ever did.
func (s Store) Source(feed, guid string) (Source, bool) {
	body, ok := s.Read(feed, guid, SourceFile)
	if !ok {
		return Source{}, false
	}
	var source Source
	if err := json.Unmarshal(body, &source); err != nil || source.URL == "" {
		return Source{}, false
	}
	return source, true
}

// Read is one file of this episode's, or not there.
func (s Store) Read(feed, guid, name string) ([]byte, bool) {
	body, err := os.ReadFile(filepath.Join(s.dir(feed, guid), name))
	if err != nil {
		return nil, false
	}
	return body, true
}

// Has says whether this episode has this file, without reading it.
func (s Store) Has(feed, guid, name string) bool {
	_, err := os.Stat(filepath.Join(s.dir(feed, guid), name))
	return err == nil
}

// Put writes one file of this episode's, whole or not at all.
func (s Store) Put(feed, guid, name string, body []byte) error {
	dir := s.dir(feed, guid)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp := filepath.Join(dir, name+".tmp")
	if err := os.WriteFile(tmp, body, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(dir, name))
}
