# What must hold

Each line is one thing a black-box test can assert, numbered so that a commit, a review or
a test can name it, with every number taken out so that a constant moving cannot make a
line false. Nothing here names a module, a function, a constant or a category; everything
is checkable through the URLs, the audio bytes, or a comparison against what the publisher
serves. `README.md` is the same promises as prose, with the numbers in.

*Asserted by* names a test in `tests/integration/`; `./run test` runs them all. *Not
asserted* means exactly that: a test is written when it would have caught a bug.

## The one rule

**1.** No second of programme is ever removed. Everything else gives way to this: where it
is not certain that a stretch is advertising, it stays, and a refusal is never worse for
the listener than doing nothing.
*Asserted by* `./run verify` against the publisher's own ad-free master. The suite plays
an episode of silence and can only check the arithmetic.

## Subscribing

**2.** The feed returned is the publisher's document with links changed and nothing else:
put the original links back, drop the ones this server added, and the two are identical.
*Asserted by* `test_the_feed_is_the_publishers_with_only_the_links_changed`.

**3.** Every episode this server can address has its audio link pointing here, and links
to its two documents here; a chapter link the publisher wrote is replaced by this
server's, not joined by it.
*Asserted by* `test_the_feed_is_the_publishers_with_only_the_links_changed`, and for
"every" by `test_two_episodes_sharing_an_identifier_keep_the_publishers_link`.

**4.** A feed that does not declare the namespace a document link would be written in
gets no document links; its audio links are repointed as usual.
*Asserted by* `test_a_feed_without_the_podcast_namespace_keeps_exactly_what_it_had`.

**5.** An episode this server cannot address unambiguously keeps the publisher's own
link.
*Asserted by* `test_two_episodes_sharing_an_identifier_keep_the_publishers_link`.

**6.** An episode is named as the publisher named it; document syntax around the name is
not part of it.
*Asserted by* `test_a_guid_wrapped_in_cdata_names_the_episode_the_publisher_named`.

**7.** An episode no fetched feed has ever named is refused, and no request goes out to
anyone on its behalf: reading a feed is the only thing that makes its episodes playable
here.
*Asserted by* `test_an_episode_no_feed_has_named_is_refused_and_nothing_goes_out`.

**8.** A feed with no addressable episode in it is answered as an error, not served back
unchanged.
*Not asserted.*

## Playing

**9.** The audio returned is the publisher's own with the advertising breaks missing, but
for a moment at each edge, and nothing else altered.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`; that the bytes are the
publisher's, by `./run verify`.

**10.** The file decodes to the length it started at, minus exactly what was removed.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`, with a decoder that
shares no code with the server.

**11.** Every cut begins and ends inside advertising, and some of each break stays
audible at both of its edges.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` as arithmetic for how
much of each break goes — a margin after the break's first word to a margin before its
last cue ends. That it goes from both edges, and that both land in advertising, only by
`./run verify`.

**12.** A cut never reaches audio no cue describes: it ends where the cue's own
description ends, not where words stamped past that end do.
*Asserted by*
`test_a_cut_in_the_middle_ends_where_its_cue_does_and_not_where_its_words_were_parked`.

**13.** A break at the very end of the episode is cut to its last word, even when the
transcriber's description of the last cue stops before it.
*Asserted by* `test_a_break_at_the_end_is_cut_when_the_transcriber_stops_before_its_words`.

**13a.** A stretch of speech the transcriber answered with nothing is asked about again,
of another transcriber, and a break inside it is cut as if it had never been skipped --
also when the next segment claims to begin inside that stretch.
*Asserted by* `test_a_stretch_the_transcriber_skipped_is_asked_for_again`.

**13b.** The transcriber is told the language the feed declares.
*Asserted by* `test_the_transcriber_is_told_the_language_the_feed_declares`.

**13c.** A break whose first words are said more than once inside it is cut from the
last of them -- unless one of them opens the cue the break is named from, which is then
where it starts.
*Asserted by* `test_a_quote_said_twice_in_its_break_is_cut_from_the_later_one` and
`test_a_spot_played_twice_is_cut_from_its_first_playing`.

**13e.** A break is never cut from a copy of its words the transcriber parked with no
duration.
*Asserted by* `test_a_quote_parked_again_with_no_duration_is_not_cut_from`.

**13d.** A quote is found however the transcriber split its words.
*Asserted by* `test_a_quote_the_transcriber_split_into_two_words_is_found`.

**13f.** A break ends where its own last words end, never after the last cue the model
named, however far the model counted; without its last words it is left in.
*Asserted by* `test_a_break_named_one_cue_too_far_ends_with_its_last_words` and
`test_a_break_whose_last_words_are_not_in_it_is_left_in`.

**13g.** Only audio frames are sent to the transcriber, never the bytes a splice left
between them.
*Asserted by* `test_only_audio_frames_are_sent_to_the_transcriber`.

**13h.** Where the publisher serves its master to a plain client, the episode is cut from
the master, and what it stitches in for podcatchers is never fetched; a plain copy that
cannot be the episode without its spots is not used.
*Asserted by* `test_the_publishers_master_is_cut_where_one_is_served` and
`test_a_master_that_is_not_the_episode_is_not_used`.

**14.** An episode too long to be transcribed in one request is cut all the same, and its
chapter marks land where they would have.
*Asserted by* `test_an_episode_too_big_for_one_transcription_request_is_cut_all_the_same`.

**15.** What is published is audio the publisher sent, not whatever their server answered
with. A reply that is not audio is a failure however it describes itself; nothing is paid
for on its behalf and nothing is kept.
*Asserted by* `test_a_publisher_that_answers_with_something_that_is_not_audio_is_refused`.

**16.** When the publisher fails, that is reported as a failure, nothing is kept, and the
next play tries the publisher again.
*Asserted by* `test_a_publisher_that_fails_is_reported_and_nothing_is_kept`.

**17.** The connection is held until the audio is whole; no listener ever receives half
an episode.
*Not asserted.*

**18.** The question a podcatcher asks before it downloads is answered the way the
download itself would be: the same status, the size it is about to get, and no body.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**19.** A request for part of the file is answered with that part, the range it covers,
and word that ranges may be asked for.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**20.** Audio is served as audio: no character encoding is claimed for bytes that are not
characters.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` and
`test_a_reply_the_model_mangles_leaves_the_episode_whole`.

