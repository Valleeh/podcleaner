package classify

// prompt is what the model is told before it is shown the transcript.
//
// It is the only place the boundary rules are stated, and the model's obedience to them
// is what the one rule rests on: no second of programme is ever removed. It was measured
// on 2026-09-18 against a hand-built gold standard over four episodes of four different
// podcasts, in two languages, and rewritten from what that measurement showed.
//
// What it showed was not what anyone expected. The model finds the advertising: the
// 62-second Tradegate read that survived a real episode was reported at 0.95 confidence,
// and the Altra post-roll at 1.0. They survived because the quotes came back one and two
// words long, and a quote that short cannot place a cut edge, so the whole segment is
// thrown away. Thirteen of eighteen segments had a quote outside the three-to-six-word
// window. Making that rule mechanical, with the consequence spelled out and a worked
// example of the failure, took the episodes that would actually have been cut from five
// of nine to eight of nine, with no new false positive.
//
// Three things in here are load-bearing and must not be tidied away.
//
// Cue numbers and never timestamps. A model that answers with a time where a cue number
// belongs names a cue that does not exist, and the segment is thrown away whole -- the
// safe outcome, but a whole break left in for a formatting slip.
//
// The quotes, copied and counted. Everything above.
//
// The post-roll paragraph. Advertising after the goodbye was the single largest class of
// miss: two of the four episodes carried one, and neither was reported before this said
// so in as many words.
//
// The German cue words are here because every episode this is measured on is German.
//
// Two rules that were tried and dropped, so that nobody adds them back: telling the model
// that end_cue must contain the words it quotes pushed it to quote repeated boilerplate,
// which is ambiguous and rejected (eight of nine down to seven). Telling it to pick words
// that occur only once changed nothing on the verifier and cost a policy violation on the
// screening model.
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
  cross_promo       An advertisement or a trailer for a show these hosts have no hand in.
  self_promo        This show talking about itself or about anything the same people make:
                    its own live dates and tickets, its own membership, its own app or
                    book, its own outro, a plug for its next episode -- and another podcast
                    they publish. "Subscribe to X, which we put out" is self_promo, not
                    cross_promo. If it is theirs, it is self_promo even when it is worded
                    exactly like an advertisement. A live-date plug is self_promo, never
                    sponsor_read.
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

THE QUOTES -- read this part twice
Every segment carries first_words and last_words: the break's own first and last words.
They are what finds the break in the audio, and they are checked mechanically before
anything is cut.

  * BETWEEN THREE AND SIX WORDS. Count them before you write them. Two words is thrown
    away. Eight words is thrown away.
  * Copied from the transcript exactly as it is written there -- same spelling, same
    language, no tidying, no translating, no paraphrase.
  * Taken from between start_cue and end_cue inclusive. A quote from a cue outside the
    segment's own range is thrown away.

A segment whose quotes fail any of these is discarded whole and its advertising stays in
the episode. This is the most common way a correct answer is wasted.

  [412] 14:02 Werbung.
  [413] 14:03 Trading kann sich schnell anfühlen wie ein Spiel.
  [418] 15:04 Mehr Infos unter tradegate.direct

  first_words: "Werbung. Trading kann sich"        correct -- three to six words, and it
                                                   runs across the cue boundary to get there
  first_words: "Werbung."                          discarded -- one word
  last_words:  "Mehr Infos unter tradegate.direct" correct -- four words
  last_words:  "tradegate.direct"                  discarded -- one word

So when a break opens or closes on a very short line, keep reading into the line next to it
until you have three words. The words need not sit in one cue; they must be consecutive in
the transcript and inside the segment's own cue range.

Where one cue holds the end of the host's sentence and the beginning of a break, quote only
the break's own words.

BOUNDARIES
Tight. A segment runs from the first cue of the break to its last cue. Never from the
host's lead-in to it, and never on into the programme after it.

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
