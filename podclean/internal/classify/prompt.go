package classify

// prompt is what the model is told before it is shown the transcript.
//
// It is the only place the boundary rules are stated, and the model's obedience to them
// is what the one rule rests on: no second of programme is ever removed. Two things in it
// are not style and must not be tidied away.
//
// Cue numbers, never timestamps. A model that answers with a time where a cue number
// belongs names a cue that does not exist, and the segment is then thrown away whole --
// which is the safe outcome, but it is a whole break left in for a formatting slip. This
// is the mistake that has actually been seen.
//
// The quotes are copied, never paraphrased. A quote that is not in the transcript cannot
// place an edge, so the break is left in. A paraphrase costs a cut -- which is the right
// price, and the prompt says so plainly so that the model can weigh it.
//
// The German cue words are here because every episode this is measured on is German.
const prompt = `You are given the transcript of one podcast episode. Mark the advertising in it.

THE TRANSCRIPT
One line per cue: [cue] m:ss text. The number in the brackets is that cue's own number and
is the only way to refer to a place in the episode. The episode may be in German.

WHAT TO REPORT
Every run of the episode that is promotional, one segment per run, in one of five
categories:

  sponsor_read      A paid advertisement for somebody who is not this show: a product, a
                    service, a discount code. "brought to you by", "presented by",
                    "Werbung", "Anzeige", "präsentiert von".
  host_endorsement  A paid sponsor recommended by the host in their own voice, with no
                    announcer around it.
  cross_promo       An advertisement or a trailer for a different show, whoever makes it.
  self_promo        This show talking about itself: its own live dates and tickets, its
                    own membership, its own other podcast, its own outro, a plug for its
                    next episode. If it is this show's own, it is self_promo even when it
                    is worded exactly like an advertisement. A live-date plug is
                    self_promo, never sponsor_read.
  credits           The closing credits: who produced it, who edited it, the music.

Stacked advertisements are separate segments, one per advertiser. The hand-off into a
break and the return out of it -- "und jetzt zurück zur Sendung", "and now back to the
show" -- belong to the break, not to the programme.

THE BOUNDARIES
Tight. A segment runs from the first cue of the break to its last cue. Never from the
host's lead-in to it, and never on into the programme after it.

first_words and last_words are the break's own first and last words, three to six of them,
copied out of the transcript exactly as they are written there. Never paraphrase them,
never correct their spelling, never translate them. Where one cue holds the end of the
host's sentence and the beginning of a break, quote only the break's own words. A quote
that is not in the transcript makes the break impossible to place, so it is left in the
episode: a paraphrase costs a cut.

confidence is your honest estimate, between 0 and 1, that the run really is promotional
AND that these boundaries are right. Lower it if you are unsure of either one.

THE CHAPTERS
Also mark 4 to 20 chapters over the editorial content of the episode. Title each one in
the language the episode is in, under 60 characters. Never put a chapter mark inside a run
you have reported as promotional.

THE ANSWER
Answer with the JSON object only. No prose around it, no code fence.

{"segments": [{"start_cue": int, "end_cue": int, "category": string,
               "confidence": number between 0 and 1, "reason": string,
               "first_words": string, "last_words": string}],
 "chapters": [{"cue": int, "title": string}]}

start_cue and end_cue are cue numbers, taken out of the brackets. Never a timestamp:
"start_cue": 742 means cue 742. A segment naming a cue number the transcript does not have
is thrown away whole.`
