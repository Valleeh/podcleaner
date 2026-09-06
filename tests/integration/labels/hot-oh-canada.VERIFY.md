# Verification checklist: Hacks On Tap - Oh Canada! (With Jonathan Martin)

File: hot-oh-canada.mp3  (sha256 55ea210e5c4adf03...)
Status: in_progress   Labeler: -
Provenance: text_annotation -- Cue-aligned to the whisper transcript. Two segments the annotator flagged ambiguous stay don't-care; music stings at ad edges were split out as don't-care so recall is measured on speech.

For every segment below, listen from about 5 s before the start to 5 s after it,
and from 5 s before the end to 5 s after it. Confirm that the boundary falls
between the ad and the programme and that the quoted lines are what you hear.
Then mark it: `python -m podcleaner.eval.labels verify --label <file> --labeler <you> --ad <n>`.
Constructed segments (source=construction) are exact splice points and need no
listening; the whole file needs one pass for advertising nobody has labelled yet.

 0. 0:00 -> 0:01  (1.6s)  other  [text] <ambiguous / don't-care>
      starts: "[MUSIC]"
      ends:   "[MUSIC]"
      note:   music sting before Ina Garten / Happy Hour with Ina
 1. 0:01 -> 0:23  (21.7s)  cross_promo  [text] 
      starts: "I'm Ina Garten."
      ends:   "new episodes every Wednesday starting September 16th."
      note:   Ina Garten / Happy Hour with Ina
 2. 0:23 -> 0:29  (6.0s)  other  [text] <ambiguous / don't-care>
      starts: "[MUSIC]"
      ends:   "[MUSIC]"
      note:   music sting after Ina Garten / Happy Hour with Ina
 3. 0:31 -> 0:54  (23.7s)  cross_promo  [text] 
      starts: "Megan Rapino here."
      ends:   "wherever you get your podcasts and on YouTube."
      note:   Megan Rapinoe / Why Are You Like This
 4. 0:54 -> 0:57  (3.2s)  other  [text] <ambiguous / don't-care>
      starts: "[MUSIC]"
      ends:   "[MUSIC]"
      note:   music sting after Megan Rapinoe / Why Are You Like This
 5. 0:57 -> 1:29  (31.2s)  cross_promo  [text] 
      starts: "Hi, it's Kara Swisher."
      ends:   "Find Pivot on YouTube or wherever you listen to podcasts."
      note:   Kara Swisher / Pivot
 6. 1:29 -> 1:39  (10.0s)  other  [text] <ambiguous / don't-care>
      starts: "[MUSIC]"
      ends:   "[MUSIC]"
      note:   music sting after Kara Swisher / Pivot
 7. 1:39 -> 2:04  (25.2s)  credits  [text] <ambiguous / don't-care>
      starts: "Hey, pull up a chair."
      ends:   "[MUSIC]"
      note:   opening announcement ('Hey, pull up a chair. It's Hacks on Tap with ... from the Vox Media Podcast Network') and theme music; the hosts' first line follows at cue 42
 8. 16:14 -> 17:51  (96.7s)  sponsor_read  [text] 
      starts: "Support for the show comes from Ground News."
      ends:   "Use groundnews.com/hack so they know we sent you."
      note:   Ground News, with promo code
 9. 17:51 -> 19:09  (78.8s)  sponsor_read  [text] 
      starts: "Support for the show comes from Pebble."
      ends:   "That's highpebl.ai, terms and conditions apply."
      note:   Pebble, with promo link
10. 19:09 -> 19:57  (47.4s)  cross_promo  [text] 
      starts: "Have you ever wondered who invented rock and roll or why Fleetwood Mac fell apart or what"
      ends:   "I promise The Monday Music Club is going to be the podcast for you."
      note:   The Monday Music Club
11. 19:57 -> 20:07  (10.0s)  other  [text] <ambiguous / don't-care>
      starts: "[MUSIC]"
      ends:   "[MUSIC]"
      note:   music sting after The Monday Music Club
12. 40:35 -> 41:16  (40.5s)  cross_promo  [text] 
      starts: "Are you stuck in a job working hard, but feel like no matter what you do, you just can't"
      ends:   "Listen wherever you get your podcasts or watch on YouTube.com/yourrichbff."
      note:   Net Worth and Chill
13. 41:16 -> 41:50  (34.4s)  cross_promo  [text] 
      starts: "Apple dropped some new upgraded computers this week, like a new Mac Studio for AI stuff,"
      ends:   "That's this week on The Vergecast, wherever you get your podcasts."
      note:   The Vergecast
14. 44:35 -> 44:49  (13.8s)  self_promo  [text] <ambiguous / don't-care>
      starts: "Hey, guys, a real fast plug for my show."
      ends:   "the next guy to go fight."
      note:   Guest plugs his own show 'On the Road' mid-conversation
15. 1:12:33 -> 1:13:06  (33.9s)  credits  [text] <ambiguous / don't-care>
      starts: "of politics and this is your home for it. Thanks for listening. Hacks on Tap is part of the Vox"
      ends:   "visit podcast.VoxMedia.com."
      note:   Production credits and Vox Media boilerplate
