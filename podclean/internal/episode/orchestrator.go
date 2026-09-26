// Package episode is the orchestrator and the pipeline it runs: one episode from the
// publisher's bytes to the listener's, and the record of what was decided on the way.
//
// orchestrator.go is what the webserver asks for and when the pipeline runs; pipeline.go
// is the stages in order; verdict.go is the record. The interesting decisions are not
// here -- they are in `plan`, which never talks to anyone, and in `mp3`, which never
// decides anything.
package episode

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"

	"podclean/internal/feed"
	"podclean/internal/outside"
	"podclean/internal/store"
)

// ErrUnknown is a (feed, episode) pair no feed fetched by this server has ever named.
var ErrUnknown = errors.New("unknown episode")

// An Orchestrator is everything the webserver asks for: a feed rewritten, an episode's
// audio, its two sidecars. It decides when the pipeline runs and keeps what it made.
type Orchestrator struct {
	Store    store.Store
	Outside  *outside.Client
	Spec     string
	MaxBytes int
	BaseURL  string // what the server calls itself in the links it writes into a feed

	// Production is taken under one lock for the whole server, not one per episode. An
	// episode is tens of megabytes held in memory and a transcription of the whole thing;
	// two at once is how this host runs out of memory. A second play of the same episode
	// waits here and then finds the first one's work done, rather than paying again.
	lock sync.Mutex
}

// Feed is the publisher's feed with its links repointed here. Reading a feed is also the
// only thing that makes its episodes playable: an episode is addressable because a feed
// this server fetched named it, never because somebody asked for it.
func (o *Orchestrator) Feed(url string) ([]byte, error) {
	document, err := o.Outside.Feed(url)
	if err != nil {
		return nil, err
	}
	rewritten, episodes, err := feed.Rewrite(document, url, o.BaseURL)
	if err != nil {
		return nil, err
	}
	for _, e := range episodes {
		if err := o.Store.PutSource(url, e.GUID, store.Source{
			URL: e.Enclosure, Feed: url, ChaptersURL: e.ChaptersURL}); err != nil {
			log.Printf("feed=%s guid=%s cannot record the episode: %v", url, e.GUID, err)
		}
	}
	return rewritten, nil
}

// Chapters and Transcript are the two sidecars, once the episode has been produced.
// Neither ever starts the pipeline: a route a crawler can reach must never begin a paid,
// minutes-long run, and a document that describes a cut cannot exist before the cut does.
func (o *Orchestrator) Chapters(feed, guid string) ([]byte, bool) {
	return o.Store.Read(feed, guid, store.ChaptersFile)
}

func (o *Orchestrator) Transcript(feed, guid string) ([]byte, bool) {
	return o.Store.Read(feed, guid, store.TranscriptFile)
}

// Play is the audio for one episode, running the pipeline if this is the first time it
// is asked for. The connection is held until the audio is whole: there is no state in
// which a listener receives half an episode.
func (o *Orchestrator) Play(feed, guid string) (Audio, error) {
	source, ok := o.Store.Source(feed, guid)
	if !ok {
		return Audio{}, ErrUnknown
	}
	// Reading a finished episode takes no lock. Everything below the lock is the work
	// that costs money, and a listener playing an episode produced last week must not
	// queue behind one being produced now.
	if audio, ok := o.published(feed, guid); ok {
		return audio, nil
	}

	o.lock.Lock()
	defer o.lock.Unlock()
	if audio, ok := o.published(feed, guid); ok {
		return audio, nil
	}
	return o.save(feed, guid, o.pipeline(source))
}

func (o *Orchestrator) published(feed, guid string) (Audio, bool) {
	if !o.Store.Has(feed, guid, store.AudioFile) {
		return Audio{}, false
	}
	return Audio{Path: filepath.Join(o.Store.Root, store.Key(feed, guid), store.AudioFile)}, true
}

// save writes a result down and says what the listener gets.
//
// The audio first, the sidecars next and the verdict last: a verdict is the record that
// the work finished, so it must not exist before the work it describes. A result that
// failed writes the verdict alone, so the next play does the work again. If the publisher
// had sent audio by then the listener gets it, out of memory; if not, they are told why.
func (o *Orchestrator) save(feed, guid string, r result) (Audio, error) {
	v := verdictOf(o.Spec, r)
	if r.err != nil {
		o.record(feed, guid, v)
		if r.file == nil {
			return Audio{}, r.err
		}
		log.Printf("episode=%s stage failed, serving the publisher's own audio: %v",
			store.Key(feed, guid), r.err)
		return Audio{Bytes: r.raw}, nil
	}
	if err := o.Store.Put(feed, guid, store.AudioFile, r.served); err != nil {
		log.Printf("episode=%s cannot keep the audio, serving it from memory: %v",
			store.Key(feed, guid), err)
		return Audio{Bytes: r.served}, nil
	}
	// Untouched was never read, so there is nothing this server could truthfully say
	// about it: no sidecars.
	if !r.untouched {
		_ = o.Store.Put(feed, guid, store.ChaptersFile, chaptersJSON(r.chapters))
		_ = o.Store.Put(feed, guid, store.TranscriptFile, []byte(r.vtt))
	}
	o.record(feed, guid, v)
	stored, _ := o.published(feed, guid)
	return stored, nil
}

func (o *Orchestrator) record(feed, guid string, v verdict) {
	body, err := json.Marshal(v)
	if err != nil {
		return
	}
	_ = o.Store.Put(feed, guid, store.VerdictFile, body)
}

// An Audio is the episode to serve: a file once it has been published, bytes while a
// stage has failed and the publisher's own audio is being handed straight through.
type Audio struct {
	Path  string
	Bytes []byte
}

// Open is the audio as something to read a range out of, and how long it is.
func (a Audio) Open() (io.ReadSeekCloser, int64, error) {
	if a.Path == "" {
		return nopCloser{bytes.NewReader(a.Bytes)}, int64(len(a.Bytes)), nil
	}
	file, err := os.Open(a.Path)
	if err != nil {
		return nil, 0, err
	}
	info, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, 0, err
	}
	return file, info.Size(), nil
}

type nopCloser struct{ io.ReadSeeker }

func (nopCloser) Close() error { return nil }
