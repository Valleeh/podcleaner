// Package episode is the pipeline: one episode from the publisher's bytes to the
// listener's, and the record of what was decided on the way.
//
// Fetch, check it is audio, transcribe, classify, plan, cut, publish. Each stage is one
// call into a package that does one thing, and this file is the order they happen in and
// what each failure means. The interesting decisions are not here -- they are in `plan`,
// which never talks to anyone, and in `mp3`, which never decides anything.
package episode

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"

	"podclean/internal/classify"
	"podclean/internal/feed"
	"podclean/internal/mp3"
	"podclean/internal/outside"
	"podclean/internal/plan"
	"podclean/internal/store"
	"podclean/internal/transcript"
)

// longestEpisode is the longest episode this server will examine. Past it, the
// publisher's audio is served untouched, unexamined and unbilled: transcribing something
// that long costs real money, and a file that size is more likely a concatenated archive
// or a live stream dump than an episode with advertising breaks in it.
const longestEpisode = 8400

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

// Feed is the publisher's feed with its links repointed here. Reading a feed is also the
// only thing that makes its episodes playable: an episode is addressable because a feed
// this server fetched named it, never because somebody asked for it.
func (p *Orchestrator) Feed(url string) ([]byte, error) {
	document, err := p.Outside.Feed(url)
	if err != nil {
		return nil, err
	}
	rewritten, episodes, err := feed.Rewrite(document, url, p.BaseURL)
	if err != nil {
		return nil, err
	}
	for _, e := range episodes {
		if err := p.Store.PutSource(url, e.GUID, store.Source{
			URL: e.Enclosure, Feed: url, ChaptersURL: e.ChaptersURL}); err != nil {
			log.Printf("feed=%s guid=%s cannot record the episode: %v", url, e.GUID, err)
		}
	}
	return rewritten, nil
}

// Chapters and Transcript are the two sidecars, once the episode has been produced.
// Neither ever starts the pipeline: a route a crawler can reach must never begin a paid,
// minutes-long run, and a document that describes a cut cannot exist before the cut does.
func (p *Orchestrator) Chapters(feed, guid string) ([]byte, bool) {
	return p.Store.Read(feed, guid, store.ChaptersFile)
}

func (p *Orchestrator) Transcript(feed, guid string) ([]byte, bool) {
	return p.Store.Read(feed, guid, store.TranscriptFile)
}

// Play is the audio for one episode, producing it if this is the first time it is asked
// for. The connection is held until the audio is whole: there is no state in which a
// listener receives half an episode.
func (p *Orchestrator) Play(feed, guid string) (Audio, error) {
	source, ok := p.Store.Source(feed, guid)
	if !ok {
		return Audio{}, ErrUnknown
	}
	// Reading a finished episode takes no lock. Everything below the lock is the work
	// that costs money, and a listener playing an episode produced last week must not
	// queue behind one being produced now.
	if audio, ok := p.published(feed, guid); ok {
		return audio, nil
	}

	p.lock.Lock()
	defer p.lock.Unlock()
	if audio, ok := p.published(feed, guid); ok {
		return audio, nil
	}
	return p.finish(feed, guid, p.produce(source))
}

func (p *Orchestrator) published(feed, guid string) (Audio, bool) {
	if !p.Store.Has(feed, guid, store.AudioFile) {
		return Audio{}, false
	}
	return Audio{Path: filepath.Join(p.Store.Root, store.Key(feed, guid), store.AudioFile)}, true
}

// An outcome is everything one run of the pipeline decided, before any of it is written.
type outcome struct {
	verdict verdict
	audio   []byte // what the listener gets; nil when the publisher sent nothing usable

	// The two sidecars, nil when nothing was read and so there is nothing this server
	// could truthfully say about the episode.
	chapters   []byte
	transcript []byte

	// err is a stage that did not finish. Nothing but the verdict is kept, so the next
	// play does the work again.
	err error
}

