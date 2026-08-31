# Audit narrative

This is the prose record of the audit of Akansha's voice, memory and control paths:
what was looked at, what turned out to be broken, how that was established, and
what is still open. It is written to be read start to finish rather than skimmed
as a checklist, because most of the defects below share one cause and that cause
is the interesting part.

## What "correct" was taken to mean

The governing requirement for this project is not *voice → text → answer*. It is
**voice understanding → context → reasoning → the right operation actually
performed**. Success is the user's intended operation completing correctly, not a
fluent-sounding reply. That definition decided which findings counted. A reply
that sounds right while the operation silently did not happen is a failure of the
whole system, and several of the defects below were exactly that shape: code that
read as a working feature, produced no error, and did nothing.

## Method: measure, do not read

Every finding here was established by running something, not by reading code and
reasoning about it. That was not a stylistic preference — it is what separated the
real defects from the plausible ones.

The clearest example is speaker identity. Reading `_chat_speaker_profile` suggested
it derived a caller's access level from their claimed relationship, and there was a
line of code that appeared to do exactly that. Calling it four times with four
different claims returned:

```
relationship 'friend' -> access=owner   closeness=close
relationship None     -> access=owner   closeness=close
relationship 'guest'  -> access=owner   closeness=close
relationship 'owner'  -> access=owner   closeness=close
```

The derivation was behind `if not merged.get("access_level")`, and the merge it
guarded always set that key. The guard could never fire. The value was computed
and discarded on every call. Reading the function would have found the intent;
only running it found that the intent was not implemented.

The same discipline applied in the other direction. Counting dead dependencies
with a substring grep reported `express` as having two references in `src/`. An
import-level pattern — `from 'dep(/|')`, `require('dep`, `"dep"` — reported zero.
The substring hits were prose in comments. Two of the twelve packages initially
flagged as dead were then *kept*, because `eslint-plugin-prettier` and
`eslint-config-prettier` have no import references anywhere and are still required
via `plugin:prettier/recommended` in `.eslintrc.json`. A count is only evidence if
it counts the right thing.

## The recurring cause: code that looks like a feature

Nearly every defect found was a *silent* one. Not a crash, not a wrong answer with
a visible error — a path that executed, returned, and accomplished nothing, while
reading in the diff as though it worked.

**A guard that can never fire is dead code that looks like a safety check.** The
speaker-identity bug above is the canonical instance. Its consequence was not
subtle: a chat request that explicitly declared itself a guest received owner
authority, and — because the resolution was a dict merge with the owner's profile
as the base — also received the owner's bio, education, current project and running
conversation summary as *its own* context, which the model was then handed as
background for that visitor.

**A test that tests a translation is worse than no test.** `src/lib/wakeWord.ts`
carried a header claiming "the tests in `backend/test_audit_regressions.py`
exercise it." There were none; the file had zero references in that suite. Writing
them found three real defects, and it found them *because* the tests drive the real
shipped TypeScript through Node rather than a Python re-implementation of the
algorithm. A translated copy would have encoded the auditor's mental model of how
matching works and passed cleanly against a broken file — reporting safety it could
not see.

The three defects it found are worth naming, because each is invisible in review:

- The normalisation filter kept `\p{L}` and `\p{N}` and dropped everything else.
  Combining marks are neither. A virama or a dependent vowel sign is `\p{M}`, so
  "ఏయ్" normalised to "ఏయ" and "హే" to "హ" — which meant all four Telugu and Hindi
  greetings in the `GREETINGS` set could never match anything. The bilingual
  support the list documents was inert, and had been since it was written.
- The two-token join, added so the recogniser's "a kansha" would still wake her,
  also joined `"ok"` + `"akansha"` into `"okakansha"` — edit distance 2 from the
  name, therefore accepted. The greeting was swallowed into the matched token, so
  `"ok akansha"` stopped registering as a bare call while `"hey akansha"` still
  did, purely because `"heyakansha"` happens to be too long to survive the distance
  limit. Two spellings of one utterance behaving differently.
- Callers recovering the text before the wake phrase via
  `normalized.indexOf(matchedToken)` got `-1` for the two-token forms, because the
  joined token never appears literally in the normalised string. `slice(0, -1)`
  then silently returned the whole transcript minus its last character instead of
  an empty prefix.