## Playing again

**21.** The same request returns the same bytes, for as long as the episode exists here.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**22.** Nothing outside is asked a second time, and nothing is paid for twice: one
episode costs one pass over it -- one transcription, and the model reading it twice, both
readings cut.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` and
`test_the_model_reads_the_episode_twice_and_every_break_either_finds_is_cut`.

**23.** Simultaneous first plays of one episode pay for one pass and get the same answer.
*Asserted by* `test_simultaneous_first_plays_only_pay_once`.

**24.** A verdict, once reached, is not revisited — across a restart of the server as
well. The audio and both documents come back unchanged and nobody outside is asked again.
*Asserted by* `test_untouched_audio_keeps_its_documents_and_is_not_reexamined` for a
verdict that left the audio whole, and within one run of the server by
`test_a_listener_subscribes_and_plays_one_episode` for a cut; a cut across a restart is
*not asserted*.

**25.** Work that did not finish is not remembered as a verdict: the next play does it
again.
*Asserted by* `test_a_reply_the_model_mangles_leaves_the_episode_whole` and
`test_a_publisher_that_fails_is_reported_and_nothing_is_kept`.

## Refusing

**26.** When the plan cannot be trusted, the publisher's own audio is served whole.
Serving it untouched is always available as an answer, and is the answer whenever the
alternative is a cut that might take programme.
*Asserted by* every test in `test_refusing_to_cut.py`.

**27.** A refusal is visible in what is served, not only in a log: the listener gets the
publisher's bytes, and no document claims a cut was made.
*Asserted by* `test_untouched_audio_keeps_its_documents_and_is_not_reexamined` for a plan
that was refused, and `test_a_reply_the_model_mangles_leaves_the_episode_whole` for a
reply that could not be read.

**28.** A break the model is not confident about is left in.
*Asserted by* `test_a_break_the_model_is_only_guessing_at_is_left_in`.

**29.** A break whose first words are not in the transcript is left in.
*Asserted by* `test_a_break_whose_words_are_not_in_the_transcript_is_left_in`.

**30.** A break naming a cue the transcript never had is left in whole, rather than
narrowed to the cues there are.
*Asserted by* `test_a_break_named_by_a_cue_the_transcript_never_had_is_left_in`.

**31.** A plan that is not believable as a whole leaves the whole episode untouched: one
break implausibly long, or breaks adding up to an implausible share of the episode.
*Asserted by* `test_a_break_that_would_swallow_most_of_the_episode_is_not_cut`, whose one
break is both longer than any believable break and most of the episode — so it proves
that one of the two rules exists, and neither on its own.

**32.** A reply this server cannot read is asked for once more; read twice and still
unreadable, it is not a verdict about the audio: the episode is served whole and no
documents are published for it.
*Asserted by* `test_a_reply_mangled_once_is_asked_for_again` and
`test_a_reply_the_model_mangles_leaves_the_episode_whole`.

**33.** An episode is examined and cut however long it runs.
*Asserted by* `test_an_episode_hours_long_is_cut_all_the_same`.

## The chapter marks and the transcript

**34.** Neither exists before the audio does, and asking for one never causes the audio
to be made or anything to be paid for.
*Asserted by* `test_the_sidecars_say_nothing_until_the_episode_has_been_played`.

**35.** They describe the audio that is served, never the audio the publisher sent: every
time in them is on the served timeline.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` and
`test_an_episode_too_big_for_one_transcription_request_is_cut_all_the_same` for the
chapter marks; the transcript's times are *not asserted*.

**36.** Anything whose audio was removed is absent from them rather than moved to the
join, and neither ever claims words that are not in the file that was served.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` for the transcript; that
a chapter mark inside a cut is dropped is *not asserted*.

**37.** An episode examined and served whole has documents that describe all of it.
*Asserted by* `test_untouched_audio_keeps_its_documents_and_is_not_reexamined`.

**38.** A cut episode carries the same marks inside the audio file itself, in the form a
podcatcher reads from an MP3, so that the two cannot disagree.
*Not asserted.*

## Throughout

**39.** This server talks to the publisher, the transcriber and the model, and to nobody
else.
*Asserted by* every test, through the check the outside stub makes as it closes.

**40.** Nothing but the first play of an episode a feed has named starts paid work.
*Asserted by* every test in `test_the_routes.py`.

## What has no test yet

8, 17, 33 and 38; each half of 31 on its own; the dropped-mark half of 36; the
transcript's times in 35; a cut across a restart in 24. Listed so that the gap is visible,
not as a backlog. 1, the bytes half of 9 and the edges of 11 are not the suite's to prove:
the suite plays silence, and only `./run verify` can say that what was removed was
advertising.
