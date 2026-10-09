package classify

// prompt is what the model is told before it is shown the transcript.
//
// It is the only place the boundary rules are stated, and the model's obedience to them
// is what the one rule rests on: no second of programme is ever removed. It was measured
// against a hand-built gold standard over four episodes of four podcasts in two languages
// and rewritten from what that showed: the model finds the advertising, and what wasted
// its answers was the quote -- one or two words, which cannot place a cut. Stating the
// count as a mechanical rule, with the consequence and a worked example, took the
// episodes that would actually have been cut from five of nine to eight of nine, with no
// new false positive.
//
// Four things in here are load-bearing and must not be tidied away.
//
// Cue numbers and never timestamps. A model that answers with a time where a cue number
// belongs names a cue that does not exist, and the segment is thrown away whole -- the
// safe outcome, but a whole break left in for a formatting slip.
//
// The quote, copied and counted, with the consequence named and the one-word example.
// Written as a preference it is ignored: thirteen of eighteen quotes came back outside
// the window.
//
// The post-roll paragraph. Advertising after the goodbye was the single largest class of
// miss: two of four episodes carried one, and neither was reported before this said so.
//
// The German cue words, because every episode this is measured on is German.
//
// Nothing about a spot played twice. "A spot played twice is two segments" was tried on
// 2026-10-10: the model began reporting every break twice, once whole from the hand-off
// and once per advertiser, and one whole one began at the end of the previous
// conversation -- 48.7 s of programme in 3 of 3 runs. Without the line: 0 s.
//
// Two rules that were tried and dropped, so that nobody adds them back: telling the model
// that end_cue must contain the words it quotes pushed it to quote repeated boilerplate,
// which is ambiguous and rejected (eight of nine down to seven). Telling it to pick words
// that occur only once changed nothing on the verifier and cost a policy violation on the
// screening model.
//
// last_words came back on 2026-10-09. Without them a cut ended wherever the model stopped
// counting cues, and on a 3.8 h transcript with nothing wrong in it that took seven and
// forty-five seconds of programme. They once caused every refusal there was -- taglines
// invented, a one-word URL, a German quote of a spot the transcriber had written in
// English -- and what changed is how they are matched: letters run together, a short
// quote enough to confirm the last cue, and German now transcribed as German.
const prompt = `You are given the transcript of one podcast episode. Mark the advertising in it.

THE TRANSCRIPT
One line per cue: [cue] m:ss text. The number in the brackets is that cue's own number and
is the only way to refer to a place in the episode. The episode may be in German.

A stretch in another language than the episode -- German in an English show -- is almost
always an advertisement the publisher inserted for the listener's region. Read it as one
unless it is plainly part of the conversation.

WHAT TO REPORT
Every run of the episode that is promotional, one segment per run, in one of five
categories:

  sponsor_read      A paid advertisement for somebody who is not this show: a product, a
                    service, a discount code. "brought to you by", "presented by",
                    "Werbung", "Anzeige", "präsentiert von". A spot for a shop, an event,
                    an employer or a local business is sponsor_read even when it is in
                    another language than the episode, has no announcer and names no
                    sponsor: the publisher inserts such spots by region.
  host_endorsement  A paid sponsor recommended by the host in their own voice, with no
                    announcer around it.
  cross_promo       An advertisement or a trailer for a show these hosts have no hand in.
  self_promo        This show talking about itself or about anything the same people make:
                    its own live dates and tickets, its own membership, its own app or
                    book, its own outro, a plug for its next episode -- and another podcast
                    they publish. "Subscribe to X, which we put out" is self_promo, not
                    cross_promo. If it is theirs, it is self_promo even when it is worded
                    exactly like an advertisement. A live-date plug is self_promo, never
                    sponsor_read. Only what the hosts themselves say is theirs is
                    self_promo; when you cannot tell, it is not.
  credits           The closing credits: who produced it, who edited it, the music.

Stacked advertisements are separate segments, one per advertiser. The hand-off into a
break and the return out of it -- "und jetzt zurück zur Sendung", "and now back to the
show" -- belong to the break, not to the programme.

WHERE ADVERTISING HIDES
Before the episode starts, in the seconds before the hosts first speak. In the middle,
after a hand-off. And after the end: advertising very often follows the goodbye -- after
"see you next time", after the credits, after the music. That run is still advertising and
still belongs in your answer. Read to the last cue; do not stop at the sign-off. A break
can be a single cue, or even part of one.

THE QUOTE
Every segment carries first_words: the first words of the break itself -- never the
host's, even when the host's last sentence and the break share a cue. They are matched
against the transcript mechanically and place the start of the cut, so they must be:

  * three to six words. One or two is thrown away.
  * copied exactly as the transcript writes them -- same spelling, same language, no
    paraphrase.
  * taken from between start_cue and end_cue inclusive.

Every segment also carries last_words: the last four to six words of the break itself,
never fewer and never the programme's, even when the programme's first sentence shares a
cue with the end of the break. Same rules as first_words, copied exactly. They place the
end of the cut, so a segment whose last_words are not in its cues is discarded whole.

A segment whose quote fails any of these is discarded whole and its advertising stays in
the episode.

  [412] 14:02 Werbung.
  [413] 14:03 Trading kann sich schnell anfühlen wie ein Spiel.

  first_words: "Werbung."                    discarded -- one word
  first_words: "Werbung. Trading kann sich"  correct -- reads on into the next cue to reach three

So when a break opens on a very short line, read on into the next cue until you have three
consecutive words.

BOUNDARIES
Tight. A segment runs from the first cue of the break to its last cue. Never from the
host's lead-in to it, and never on into the programme after it. The cut ends where end_cue
ends, so end_cue is the last cue that is still advertising -- not the one where the
programme resumes.

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
