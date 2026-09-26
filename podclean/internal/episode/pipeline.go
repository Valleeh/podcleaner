package episode

import (
	"fmt"

	"podclean/internal/classify"
	"podclean/internal/mp3"
	"podclean/internal/plan"
	"podclean/internal/store"
	"podclean/internal/timeline"
	"podclean/internal/transcript"
)

// longestEpisode is the longest episode this server will examine. Past it, the
// publisher's audio is served untouched, unexamined and unbilled: transcribing something
// that long costs real money, and a file that size is more likely a concatenated archive
// or a live stream dump than an episode with advertising breaks in it.
const longestEpisode = 8400

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

	untouched bool  // too long to examine, served as it came
	err       error // the stage that did not finish
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
	if r.untouched = r.file.Seconds() > longestEpisode; r.untouched {
		r.served = r.raw
		return r
	}
	if r.text, r.err = o.transcribe(r.file); r.err != nil {
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

// transcribe sends the episode up in pieces and puts the answers back on one timeline.
func (o *Orchestrator) transcribe(file *mp3.File) (*transcript.Transcript, error) {
	var parsed []transcript.Piece
	for _, piece := range file.Pieces(o.MaxBytes) {
		body, err := o.Outside.Transcribe(piece.Data)
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
