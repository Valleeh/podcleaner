package episode

import (
	"encoding/json"
	"fmt"

	"podclean/internal/classify"
	"podclean/internal/mp3"
	"podclean/internal/plan"
	"podclean/internal/store"
	"podclean/internal/timeline"
	"podclean/internal/transcript"
)

// A result is everything one run of the pipeline found, as far as it got. Each stage
// fills its field; a field still empty is a stage that was never reached.
type result struct {
	raw   []byte    // what the publisher sent
	file  *mp3.File // set once those bytes proved to be audio
	text  *transcript.Transcript
	reply classify.Reply
	plan  plan.Plan

	served   []byte             // the audio the listener gets
	chapters []timeline.Chapter // on the served timeline
	vtt      string

	err error // the stage that did not finish
}

// pipeline runs the stages for one episode, in order, and stops at the first one that
// ends it. It writes nothing; save does.
func (o *Orchestrator) pipeline(source store.Source) (r result) {
	if r.raw, r.err = o.fetch(source.URL); r.err != nil {
		return r
	}
	if r.file, r.err = checkAudio(source.URL, r.raw); r.err != nil {
		return r
	}
	if r.text, r.err = o.transcribe(r.file, source.Language); r.err != nil {
		return r
	}
	if r.reply, r.err = o.findAds(source, r.text); r.err != nil {
		return r
	}
	r.plan = plan.Build(r.text, r.reply.Segments, r.file.Seconds())
	r.served, r.chapters, r.vtt = render(r)
	return r
}

// leastMaster is how small a plain client's copy may be next to the podcatcher's and
// still be taken for the master. The same line plan draws at a fifth of an episode: no
// real show is that much advertising, so a copy smaller still is something else -- a
// trailer, a preview -- and would be served for good.
const leastMaster = 0.8

// fetch is the episode's audio: the publisher's master where it serves one, else the
// copy a podcatcher gets.
//
// A publisher that stitches spots in for podcatchers serves its master to a plain client,
// and the stitched copy reuses the master's frames byte for byte: one 3.8 h episode's
// master was its stitched copy without exactly its three German spots, 114.8 s. What is
// not fetched need not be found -- a spot in another language was the hardest thing
// there was to find. A plain copy is taken only when it is audio, no larger than the
// podcatcher's (by HEAD, without fetching it) and not much smaller; a publisher that
// serves everyone the same bytes passes, and nothing changes for it.
func (o *Orchestrator) fetch(url string) ([]byte, error) {
	if master, err := o.Outside.Master(url); err == nil {
		size, stitched := float64(len(master)), float64(o.Outside.AudioLength(url))
		if stitched > 0 && size <= stitched && size >= leastMaster*stitched {
			if _, err := mp3.Parse(master); err == nil {
				return master, nil
			}
		}
	}
	return o.Outside.Audio(url)
}

// checkAudio is the question the cutter asks, asked first: do these bytes parse as MP3
// frames? What is published is audio the publisher sent, not whatever their server
// answered with. A block page carrying an episode's content type and its own length is a
// failure however it describes itself.
func checkAudio(url string, raw []byte) (*mp3.File, error) {
	file, err := mp3.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("%s did not answer with audio: %v", url, err)
	}
	return file, nil
}

// shortestHole is the shortest stretch with no words in it that is sent to the
// transcriber again. The transcriber now and then answers a stretch of speech with
// nothing: the jingle and first words of nearly every break in one episode, 12 to 27 s
// each. Sent again on its own, each came back. A pause between sentences is a second or
// two; a music bed that really is empty costs one short request.
const shortestHole = 10.0

// transcribe sends the episode up in pieces and puts the answers back on one timeline,
// then sends every hole in that timeline up once more.
//
// A hole that fails again stays a hole, as it was before it was asked about: the first
// pass is already a transcript, and a word that is missing only leaves advertising in.
func (o *Orchestrator) transcribe(file *mp3.File, language string) (*transcript.Transcript, error) {
	var parsed []transcript.Piece
	for _, piece := range file.Pieces(o.MaxBytes) {
		answer, err := o.hear(piece, language)
		if err != nil {
			return nil, err
		}
		parsed = append(parsed, answer)
	}
	for _, hole := range transcript.Join(parsed, file.Seconds()).Holes(shortestHole, file.Seconds()) {
		for _, piece := range file.Within(hole, o.MaxBytes) {
			if answer, err := o.hear(piece, language); err == nil {
				parsed = append(parsed, answer.Filling(hole))
			}
		}
	}
	return transcript.Join(parsed, file.Seconds()), nil
}

func (o *Orchestrator) hear(piece mp3.Piece, language string) (transcript.Piece, error) {
	body, err := o.Outside.Transcribe(piece.Data, language)
	if err != nil {
		return transcript.Piece{}, err
	}
	return transcript.Parse(body, piece.Start)
}

// findAds asks the models what is advertising, with the publisher's own chapter marks as
// a hint where the feed named any.
func (o *Orchestrator) findAds(source store.Source, text *transcript.Transcript) (classify.Reply, error) {
	return classify.Task{Spec: o.Spec, Completer: o.Outside}.Run(
		o.Outside.PublisherChapters(source.ChaptersURL), text.Render())
}

// render is what the listener gets from a decision: the audio, cut only when the plan
// says cut and then with the chapters written into it too, and the chapter marks and
// transcript moved onto the served timeline. Clean and refused serve the publisher's
// bytes as they came, with both sidecars.
func render(r result) (audio []byte, chapters []timeline.Chapter, vtt string) {
	line := r.plan.Timeline()
	chapters = line.Chapters(plan.Chapters(r.text, r.reply.Chapters))
	audio = r.raw
	if r.plan.State == "cut" {
		audio = append(mp3.ChapterTag(chapters, r.file.Seconds()-line.Total()), r.file.Cut(line)...)
	}
	return audio, chapters, r.text.VTT(line)
}

// chaptersJSON is the chapters sidecar, in the podcast namespace's own format. Seconds,
// not milliseconds.
func chaptersJSON(chapters []timeline.Chapter) []byte {
	type entry struct {
		StartTime float64 `json:"startTime"`
		Title     string  `json:"title"`
	}
	doc := struct {
		Version  string  `json:"version"`
		Chapters []entry `json:"chapters"`
	}{Version: "1.2.0", Chapters: make([]entry, 0, len(chapters))}
	for _, c := range chapters {
		doc.Chapters = append(doc.Chapters, entry{StartTime: round(c.At), Title: c.Title})
	}
	body, _ := json.Marshal(doc) // floats and strings: it cannot fail
	return body
}
