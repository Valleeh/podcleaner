# What must hold

The requirements. Each is one thing a black-box test can assert, numbered so that a
commit, a review or a test can name one.

Three documents, three jobs. `docs/spec.md` is the prose: what the product is to a
listener, with the numbers it was measured at. This one is the same promises as
assertions, **with every number taken out**, so that a constant moving cannot make a line
here false -- and with the test that asserts it named beside it. `docs/contract.md` is the
rest: what a re-implementation must match and no listener can see.

Nothing below names a module, a function, a constant or a category. Everything below is
checkable from outside, through the URLs, the audio bytes, or a comparison against what
the publisher serves.

Where a line here and the spec disagree, one of them is wrong and one commit fixes both.

*Asserted by* names a test in `tests/integration/`; run them all with `./run test`.
*Not asserted* means exactly that, and is a standing invitation, not a plan: see the end.

## The one rule

**R1. No second of programme is ever removed.** Everything else gives way to this. A
missed advertisement is an annoyance; a sentence that disappears cannot be recovered, and
the listener cannot tell it is gone.
*Asserted by* `./run verify` against the publisher's own ad-free master -- the suite plays
an episode of silence and can only check the arithmetic. It follows that where it is not
certain that a stretch is advertising, it stays, and that a refusal is never worse for the
listener than doing nothing.

## Subscribing

**R2.** The feed returned is the publisher's document with links changed and nothing else:
put the original links back, drop the ones this server added, and the two documents are
identical.
*Asserted by* `test_the_feed_is_the_publishers_with_only_the_links_changed`.

**R3.** Every episode this server can address has its audio link pointing here.
*Asserted by* `test_the_feed_is_the_publishers_with_only_the_links_changed`.

**R4.** Only links this server can actually serve are repointed or added. A feed that does
not declare the namespace a sidecar link would be written in keeps exactly what it had.
*Asserted by* `test_a_feed_without_the_podcast_namespace_keeps_exactly_what_it_had`.

**R5.** An episode this server cannot address unambiguously keeps the publisher's own
link, and the episodes in the same feed that it can address are repointed as usual.
*Asserted by* `test_two_episodes_sharing_an_identifier_keep_the_publishers_link`.

**R6.** An episode is named as the publisher named it. Document syntax around the name
belongs to the document, not to the name.
*Asserted by* `test_a_guid_wrapped_in_cdata_names_the_episode_the_publisher_named`.

**R7.** Reading a feed is the only thing that makes its episodes playable here.
*Asserted by* `test_an_episode_no_feed_has_named_is_refused_and_nothing_goes_out`.

**R8.** A feed with no addressable episode in it at all is answered as an error, not
served back unchanged.
*Not asserted.*

## Playing

**R9.** The audio returned is the publisher's own audio with whole advertising breaks
missing and nothing else altered.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`; that it is the
publisher's own bytes, by `./run verify`.

**R10.** The file decodes to the length it started at, minus exactly what was removed.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`, with a decoder that
shares no code with the server.

**R11.** Every cut begins and ends inside advertising. Some of each break that was cut
stays audible at both of its edges.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` as arithmetic -- what is
removed is each break less a margin at each end -- and as a fact about audio by
`./run verify`.

**R12.** An episode no fetched feed has ever named is refused, and no request goes out to
anyone on its behalf.
*Asserted by* `test_an_episode_no_feed_has_named_is_refused_and_nothing_goes_out`.

**R13.** When the publisher fails, that is reported as a failure, nothing is kept, and the
next play tries the publisher again rather than serving or remembering the failure.
*Asserted by* `test_a_publisher_that_fails_is_reported_and_nothing_is_kept`.

**R14.** The connection is held until the audio is whole. There is no state in which a
listener receives half an episode.
*Not asserted.*

**R15.** The question a podcatcher asks before it downloads is answered the way the
download itself would be: the same status, the size it is about to get, and no body.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**R16.** A request for part of the file is answered with that part and with the range it
covers, and every reply says that ranges may be asked for.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**R17.** Audio is served as audio: no character encoding is claimed for bytes that are not
characters.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` and
`test_a_reply_the_model_mangles_leaves_the_episode_whole`.

**R39.** What is published is audio the publisher sent, not whatever their server
answered with. A reply that is not audio is a failure however it describes itself, nothing
is paid for on its behalf, and nothing is kept.
*Asserted by* `test_a_publisher_that_answers_with_something_that_is_not_audio_is_refused`.

