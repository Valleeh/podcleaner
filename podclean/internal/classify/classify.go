// Package classify is the words this server says to a model about an episode, and the
// one answer it takes away.
//
// It knows nothing about HTTP and nothing about audio: it is handed a rendered transcript
// and something that can complete, and it returns one reading of the episode. What it
// owns is the composition -- which models are asked, in what order, and what each is told.
package classify

import (
	"fmt"
	"strings"

	"podclean/internal/plan"
)

// A Completer is one model, asked one question.
type Completer interface {
	Complete(model, system, user string) (string, error)
}

// A Mark is one of the publisher's own chapter marks, handed to the model as a hint.
type Mark struct {
	At    float64
	Title string
}

// A Task is one episode's classification, configured by PODCLEANER_LLM_SPEC.
type Task struct {
	Spec      string
	Completer Completer
}

// Run reads the episode: what is advertising in it, and where its chapters are.
func (t Task) Run(publisher []Mark, rendered string) (plan.Reply, error) {
	screens, verifier := models(t.Spec)

	hint := publisherHint(publisher)
	if found := t.screen(screens, hint, rendered); found != "" {
		hint = join(hint, found)
	}
	content, err := t.Completer.Complete(verifier, prompt, join(hint, rendered))
	if err != nil {
		return plan.Reply{}, err
	}
	return plan.ParseReply(content)
}

// screen runs the first pass and lists what it found for the verifier.
//
// A screening model that fails or answers rubbish is dropped in silence. It can only ever
// have added candidates to look at, so losing one costs a break that might have been
// found and nothing else -- where letting it fail the episode would cost the cuts the
// verifier was going to make anyway.
func (t Task) screen(screens []string, hint, rendered string) string {
	var lines []string
	for _, model := range screens {
		content, err := t.Completer.Complete(model, prompt, join(hint, rendered))
		if err != nil {
			continue
		}
		reply, err := plan.ParseReply(content)
		if err != nil {
			continue
		}
		for _, s := range reply.Segments {
			lines = append(lines, fmt.Sprintf("cues %d-%d (%s): %s",
				s.StartCue, s.EndCue, s.Category, s.Reason))
		}
	}
	if len(lines) == 0 {
		return ""
	}
	return "A first pass reported these; verify each and add any it missed:\n" +
		strings.Join(lines, "\n")
}

// models reads the spec: cascade:screen[+screen]>verifier, or one model id on its own.
//
// Only the verifier's answer is ever used. The screens exist to put candidates in front
// of it, because a cheap model reading the whole transcript and a better one checking
// what it found costs less than the better one reading the whole transcript twice.
func models(spec string) (screens []string, verifier string) {
	rest, isCascade := strings.CutPrefix(strings.TrimSpace(spec), "cascade:")
	if !isCascade {
		return nil, rest
	}
	before, after, hasVerifier := strings.Cut(rest, ">")
	if !hasVerifier {
		return nil, strings.TrimSpace(before)
	}
	for _, model := range strings.Split(before, "+") {
		if model = strings.TrimSpace(model); model != "" {
			screens = append(screens, model)
		}
	}
	return screens, strings.TrimSpace(after)
}

// publisherHint is the publisher's own marks, where the feed named any, as a starting
// point rather than an answer: they are timed against audio this server is about to make
// shorter, and they are often wrong about where a break is.
func publisherHint(marks []Mark) string {
	if len(marks) == 0 {
		return ""
	}
	lines := make([]string, 0, len(marks)+1)
	lines = append(lines, "The publisher's own chapter marks for this episode, as a "+
		"starting point. They may be wrong and they do not say where the advertising is:")
	for _, m := range marks {
		lines = append(lines, fmt.Sprintf("%d:%02d %s", int(m.At)/60, int(m.At)%60, m.Title))
	}
	return strings.Join(lines, "\n")
}

// join puts a blank line between the hint and what follows it, and nothing at all when
// there is no hint.
func join(hint, rest string) string {
	if hint == "" {
		return rest
	}
	return hint + "\n\n" + rest
}
