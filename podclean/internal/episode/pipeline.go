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
	if r.raw, r.err = o.Outside.Audio(source.URL); r.err != nil {
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
// nothing: the jingle and first words of nearly every break, and ten minutes after a
// foreign-language spot, in one episode. Sent again on its own, each came back. A pause
// between sentences is a second or two; a music bed that really is empty costs one short
// request a round.
const shortestHole = 10.0

// holeRounds is how many times what is still missing is asked about. Sent again, the ten
// minutes after that spot came back as the spot and nothing else -- the same silence,
// now starting later -- and only a second round, starting after the spot, got the rest.
// A round that fills nothing ends the asking early.
const holeRounds = 3

// transcribe sends the episode up in pieces and puts the answers back on one timeline,
// then sends every hole in that timeline up again, round after round.
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
	for round := 0; round < holeRounds; round++ {
		filled := false
		for _, hole := range transcript.Join(parsed, file.Seconds()).Holes(shortestHole, file.Seconds()) {
			for _, piece := range file.Within(hole, o.MaxBytes) {
				if answer, err := o.hear(piece, language); err == nil {
					parsed = append(parsed, answer.Filling(hole))
					filled = true
				}
			}
		}
		if !filled {
			break
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