## Playing again

**R18.** The same request returns the same bytes, for as long as the episode exists here.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**R19.** Nothing outside is asked a second time, and nothing is paid for twice. One
episode costs one pass over it.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode`.

**R20.** Simultaneous first plays of one episode pay for one pass and get the same answer.
*Asserted by* `test_simultaneous_first_plays_only_pay_once`.

**R21.** A verdict, once reached, is not revisited -- across a restart of the server as
well. The audio and both documents come back unchanged and nobody outside is asked again.
*Asserted by* `test_untouched_audio_keeps_its_documents_and_is_not_reexamined`.

**R22.** Work that did not finish is not remembered as a verdict: the next play does it
again.
*Asserted by* `test_a_reply_the_model_mangles_leaves_the_episode_whole` and
`test_a_publisher_that_fails_is_reported_and_nothing_is_kept`.

## Refusing

**R23.** When the plan cannot be trusted, the publisher's own audio is served whole rather
than a cut one. Serving it untouched is always available as an answer, and is the answer
whenever the alternative is a cut that might take programme.
*Asserted by* every test in `test_refusing_to_cut.py`.

**R24.** A refusal is visible in what is served, not only in a log: the listener gets the
publisher's bytes, and no document claims a cut was made.
*Asserted by* `test_a_reply_the_model_mangles_leaves_the_episode_whole`.

**R25.** A break the model is not confident about is left in.
*Asserted by* `test_a_break_the_model_is_only_guessing_at_is_left_in`.

**R26.** A break whose edges cannot be placed on words the transcript actually holds is
left in.
*Asserted by* `test_a_break_whose_words_are_not_in_the_transcript_is_left_in`.

**R27.** A break naming a cue the transcript never had is left in, and its quoted edges
are not looked for anywhere else in the episode.
*Asserted by* `test_a_break_named_by_a_cue_the_transcript_never_had_is_left_in`.

**R28.** A plan that is not believable as a whole leaves the whole episode untouched: one
break implausibly long, or breaks adding up to an implausible share of the episode.
*Asserted by* `test_a_break_that_would_swallow_most_of_the_episode_is_not_cut` for the
share. The single implausibly long break is *not asserted*.

**R29.** A reply this server cannot read is not a verdict about the audio: the episode is
served whole, and no documents are published for it.
*Asserted by* `test_a_reply_the_model_mangles_leaves_the_episode_whole`.

**R30.** An episode too long to examine is served untouched, and nothing is asked about
it.
*Not asserted.*

## The chapter marks and the transcript

**R31.** Neither exists before the audio does, and asking for one never causes the audio
to be made or anything to be paid for.
*Asserted by* `test_the_sidecars_say_nothing_until_the_episode_has_been_played`.

**R32.** They describe the audio that is served, never the audio the publisher sent: every
time in them is on the served timeline.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` and
`test_an_episode_too_big_for_one_transcription_request_is_cut_all_the_same`.

**R33.** Anything whose audio was removed is absent from them rather than moved to the
join, and neither ever claims words that are not in the file that was served.
*Asserted by* `test_a_listener_subscribes_and_plays_one_episode` for the transcript. That
a chapter mark inside a removed break is dropped is *not asserted*.

**R34.** An episode served untouched keeps documents that describe all of it.
*Asserted by* `test_untouched_audio_keeps_its_documents_and_is_not_reexamined`.

**R35.** A cut episode carries the same marks inside the audio file itself, in the form a
podcatcher reads from an MP3, so that the two cannot disagree.
*Not asserted.*

## Throughout

**R36.** Where this server cannot improve on the publisher, it gets out of the way and
serves what the publisher sent.
*Asserted by* every test in `test_refusing_to_cut.py`.

**R37.** Nothing is served that the listener could not have got from the publisher, except
the sidecars this server adds.
*Asserted by* `test_the_feed_is_the_publishers_with_only_the_links_changed` and
`test_a_listener_subscribes_and_plays_one_episode`.

**R38.** A route a crawler can reach never starts paid work.
*Asserted by* every test in `test_the_routes.py`.

## What has no test yet

R8, R14, R30, R35, the long-break half of R28 and the dropped-mark half of R33. They are
listed so that the gap is visible, not as a backlog: a test is written when it would have
caught a bug, and none of these has bitten yet. R1 and the audio half of R11 are not the
suite's to prove at all -- the suite plays silence, and only `./run verify`, against the
publisher's own ad-free master, can say that what was removed was advertising.