**A memoization wrapper is easy to silently neuter.** `memo` on the five chat
panels does nothing if one inline arrow is passed at the call site: the props
comparison fails every render, the wrapper skips nothing, and the diff still shows
`memo(...)` sitting there. The cost was measurable rather than theoretical —
`ChatWorkspace` re-renders whenever `onStatsChange` fires, and `contextUnits` is
`ceil(characters / 4)`, so a thousand-character answer re-rendered that subtree a
couple of hundred times.

**Sixteen literals are a single point of failure spread out.** The
knowledge-graph owner id is the graph's filename. It was written as a hardcoded
`"default_user"` at sixteen call sites, so introducing real identity later meant
finding all sixteen — and one miss would split a person's facts across two files,
with the second never read again.

## Where voice was quietly a second-class channel

Three findings shared a shape: the voice path was cheaper than the typed path, and
the saving was taken out of correctness.

`/api/voice/chat` streamed a reply and persisted nothing. No `ChatMessage` rows, no
knowledge-graph record, no memory analysis — all three of which run on a typed
message. Anything said out loud taught the assistant nothing, never appeared in
history, and gave the next question less context than the identical question typed.
That is the concrete reason voice felt like it had no memory. It was not a
forgetting bug; the turn was never written down.

`generate_chat_stream` skipped live web fetches whenever the session id started
with `voice`, to avoid 2–8 seconds of blocking latency. That traded accuracy for
responsiveness on the one channel where a wrong answer is hardest to catch — a
spoken answer has no visible citation to disbelieve. The latency was real, so it is
now covered rather than reintroduced: a short acknowledgement is yielded before the
fetch and the client speaks it over the network wait, which is what a person does
before looking something up.

`shouldUseFastBrowserSpeech` returned true for any queued English chunk of ≤180
characters, routing that chunk to the browser's system voice while longer chunks
used the server voice. Her identity changed between chunks of a single reply — two
different voices in one answer. The rule also suppressed the `preparedAudio`
prefetch for exactly the short chunks it existed to speed up, so it was not even a
real latency trade.

## Where the risk was outward-facing

Voice has no undo. A typed message can be re-read before you press send; a spoken
one is gone the moment it is understood, and it was understood by a recogniser that
guesses. `voice_dialog` therefore asks which app, asks who, reads the message back,
and waits — and when what came back is not name-shaped, it asks for the name letter
by letter. Thirteen tests pin that nothing leaves the machine without both filled
slots and an explicit yes.

The privilege problem behind speaker identity was reachable, not theoretical.
`analyze_intent_and_memory` runs as a background task from three chat endpoints and
calls `execute_desktop_command`, so a chat turn can launch a real application on
this machine, with the action chosen by a model reading the conversation. Before
this audit there was no authority check on that path at all. There is now, and the
gate was verified by replacing the executor and confirming that owner and
no-claim turns launch while trusted and guest turns do not.

Two adjacent holes surfaced while fixing it. `POST /api/voice/speakers` accepted
`access_level` straight from the request body, so any caller could store a
permanent owner-level speaker under any display name, with nothing validating the
caller and nothing ever re-deriving the level afterwards. And `_speaker_access_level`
kept its own private copy of the relationship alias table, which had already drifted
from the chat path's copy — the two disagreed about whether "teacher" was a
relationship at all.

## What the identity work is, stated plainly

It is authority derived from a **self-declaration**. It is not authentication.

There is no voiceprint, no speaker embedding, no face recognition, no acoustic
model and no password check anywhere in this codebase. The `speaker_profiles` table
has a `voice_signature_json` column; nothing computes a signature and nothing ever
compares one, so it is an opaque blob a client chose to store. The honest claim is
that a claimed relationship no longer *grants owner rights* — which is a real
improvement over granting them to every claim, and is not the same as knowing who
is speaking.

Every resolved profile is therefore stamped `identity_verified = False` and an
`identification_method` of either `local_session` (no claim was made, so the
request came from the desktop session someone is sitting in front of) or
`self_declared` (the body named somebody). A regression test asserts that flag on
every path, specifically so a later reader cannot mistake declaration for
verification. Building a plausible-looking verifier out of the stored blob would
have been security theatre, and worse than the original bug because it would have
looked solved.