// produce is the pipeline itself: every stage, in order, and what each failure means.
// It writes nothing; finish does.
func (p *Orchestrator) produce(source store.Source) outcome {
	v := verdict{Schema: "podclean.verdict/1", State: "failed", ModelSpec: p.Spec}

	// The publisher. A failure here is the one failure with no audio to fall back on, so
	// it is the one the listener is told about.
	raw, err := p.Outside.Audio(source.URL)
	if err != nil {
		return outcome{verdict: v, err: err}
	}
	sum := sha256.Sum256(raw)
	v.SourceSHA256 = hex.EncodeToString(sum[:])

	// What is published is audio the publisher sent, not whatever their server answered
	// with. A block page carrying an episode's content type and its own length is a
	// failure however it describes itself -- and the question asked of it is the same one
	// the cutter asks: do these bytes parse as MP3 frames?
	file, err := mp3.Parse(raw)
	if err != nil {
		return outcome{verdict: v, err: &outside.RemoteError{
			Message: fmt.Sprintf("%s did not answer with audio: %v", source.URL, err)}}
	}
	v.DurationSeconds = round(file.Seconds())

	if file.Seconds() > longestEpisode {
		v.State = "untouched"
		v.Error = fmt.Sprintf("%.0f s is longer than the %d s this server will examine",
			file.Seconds(), longestEpisode)
		return outcome{verdict: v, audio: raw}
	}

	// Everything from here on has the episode in hand. A failure is answered with the
	// publisher's own bytes and a 200.
	text, err := p.transcribe(file)
	if err != nil {
		return outcome{verdict: v, audio: raw, err: err}
	}
	v.Cues = len(text.Cues)

	reply, err := classify.Task{Spec: p.Spec, Completer: p.Outside}.Run(
		p.Outside.PublisherChapters(source.ChaptersURL), text.Render())
	if err != nil {
		return outcome{verdict: v, audio: raw, err: err}
	}
	v.Proposed = proposed(reply.Segments)
	v.ProposedCount = len(reply.Segments)

	decision := plan.Build(text, reply.Segments, file.Seconds())
	v.State = decision.State
	v.Error = errorOf(decision)
	line := decision.Timeline()
	v.Removed = spans(line.Removed)
	v.RemovedSeconds = round(line.Total())

	chapters := line.Chapters(plan.Chapters(text, reply.Chapters))
	v.Chapters = marks(chapters)

	audio := raw
	if decision.State == "cut" {
		audio = append(mp3.ChapterTag(chapters, file.Seconds()-line.Total()),
			file.Cut(line)...)
	}
	return outcome{verdict: v, audio: audio,
		chapters: chaptersJSON(chapters), transcript: []byte(text.VTT(line))}
}

// transcribe sends the episode up in pieces and puts the answers back on one timeline.
func (p *Orchestrator) transcribe(file *mp3.File) (*transcript.Transcript, error) {
	var parsed []transcript.Piece
	for _, piece := range file.Pieces(p.MaxBytes) {
		body, err := p.Outside.Transcribe(piece.Data)
		if err != nil {
			return nil, err
		}
		answer, err := transcript.Parse(body, piece.Start)
		if err != nil {
			return nil, err
		}
		parsed = append(parsed, answer)
	}
	return transcript.Join(parsed, file.Seconds()), nil
}

// finish writes an outcome down and says what the listener gets.
//
// The audio first, the sidecars next and the verdict last: a verdict is the record that
// the work finished, so it must not exist before the work it describes. An outcome that
// failed writes the verdict alone. With no audio the listener is told why; with the
// publisher's audio in hand they get that, out of memory, and nothing is kept.
func (p *Orchestrator) finish(feed, guid string, o outcome) (Audio, error) {
	if o.err != nil {
		o.verdict.Error = o.err.Error()
		p.record(feed, guid, o.verdict)
		if o.audio == nil {
			return Audio{}, o.err
		}
		log.Printf("episode=%s stage failed, serving the publisher's own audio: %v",
			store.Key(feed, guid), o.err)
		return Audio{Bytes: o.audio}, nil
	}
	if err := p.Store.Put(feed, guid, store.AudioFile, o.audio); err != nil {
		return Audio{Bytes: o.audio}, nil
	}
	if o.chapters != nil {
		_ = p.Store.Put(feed, guid, store.ChaptersFile, o.chapters)
	}
	if o.transcript != nil {
		_ = p.Store.Put(feed, guid, store.TranscriptFile, o.transcript)
	}
	p.record(feed, guid, o.verdict)
	stored, _ := p.published(feed, guid)
	return stored, nil
}

func (p *Orchestrator) record(feed, guid string, v verdict) {
	body, err := json.Marshal(v)
	if err != nil {
		return
	}
	_ = p.Store.Put(feed, guid, store.VerdictFile, body)
}
