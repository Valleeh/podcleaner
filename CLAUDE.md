# PodClean v2

Removes advertising from podcast episodes. Serves a rewritten RSS feed whose enclosure
URLs point back here, and produces the ad-free audio when one is played.

**Current phase: MVP.** Read `docs/mvp.md` first — it defines what is in scope and holds
the ordered to-do list. Read `docs/architecture.md` only when you need a decision's
rationale; it describes the full system, not the MVP.

## The one rule

Never remove real content. A missed advertisement is an annoyance. A sentence clipped
mid-word is destructive and the listener cannot recover it. When a cut edge is uncertain,
**refuse to cut** and serve the original. This rule outranks every other goal, including
in the MVP.

## Commands

There is no `python` and no `make` on this host. Use `./run`.

    ./run test       # 92 offline unit tests, under a second
    ./run ads        # LLM suite, costs money
    ./run whisper    # whisper suite, minutes to hours
    ./run labels …   # label CLI: checklist / verify / finish

## Host limits

2 GB RAM, about 1.3 GB free; Caddy, Vikunja and Portainer hold the rest.
**Never run the whisper suite and the LLM suite at the same time**, and only one whisper
job at once. Three background jobs were already killed here for memory.
whisper runs only in the `whisper-cpp:local` Docker image at about 1.6x the audio length.

## Never commit

`var/` (823 MB of commercial audio, caches, reports) and `.secret.json`.
The OpenRouter token lives in `.secret.json` under `openrouter-token`.

## `old/` is an archive

The v1 MQTT pipeline. It is kept to harvest two modules from, nothing else runs.
`old/v1-notes.md` describes v1 and does **not** apply here — its setup commands are wrong
for this repo. Do not follow it and do not extend anything under `old/`.

## How to work here

- Simplicity wins. Functions over classes, no base classes, no dependency injection, no
  config objects passed through layers. Do not build for cases that have not happened.
- Commit at every green test run. Small commits on a branch are cheap.
- Delegate broad searches to a subagent so its hits never enter the main context.
- Change files with Edit, not with shell scripts that pipe whole files through the context.
- Batch independent tool calls into one response.
- Clean up after every step: delete what the step made unnecessary, then commit.

## The step workflow

Every numbered step in `docs/mvp.md` runs the same loop. The lead drives it; the agents do
the work. `master` is the trunk and every step lands there through a pull request.

1. **Branch.** `mvp/step-<n>-<slug>`, cut from `origin/master`. One step per branch.
2. **Build.** The `mvp-build` agent, given the step number and nothing else.
3. **Review.** The `cut-guard` agent, on every step that touches detection, boundaries,
   cutting or the audio path. Findings go back to `mvp-build`; then `cut-guard` re-reviews
   the fix, because a review of a version that no longer exists proves nothing.
4. **Finish.** The `janitor` agent: delete what the step made unnecessary, tick the step
   off in `docs/mvp.md`, `./run test`, commit.
5. **Pull request** into `master`, then merge once the review is clean and tests are green.

A step is not done until its box in `docs/mvp.md` is ticked and its PR is merged. Do not
start the next step before that.

`gh` needs `gh auth login` on this host; pushing over SSH already works.