One deliberate non-hardening: a request with no `speaker_profile` still resolves to
the owner. The shipped frontend sends no speaker claim on any request, so making a
missing claim a guest would have locked out every real turn while looking correct
in isolation. This is a single-user desktop assistant; the person at the machine is
the owner.

## What cannot be verified in this environment

No part of this audit heard anything. This environment has no microphone and no
speaker. Transcripts are injected as events, which proves everything from the
transcript inward — matching, dialogue state, persistence, routing, authority — and
proves **nothing** about how her voice sounds, about recognition accuracy on real
audio, or about barge-in and echo behaviour with a live microphone. Any claim of a
live spoken test would be false. The wake-word tests are honest precisely because
that module takes a string and returns a decision, with no audio, no network and no
browser anywhere in it.

Desktop control was never exercised against the real machine during the audit. The
one test that reaches the automation branch replaces `execute_desktop_command` and
asserts the replacement is in place before the call, so a bug in the test cannot
open an application.

## Still open

**Styling and performance sweep of the ten non-chat routes.** `browser-automation`,
`channel-integrations`, `cognitive-dashboard`, `conversation-history`, `generated`,
`planner-service`, `settings`, `sign-up-login-screen`, `task-automations` and
`voice-assistant` have not had the pass that chat-interface received. `api-keys` is
now off that list, having been rebuilt from scratch as the connect surface below.

## One-click connection, and why one click cannot mean zero credentials

There was no single answer to "what can Akansha connect to, how, and is it
connected right now." Social platforms went through `/api/social/setup/{platform}`
against a hardcoded five-platform catalog; desktop apps were launch commands in
`automation.py` with no concept of being connected at all; model providers were a
third surface. Three families, three mechanisms, no shared registry — and
`/api-keys`, the page that should have held the credentials any of it needed, was
display-only: the keys it listed were hardcoded bullet strings, the reveal toggle
unmasked the dummy, copy put bullet characters on the clipboard, and the add button
added nothing.

`backend/app_connect.py` is now the one registry: twenty-seven apps across six
families, each declaring one of four strategies — an executable on this machine, an
API key, an OAuth app, or a self-hosted bridge. Adding an app is adding data.

The design decision worth recording is the refusal to return a boolean. A click
answers with one of six named outcomes, because "not connected" is not something a
user can act on: a missing API key, an application that is not installed, and a
bridge that is configured but not running are three different problems, and only
the first is fixable by typing. `test_no_branch_returns_a_bare_failure` is what
stops a later shortcut from collapsing them back into one flag.

Two defects were found by measuring rather than reading, both of the same shape as
everything else in this document — code that read as working and quietly was not:

- The credential round-trip silently failed. `encrypt_social_config` returns a bare
  `{"scheme", "payload"}` pair, so `metadata.update(...)` put those two keys at the
  top level while `decrypt_social_config` reads `metadata["config_encrypted"]`. The
  save reported connected and the very next status check reported the field still
  missing. Assigning under that one key fixed it.
- Disconnect hardcoded its own answer instead of re-probing. For a desktop app
  there is no credential to remove, so disconnecting Chrome replied "Google Chrome
  credentials removed from this machine" — a sentence about something that never
  existed — reported `disconnected`, and then read `connected` again on the next
  probe, because Chrome is still installed. A card that flips between two states on
  alternate reads is the earlier lie pointed the other way. All three write
  endpoints now return the same re-probed state through one helper, so a click, a
  save and a disconnect cannot disagree.

The frontend follows from the same rule. Each outcome gets its own label, colour
and next action, and the action is chosen by strategy as well as outcome — a
connected API key offers *Disconnect*, a connected executable offers *Re-check*,
because there is nothing to revoke on an installed application. The button label
and the click handler both come from `actionFor`, so a button cannot say one thing
and do another.

Nothing on that page can render a credential. The catalog ships field
*descriptions* — key, label, where to find the value, whether it is secret — and
never values, masked or otherwise, which is why there is no reveal control and no
copy button anywhere in the replacement. Replacing a stored key means typing the
new one. Verified end to end through the browser: a key entered in the form came
back `plaintext in blob: False`, decrypted correctly on read, and left no row
behind after disconnect.

