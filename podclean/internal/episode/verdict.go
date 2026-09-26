package episode

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"strings"

	"podclean/internal/classify"
	"podclean/internal/timeline"
)

// A verdict is the record of what was decided about one episode, and the only thing
// written for an episode whose production did not finish.
//
// Its state is one of five: cut, clean (nothing was proposed worth acting on), refused
// (something was proposed that could not be trusted), untouched (never examined), failed
// (a stage did not finish). Only failed is retried on the next play -- every other state
// is a decision, and a decision is not revisited, so an improvement made later does not
// reach an episode already produced.
//
// Three of these names are read by programs outside this one: `tools/health.py` reads
// state, and `tools/verify.py` reads removed, removed_seconds and source_sha256. Renaming
// one of those breaks a tool rather than the server, which is the harder failure to see.
type verdict struct {
	Schema          string       `json:"schema"`
	State           string       `json:"state"`
	Error           string       `json:"error"`
	ModelSpec       string       `json:"model_spec"`
	Proposed        []proposal   `json:"proposed"`
	ProposedCount   int          `json:"proposed_count"`
	Cues            int          `json:"cues"`
	DurationSeconds float64      `json:"duration_seconds"`
	RemovedSeconds  float64      `json:"removed_seconds"`
	Removed         [][2]float64 `json:"removed"`
	Chapters        [][2]any     `json:"chapters"` // [at, title], so the times read as a column

	// SourceSHA256 is over the bytes that were fetched, bare lowercase hex. It is how
	// `./run verify` notices that the publisher has re-stitched the episode since it was
	// cut, and refuses to report a number computed against different bytes.
	SourceSHA256 string `json:"source_sha256"`
}

// verdictOf is the record of one run, read off the result once it is over. A field is
// filled only if the stage that produces it was reached: a verdict written before the
// model was asked says null for what the model would have said, not an empty list.
func verdictOf(spec string, r result) verdict {
	v := verdict{Schema: "podclean.verdict/1", State: "failed", ModelSpec: spec}
	if r.raw != nil {
		sum := sha256.Sum256(r.raw)
		v.SourceSHA256 = hex.EncodeToString(sum[:])
	}
	if r.file != nil {
		v.DurationSeconds = round(r.file.Seconds())
	}
	if r.text != nil {
		v.Cues = len(r.text.Cues)
	}
	switch {
	case r.err != nil:
		v.Error = r.err.Error()
		return v
	case r.untouched:
		v.State = "untouched"
		v.Error = fmt.Sprintf("%.0f s is longer than the %d s this server will examine",
			r.file.Seconds(), longestEpisode)
		return v
	}
	v.Proposed = proposed(r.reply.Segments)
	v.ProposedCount = len(r.reply.Segments)
	v.State = r.plan.State
	// Why the plan removed less than the model asked for, in one line. A refusal that
	// leaves no trace is a refusal nobody can improve on.
	v.Error = strings.Join(r.plan.Refusals, "; ")
	line := r.plan.Timeline()
	v.Removed = spans(line.Removed)
	v.RemovedSeconds = round(line.Total())
	v.Chapters = marks(r.chapters)
	return v
}

// A proposal is one break as the model reported it, kept whether or not it was cut: when
// a cut goes wrong in production, this is the only record of what was asked for.
type proposal struct {
	StartCue   int     `json:"start_cue"`
	EndCue     int     `json:"end_cue"`
	Category   string  `json:"category"`
	Confidence float64 `json:"confidence"`
}

func proposed(segments []classify.Segment) []proposal {
	out := make([]proposal, 0, len(segments))
	for _, s := range segments {
		out = append(out, proposal{StartCue: s.StartCue, EndCue: s.EndCue,
			Category: s.Category, Confidence: s.Confidence})
	}
	return out
}

func spans(in []timeline.Span) [][2]float64 {
	out := make([][2]float64, 0, len(in))
	for _, s := range in {
		out = append(out, [2]float64{round(s.Start), round(s.End)})
	}
	return out
}

func marks(chapters []timeline.Chapter) [][2]any {
	out := make([][2]any, 0, len(chapters))
	for _, c := range chapters {
		out = append(out, [2]any{round(c.At), c.Title})
	}
	return out
}

func round(seconds float64) float64 { return math.Round(seconds*1000) / 1000 }

// chaptersJSON is the podcast namespace's own format. Seconds, not milliseconds.
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
