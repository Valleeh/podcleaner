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

// A Producer makes episodes, one at a time.
type Producer struct {
	Store    store.Store
	Outside  *outside.Client
	Spec     string
	MaxBytes int

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

// Play is the audio for one episode, producing it if this is the first time it is asked
// for. The connection is held until the audio is whole: there is no state in which a
// listener receives half an episode.
func (p *Producer) Play(feed, guid string) (Audio, error) {
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
	return p.produce(feed, guid, source)
}

func (p *Producer) published(feed, guid string) (Audio, bool) {
	if !p.Store.Has(feed, guid, store.AudioFile) {
		return Audio{}, false
	}
	return Audio{Path: filepath.Join(p.Store.Root, store.Key(feed, guid), store.AudioFile)}, true
}

// produce is the pipeline itself.
func (p *Producer) produce(feed, guid string, source store.Source) (Audio, error) {
	v := verdict{Schema: "podclean.verdict/1", State: "failed", ModelSpec: p.Spec}

	// The publisher. A failure here is the one failure with no audio to fall back on, so
	// it is the one the listener is told about -- and nothing is kept, so the next play
	// asks the publisher again rather than remembering that it once broke.
	raw, err := p.Outside.Audio(source.URL)
	if err != nil {
		return p.failed(feed, guid, v, err)
	}
	sum := sha256.Sum256(raw)
	v.SourceSHA256 = hex.EncodeToString(sum[:])

	// What is published is audio the publisher sent, not whatever their server answered
	// with. A block page carrying an episode's content type and its own length is a
	// failure however it describes itself -- and the question asked of it is the same one
	// the cutter asks: do these bytes parse as MP3 frames?
	file, err := mp3.Parse(raw)
	if err != nil {
		return p.failed(feed, guid, v, &outside.RemoteError{
			Message: fmt.Sprintf("%s did not answer with audio: %v", source.URL, err)})
	}
	v.DurationSeconds = round(file.Seconds())

	if file.Seconds() > longestEpisode {
		v.State = "untouched"
		v.Error = fmt.Sprintf("%.0f s is longer than the %d s this server will examine",
			file.Seconds(), longestEpisode)
		return p.publishUntouched(feed, guid, raw, v)
	}

	// Everything from here on has the episode in hand. A failure is answered with the
	// publisher's own bytes and a 200, and remembered only as work that did not finish,
	// so that the next play does it again.
	text, err := p.transcribe(file)
	if err != nil {
		return p.failedWithAudio(feed, guid, raw, v, err)
	}
	v.Cues = len(text.Cues)

	reply, err := classify.Task{Spec: p.Spec, Completer: p.Outside}.Run(
		publisherMarks(p.Outside, source.ChaptersURL), text.Render())
	if err != nil {
		return p.failedWithAudio(feed, guid, raw, v, err)
	}
	v.Proposed = proposed(reply.Segments)
	v.ProposedCount = len(reply.Segments)

	decision := plan.Build(text, reply.Segments, file.Seconds())
	v.State = decision.State
	v.Error = errorOf(decision)
	line := decision.Timeline()
	v.Removed = spans(line.Removed)
	v.RemovedSeconds = round(line.Total())

	chapters := plan.Served(plan.Chapters(text, reply.Chapters), line)
	v.Chapters = marks(chapters)

	audio := raw
	if decision.State == "cut" {
		audio = append(mp3.ChapterTag(tagMarks(chapters), file.Seconds()-line.Total()),
			file.Cut(line)...)
	}
	return p.publish(feed, guid, audio, text.VTT(line), chapters, v)
}

// transcribe sends the episode up in pieces and puts the answers back on one timeline.
func (p *Producer) transcribe(file *mp3.File) (*transcript.Transcript, error) {
	pieces := file.Pieces(p.MaxBytes)
	parsed := make([]transcript.Piece, 0, len(pieces))
	starts := make([]float64, 0, len(pieces))
	for _, piece := range pieces {
		body, err := p.Outside.Transcribe(piece.Data)
		if err != nil {
			return nil, err
		}
		answer, err := transcript.Parse(body)
		if err != nil {
			return nil, err
		}
		parsed = append(parsed, answer)
		starts = append(starts, piece.Start)
	}
	return transcript.Join(parsed, starts), nil
}

func publisherMarks(client *outside.Client, url *string) []classify.Mark {
	if url == nil || *url == "" {
		return nil
	}
	var marks []classify.Mark
	for _, c := range client.PublisherChapters(*url) {
		marks = append(marks, classify.Mark{At: c.StartTime, Title: c.Title})
	}
	return marks
}

// publish writes the audio first, the documents next and the verdict last: a verdict is
// the record that the work finished, so it must not exist before the work it describes.
func (p *Producer) publish(feed, guid string, audio []byte, vtt string,
	chapters []plan.Chapter, v verdict) (Audio, error) {
	if err := p.Store.Put(feed, guid, store.AudioFile, audio); err != nil {
		return Audio{Bytes: audio}, nil
	}
	_ = p.Store.Put(feed, guid, store.ChaptersFile, chaptersJSON(chapters))
	_ = p.Store.Put(feed, guid, store.TranscriptFile, []byte(vtt))
	p.record(feed, guid, v)
	stored, _ := p.published(feed, guid)
	return stored, nil
}

// publishUntouched keeps the publisher's audio and writes no documents: nothing was read,
// so there is nothing this server could truthfully say about it.
func (p *Producer) publishUntouched(feed, guid string, raw []byte, v verdict) (Audio, error) {
	if err := p.Store.Put(feed, guid, store.AudioFile, raw); err != nil {
		return Audio{Bytes: raw}, nil
	}
	p.record(feed, guid, v)
	stored, _ := p.published(feed, guid)
	return stored, nil
}

// failed is a stage that did not finish with nothing to serve: the listener is told.
func (p *Producer) failed(feed, guid string, v verdict, err error) (Audio, error) {
	v.Error = err.Error()
	p.record(feed, guid, v)
	return Audio{}, err
}

// failedWithAudio is a stage that did not finish after the episode was in hand: the
// listener gets the publisher's own bytes, out of memory, and only the verdict is
// written, so the next play does the work again.
func (p *Producer) failedWithAudio(feed, guid string, raw []byte, v verdict, err error) (Audio, error) {
	log.Printf("episode=%s stage failed, serving the publisher's own audio: %v",
		store.Key(feed, guid), err)
	v.Error = err.Error()
	p.record(feed, guid, v)
	return Audio{Bytes: raw}, nil
}

func (p *Producer) record(feed, guid string, v verdict) {
	body, err := json.Marshal(v)
	if err != nil {
		return
	}
	_ = p.Store.Put(feed, guid, store.VerdictFile, body)
}