Reading the catalog is deliberately ungated — knowing which apps *could* be
connected leaks nothing. All three mutating routes require owner access, because
connecting grants Akansha standing authority over an account or over this desktop,
which is the class of action `speaker_identity` exists to gate.

## Why the assistant looked like a photograph

Two complaints, both accurate: the voice panel showed "old akansha", and the
full-screen presence showed "only image ... no hand movement lip movement eye
movement". Neither was a missing asset. Measured in the browser on
`/voice-assistant`, before the fix:

```
reducedMotion: true   <video> elements: 0   <img>: akansha-presence.webp
canvas: 300x150 at opacity 0
```

Three separate defects had to line up to produce that, and each of them is the
same mistake in a different place — a single flag standing in for a set.

**Reduced motion deleted her.** `prefers-reduced-motion: reduce` is true on this
machine, and `BodyPresence` read it as "replace the performance with a
photograph". That is the wrong reading of the preference: it exists to stop
movement a person did not ask for, and the assistant's face is the content of the
page, not decoration around it. Reduced motion now removes the cross-fade, the
gesture interrupts and the lean-in, and she keeps playing. The decision moved into
`src/lib/presenceBehavior.ts`, whose `MotionPlan` deliberately has no field that
*can* stop playback — the bug was a boolean that could, so the shape of the type
is the fix and a test asserts the field list.

**One broken clip froze all five.** A single `onError` set one `failed` flag for
the whole component. Health is per clip now, and `playableClip` walks a
substitution chain — a talking pass stands in for a gesture because both have her
mouth moving, `idle` stands in for `listening` because both are hands-at-rest —
so the still frame is reached only when every clip has failed.

**There was one encoding.** The clips were H.264 only, and a Chromium build
without that decoder answers `canPlayType('avc1.42E01E')` with `"probably"` and
then fails the load with `MEDIA_ELEMENT_ERROR: Format error`, code 4,
`decodedFrames: 0` — measured here. Each clip now ships as VP9 WebM beside the
MP4, offered as two `<source>` children so the browser's own selection walks past
whichever it cannot decode. With `src` a failure is terminal; with `<source>` it
is a fallback.

The small panel was a fourth thing: it rendered `HumanPresence`, which warps one
photograph from the viseme timeline. That is phoneme-exact and the film is not,
but one photograph has no head turn, no eye movement and no hands. The panel now
shows the footage cropped to head and shoulders, measured against the frame
rather than guessed — a 340x510 box at (190, 40) of the 720x1280 source, which is
2:3, the panel's own aspect, so nothing is stretched. Verified in the browser: a
120x180 panel holding a 254x452 video at offset (-67, -14), which is that box to
the pixel.

Two honest limits. Without the MuseTalk worker her mouth moves as filmed, not as
the words require; it is gated to speech, so it is never a figure mouthing
silence, but it is not lipsync. And `HumanPresence.tsx` is now referenced by
nothing — it is kept rather than deleted because it is not yet in git, so removing
it would be unrecoverable, and it is the only phoneme-accurate mouth in the tree
if the GPU worker never lands.

## Known debt carried forward

`wants_spelling()` in `backend/voice_dialog.py` is exported and never called by
anything.

`useVoice` has two writers for `speakingVolume`: an `AnalyserNode` on a
`requestAnimationFrame` tick, and a 34 ms `setInterval` that replays the viseme
timeline's intensity. The second exists for the paths with no analysable audio
element. Its `viseme` field now has no reader, because the renderer that consumed
it is unmounted — the field is written and dropped.

`_needs_live_web_context` is a broad regex. It matches `now`, `today`, `top`,
`table`, `model`, `version`, `trend` and `stats`, so some casual spoken questions
take the 2–8 second live-lookup path unnecessarily. The acknowledgement covers the
wait, so this is a cost rather than a break, but it is a cost paid on questions
that did not need it.

## Coverage as it stands

`backend/test_audit_regressions.py` holds 162 tests across eleven classes, plus
321 subtests. Each class exists because something in it was measurably broken, and
each docstring records the specific failure rather than the general principle, on
the theory that the next person to touch that code needs the failure more than they
need the principle.

