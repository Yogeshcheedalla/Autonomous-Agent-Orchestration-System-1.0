"""Who is actually asking.

Until now authority in Akansha was self-declared: `resolve_speaker_identity`
returns `identity_verified = False` on every path, and a turn that claims nothing
resolves to OWNER because the local desktop session is assumed to be Yogesh. That
is a reasonable default for a machine in one person's room, and it is not evidence.

This module turns "who is the boss" into evidence that can be counted, with one
rule above all others: **a factor that cannot prove anything must say so.** No
model installed means `unavailable`, never a quiet pass. The four factors the user
asked for are not equal and are not pretended to be:

* `passphrase` -- a shared secret, scrypt-hashed, compared in constant time. Real
  proof of knowledge. The only factor strong enough for a step that cannot be undone.
* `history_question` -- a challenge built from the owner's own remembered rows.
  Proof of *acquaintance*: a stranger fails it, someone who has read this screen
  over your shoulder might not.
* `voice_match` -- MFCC statistics against an enrolled voiceprint (see
  `voice_factor` for exactly how weak that is). Suggestive. Never sufficient alone.
* `camera_match` -- honestly unavailable: no face model is installed. Reports
  `unavailable` and earns nothing.
* `local_session` -- someone is at this desk. Weak, but real, and load-bearing:
  it is what keeps the person at the machine from being locked out of ordinary work.

Nothing here performs a side effect. No DB session, no audio capture, no clock
that is not passed in. `assess()` is a pure function of the factors it is handed,
so the API layer stays the only place that can decide what a real request proves.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# --- Outcomes ---------------------------------------------------------------
#
# The distinction between `fail` and `unavailable` is the whole point of the
# module. `fail` is an answer that was wrong -- evidence *against* the claim.
# `unavailable` is a question that could not be asked, which is evidence of
# nothing and must never be scored as either side.

OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_NOT_ENROLLED = "not_enrolled"
OUTCOME_ABSENT = "absent"

FACTOR_SESSION = "local_session"
FACTOR_PASSPHRASE = "passphrase"
FACTOR_HISTORY = "history_question"
FACTOR_VOICE = "voice_match"
FACTOR_CAMERA = "camera_match"

#: What a factor is worth when it passes outright. These are evidence weights,
#: not probabilities, and they are deliberately not normalised: `passphrase`
#: alone clears a grant, `local_session` alone never does.
_WEIGHT: dict[str, float] = {
    FACTOR_SESSION: 0.35,
    FACTOR_PASSPHRASE: 0.60,
    FACTOR_HISTORY: 0.35,
    FACTOR_VOICE: 0.30,
    FACTOR_CAMERA: 0.40,
}

#: Factors that require the asker to *answer* something. Adding up passive
#: signals -- at the desk, sounds about right -- can never reach a grant, because
#: every one of them is available to whoever walked into the room.
_CHALLENGE_FACTORS = (FACTOR_PASSPHRASE, FACTOR_HISTORY)

FACTOR_LABEL: dict[str, str] = {
    FACTOR_SESSION: "at this machine",
    FACTOR_PASSPHRASE: "passphrase",
    FACTOR_HISTORY: "something only you would know",
    FACTOR_VOICE: "voice",
    FACTOR_CAMERA: "camera",
}

# --- Tiers ------------------------------------------------------------------
#
# Sensitivity is a property of the operation, not of the speaker, so the caller
# names the tier and this module never guesses it.

TIER_READ = "read"
TIER_ACT = "act"
TIER_GRANT = "grant"
TIER_IRREVERSIBLE = "irreversible"

_THRESHOLD: dict[str, float] = {
    TIER_READ: 0.0,
    TIER_ACT: 0.30,
    TIER_GRANT: 0.70,
    TIER_IRREVERSIBLE: 0.90,
}

TIER_PROSE: dict[str, str] = {
    TIER_READ: "read what is already here",
    TIER_ACT: "do that",
    TIER_GRANT: "connect an account in your name",
    TIER_IRREVERSIBLE: "do something that cannot be taken back",
}


@dataclass(frozen=True)
class Factor:
    """One piece of evidence about who is asking, and what it is worth.

    `confidence` scales a passing factor: a voice match at the very edge of the
    threshold should not count for as much as one that is unmistakable. It is
    ignored for every outcome other than `pass`, because a partial failure is
    still a failure and a partial `unavailable` is a contradiction in terms.
    """

    name: str
    outcome: str
    confidence: float = 1.0
    detail: str = ""

    @property
    def earned(self) -> float:
        if self.outcome != OUTCOME_PASS:
            return 0.0
        weight = _WEIGHT.get(self.name, 0.0)
        return round(weight * max(0.0, min(1.0, self.confidence)), 4)

    @property
    def contradicts(self) -> bool:
        """True only for a wrong answer -- not for a question never asked."""
        return self.outcome == OUTCOME_FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": FACTOR_LABEL.get(self.name, self.name),
            "outcome": self.outcome,
            "confidence": round(self.confidence, 4),
            "earned": self.earned,
            "detail": self.detail,
        }


@dataclass
class Verdict:
    tier: str
    score: float
    threshold: float
    allowed: bool
    factors: list[Factor] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    #: The one thing that would settle it, phrased to be said out loud.
    challenge: str = ""
    #: Which factor that challenge is asking for, so the caller can route the answer.
    challenge_factor: str = ""

    @property
    def contradicted(self) -> bool:
        return any(factor.contradicts for factor in self.factors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "allowed": self.allowed,
            "contradicted": self.contradicted,
            "factors": [factor.as_dict() for factor in self.factors],
            "reasoning": list(self.reasoning),
            "challenge": self.challenge,
            "challenge_factor": self.challenge_factor,
        }


# --- The decision -----------------------------------------------------------


def assess(tier: str, factors: Sequence[Factor]) -> Verdict:
    """What this set of factors is allowed to do. Pure.

    Three rules, in the order they are applied, because the arithmetic alone gives
    the wrong answer in two specific cases:

    1. **A wrong answer stops everything above ordinary work.** Not because the
       score dropped -- a failed factor earns nothing, so it already did -- but
       because a wrong passphrase is a different event from an unasked one and
       must not be dissolved into a total. `act` stays reachable on purpose: a
       fumbled passphrase must not lock the owner out of their own desktop.
    2. **Passive signals cannot add up to a grant.** Sitting at the desk and
       sounding roughly right are both available to whoever is in the room, so a
       grant needs at least one factor that had to be *answered*.
    3. **An irreversible step needs the strong factor specifically.** Not 0.90
       of anything; the passphrase.
    """

    tier = tier if tier in _THRESHOLD else TIER_ACT
    threshold = _THRESHOLD[tier]
    score = min(1.0, round(sum(factor.earned for factor in factors), 4))
    verdict = Verdict(tier=tier, score=score, threshold=threshold, allowed=False, factors=list(factors))

    passed = {factor.name for factor in factors if factor.outcome == OUTCOME_PASS}
    wrong = [factor for factor in factors if factor.contradicts]
    unavailable = [factor for factor in factors if factor.outcome == OUTCOME_UNAVAILABLE]

    if tier == TIER_READ:
        verdict.allowed = True
        verdict.reasoning.append("Reading what is already here needs no proof of who is asking.")
        return verdict

    if wrong and tier != TIER_ACT:
        names = ", ".join(FACTOR_LABEL.get(f.name, f.name) for f in wrong)
        verdict.reasoning.append(f"The {names} check was answered, and answered wrong.")
        verdict.challenge_factor = wrong[0].name
        verdict.challenge = _challenge_sentence(wrong[0].name, retry=True)
        return verdict

    if score < threshold:
        verdict.reasoning.append(
            f"That would {TIER_PROSE[tier]}, and I am {score:.2f} of the {threshold:.2f} sure it is you."
        )
    elif tier in (TIER_GRANT, TIER_IRREVERSIBLE) and not passed & set(_CHALLENGE_FACTORS):
        verdict.reasoning.append(
            "Being at this machine is not the same as being you, and nothing has been asked yet."
        )
    elif tier == TIER_IRREVERSIBLE and FACTOR_PASSPHRASE not in passed:
        verdict.reasoning.append("A step that cannot be taken back needs the passphrase, not a near miss.")
    else:
        verdict.allowed = True
        earned = [f"{FACTOR_LABEL.get(f.name, f.name)} ({f.earned:.2f})" for f in factors if f.earned]
        verdict.reasoning.append("Satisfied by: " + ", ".join(earned) + ".")
        if unavailable:
            missing = ", ".join(FACTOR_LABEL.get(f.name, f.name) for f in unavailable)
            verdict.reasoning.append(f"Not counted either way: {missing} could not be checked here.")
        return verdict

    verdict.challenge_factor = _next_factor(tier, factors)
    verdict.challenge = _challenge_sentence(verdict.challenge_factor) if verdict.challenge_factor else ""
    if not verdict.challenge_factor:
        verdict.reasoning.append(
            "Nothing is enrolled that I could ask you for, so there is no question that would settle this."
        )
    if unavailable:
        missing = "; ".join(f"{FACTOR_LABEL.get(f.name, f.name)}: {f.detail}" for f in unavailable if f.detail)
        if missing:
            verdict.reasoning.append(missing)
    return verdict


def _next_factor(tier: str, factors: Sequence[Factor]) -> str:
    """The cheapest factor that would actually move the score, or "" if there is none.

    Ordered by what it costs the person, not by what it is worth: a question they
    can answer from memory before a secret they have to recall exactly. An
    irreversible step skips that courtesy, because only the passphrase clears it.

    Returning "" is load-bearing rather than a fallback. A factor that has never
    been enrolled cannot be asked for -- there is no answer that would satisfy it
    -- so on a fresh machine there is genuinely nothing to ask, and saying so lets
    the caller decide whether to hold the door open instead of demanding the
    impossible.
    """

    already = {f.name for f in factors if f.outcome in (OUTCOME_PASS, OUTCOME_FAIL)}
    unenrolled = {f.name for f in factors if f.outcome == OUTCOME_NOT_ENROLLED}
    if tier == TIER_IRREVERSIBLE:
        return "" if FACTOR_PASSPHRASE in unenrolled else FACTOR_PASSPHRASE
    for candidate in (FACTOR_HISTORY, FACTOR_PASSPHRASE):
        if candidate not in already and candidate not in unenrolled:
            return candidate
    return "" if FACTOR_PASSPHRASE in unenrolled else FACTOR_PASSPHRASE


def _challenge_sentence(factor: str, *, retry: bool = False) -> str:
    if factor == FACTOR_PASSPHRASE:
        return "That is not the passphrase. Say it again?" if retry else "Say your passphrase and I will go ahead."
    if factor == FACTOR_HISTORY:
        return (
            "That is not what I have written down. Let me ask you something else."
            if retry
            else "Let me check it is you first -- answer one thing only you would know."
        )
    return "I need to be sure it is you before I do that."


# --- Passphrase -------------------------------------------------------------
#
# A passphrase here is usually *spoken*, which changes the rules. It arrives via
# a transcriber, so it cannot be case-sensitive, cannot carry punctuation, and
# cannot depend on whitespace -- "Open Sesame." and "open sesame" have to be the
# same secret or the factor is unusable by voice. `_spoken_form` is where that
# leniency lives, and it is the only leniency: after normalising, the compare is
# exact and constant time.

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

#: Short enough to be guessed by someone who heard you say it once.
MIN_PASSPHRASE_WORDS = 2


def _spoken_form(secret: str) -> str:
    text = unicodedata.normalize("NFKC", secret or "").casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text)).strip()


def hash_passphrase(secret: str, *, salt: bytes | None = None) -> dict[str, Any]:
    """A stored passphrase record. The plaintext never leaves this call."""
    normalised = _spoken_form(secret)
    if not normalised:
        raise ValueError("A passphrase cannot be empty.")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        normalised.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return {
        "algorithm": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": salt.hex(),
        "hash": digest.hex(),
        "words": len(normalised.split(" ")),
    }


def passphrase_factor(said: str | None, stored: dict[str, Any] | None) -> Factor:
    """Compare a spoken or typed passphrase against the stored record.

    Nothing enrolled is `not_enrolled`, not `fail`: the owner has not chosen a
    passphrase yet, which says nothing about whether this is the owner.
    """

    if not stored or not stored.get("hash") or not stored.get("salt"):
        return Factor(
            FACTOR_PASSPHRASE,
            OUTCOME_NOT_ENROLLED,
            detail="No passphrase set yet -- say 'set my passphrase' to add one.",
        )
    if said is None or not _spoken_form(said):
        return Factor(FACTOR_PASSPHRASE, OUTCOME_ABSENT, detail="Not asked this turn.")

    try:
        candidate = hashlib.scrypt(
            _spoken_form(said).encode("utf-8"),
            salt=bytes.fromhex(str(stored["salt"])),
            n=int(stored.get("n", _SCRYPT_N)),
            r=int(stored.get("r", _SCRYPT_R)),
            p=int(stored.get("p", _SCRYPT_P)),
            dklen=32,
        )
    except (ValueError, TypeError) as error:
        return Factor(FACTOR_PASSPHRASE, OUTCOME_UNAVAILABLE, detail=f"Stored record unreadable: {error}")

    if hmac.compare_digest(candidate.hex(), str(stored["hash"])):
        return Factor(FACTOR_PASSPHRASE, OUTCOME_PASS, detail="Passphrase matched.")
    return Factor(FACTOR_PASSPHRASE, OUTCOME_FAIL, detail="Passphrase did not match.")


# --- The question only the owner can answer ---------------------------------
#
# Built from the owner's own rows, never from a fixed list, because a fixed list
# is a list someone can read. The keys are chosen for how *rare* they are across
# the whole corpus: "the" is in every row and proves nothing, "hyderabad" is in
# one and proves acquaintance. Answers are matched on recall, not wording -- this
# is spoken, and demanding an exact sentence would fail the real owner first.

_STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have he her him his how i
    if in into is it its me my not of on or our she that the their them then there they this to us
    was we were what when where which who will with would you your about with""".split()
)
_WORD = re.compile(r"[a-z0-9']+")
_POSSESSIVE = re.compile(r"'s$")
#: Below this the question is not discriminating enough to be worth asking: one
#: key makes the answer all-or-nothing on a single word the owner may paraphrase.
_MIN_KEYS = 2
_MAX_KEYS = 4


@dataclass(frozen=True)
class HistoryChallenge:
    question: str
    answer_keys: tuple[str, ...]
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        # `answer_keys` is deliberately absent: this dict is sent to a client, and
        # a challenge that ships its own answer is theatre.
        return {"question": self.question, "source": self.source, "keys": len(self.answer_keys)}


def _content_words(text: str) -> list[str]:
    """Words worth matching on, in the form a transcriber would produce.

    The possessive is stripped because a transcript says "akansha sister" as
    often as "akansha's sister", and a factor that turns on an apostrophe fails
    the real owner before it fails anyone else.
    """
    words = (_POSSESSIVE.sub("", w) for w in _WORD.findall((text or "").casefold()))
    return [w for w in words if len(w) >= 4 and w not in _STOPWORDS]


def history_challenge(rows: Sequence[dict[str, Any]], *, seed: int | None = None) -> HistoryChallenge | None:
    """A question drawn from what the owner has actually said or saved.

    Returns None when there is not enough history to ask anything discriminating,
    which is a real state on a fresh install and must not be faked into a question
    the owner cannot answer either.
    """

    import random

    usable: list[tuple[dict[str, Any], list[str]]] = []
    frequency: dict[str, int] = {}
    for row in rows:
        topic_words = set(_content_words(str(row.get("topic") or "")))
        body = _content_words(str(row.get("text") or ""))
        # A question that quotes the topic must not also accept the topic as its
        # own answer -- otherwise the challenge ships the answer with it, and
        # anyone who heard the question passes.
        askable = [word for word in body if word not in topic_words]
        for word in set(body) | topic_words:
            frequency[word] = frequency.get(word, 0) + 1
        if len(set(askable)) < _MIN_KEYS:
            continue
        usable.append((row, askable))
    if not usable:
        return None

    chooser = random.Random(seed)
    row, words = chooser.choice(usable)
    # Rarest first: a key that appears in one row is the one a stranger cannot guess.
    ranked = sorted(dict.fromkeys(words), key=lambda w: (frequency.get(w, 1), -len(w)))
    keys = tuple(ranked[:_MAX_KEYS])
    if len(keys) < _MIN_KEYS:
        return None

    topic = str(row.get("topic") or "").strip()
    when = str(row.get("when") or "").strip()
    if topic:
        question = f"You had me write something down about {topic} -- what was it?"
    elif when:
        question = f"Around {when} you told me something worth keeping. What was it about?"
    else:
        question = "Tell me one thing you have had me remember for you."
    return HistoryChallenge(question=question, answer_keys=keys, source=str(row.get("kind") or "history"))


def history_factor(said: str | None, challenge: HistoryChallenge | None) -> Factor:
    """Score an answer by how much of the remembered detail it recovers."""

    if challenge is None:
        return Factor(
            FACTOR_HISTORY,
            OUTCOME_NOT_ENROLLED,
            detail="Not enough history yet to ask you anything only you would know.",
        )
    if said is None or not said.strip():
        return Factor(FACTOR_HISTORY, OUTCOME_ABSENT, detail="Not asked this turn.")

    heard = set(_content_words(said))
    hit = [key for key in challenge.answer_keys if key in heard]
    recall = len(hit) / max(1, len(challenge.answer_keys))
    if not hit:
        return Factor(FACTOR_HISTORY, OUTCOME_FAIL, confidence=0.0, detail="None of that matches what I have.")
    # Half the detail is enough: this is a spoken answer, and the owner paraphrases.
    if recall < 0.5:
        return Factor(
            FACTOR_HISTORY,
            OUTCOME_FAIL,
            confidence=round(recall, 3),
            detail=f"Only {len(hit)} of {len(challenge.answer_keys)} details matched.",
        )
    # A passing challenge is worth its full weight, and the recall goes in the
    # detail rather than into the score. Scaling it down here would let a factor
    # report `pass` and still not clear the tier it was asked for -- which reads,
    # to the person who answered correctly in their own words, as being called a
    # liar by arithmetic. `voice_match` scales because similarity is continuous;
    # an answered question is not.
    return Factor(
        FACTOR_HISTORY,
        OUTCOME_PASS,
        confidence=1.0,
        detail=(
            f"{len(hit)} of {len(challenge.answer_keys)} remembered details matched "
            f"({recall:.0%} of the wording)."
        ),
    )


# --- Voice -------------------------------------------------------------------
#
# What this is, stated plainly so nobody upstream mistakes it for speaker
# verification: it is the mean and standard deviation of 20 MFCCs, compared by
# cosine similarity. That measures the average *timbre* of a recording. It
# separates two clearly different voices in the same room and it is confounded by
# a head cold, a different microphone, a fan, and a phone on speaker. It can be
# defeated outright by playing back a recording of the owner.
#
# Real speaker embeddings need a model -- resemblyzer, speechbrain or
# pyannote.audio, all of which need torch, and none of which is installed here
# (measured, not assumed: see `probe_voice_stack`). So this factor is capped at
# 0.30 of the evidence, can never satisfy a grant on its own, and the docstring
# above is the reason rather than an apology.

VOICEPRINT_ALGORITHM = "mfcc20-meanstd-v1"
#: Cosine similarity above which two recordings are called the same speaker.
#: Chosen conservatively: MFCC-mean cosine similarity runs high between any two
#: human voices, so a low bar here would pass everyone.
VOICE_MATCH_THRESHOLD = 0.92
#: Below this there is not enough signal to say anything at all.
MIN_VOICE_SECONDS = 1.2


def probe_voice_stack() -> dict[str, bool]:
    """Which voice libraries are actually importable, right now, on this machine."""
    present: dict[str, bool] = {}
    for module in ("librosa", "soundfile", "numpy", "resemblyzer", "speechbrain", "torch"):
        try:
            __import__(module)
            present[module] = True
        except Exception:
            present[module] = False
    return present


def voiceprint_from_audio(path: str) -> dict[str, Any] | None:
    """A voiceprint for one recording, or None if it cannot honestly be made.

    None covers every reason: librosa missing, file unreadable, clip too short.
    The caller must treat all of them as `unavailable`, not as a mismatch.
    """

    try:
        import librosa
    except Exception:
        return None
    try:
        samples, rate = librosa.load(path, sr=16000, mono=True)
    except Exception:
        return None
    return voiceprint_from_samples(samples, rate)


def voiceprint_from_samples(samples: Any, rate: int = 16000) -> dict[str, Any] | None:
    """The same voiceprint, from audio that has already been decoded.

    Exists so the live path costs nothing extra: `/api/voice/stt` has already
    turned the upload into mono float32 at 16 kHz for Whisper, and writing it
    back out to a temp file just so librosa could read it again would add a disk
    round trip to every spoken turn. `voiceprint_from_audio` is the same function
    with a `load` in front of it.
    """

    try:
        import librosa
        import numpy
    except Exception:
        return None
    if samples is None or len(samples) < int(MIN_VOICE_SECONDS * rate):
        return None
    # Trim the silence first: leading room tone drags the mean towards the room
    # rather than the person.
    try:
        trimmed, _ = librosa.effects.trim(numpy.asarray(samples, dtype=numpy.float32), top_db=30)
    except Exception:
        return None
    if len(trimmed) < int(MIN_VOICE_SECONDS * rate):
        trimmed = numpy.asarray(samples, dtype=numpy.float32)
    try:
        mfcc = librosa.feature.mfcc(y=trimmed, sr=rate, n_mfcc=20)
    except Exception:
        return None
    vector = numpy.concatenate([mfcc.mean(axis=1), mfcc.std(axis=1)])
    if not numpy.all(numpy.isfinite(vector)):
        return None
    return {
        "algorithm": VOICEPRINT_ALGORITHM,
        "vector": [round(float(value), 6) for value in vector],
        "seconds": round(len(trimmed) / float(rate), 3),
    }


def voice_similarity(left: dict[str, Any] | None, right: dict[str, Any] | None) -> float | None:
    """Cosine similarity of two voiceprints, or None if they are not comparable.

    Pure arithmetic -- no numpy, so this stays importable and testable on a box
    with nothing installed.
    """

    if not left or not right:
        return None
    if left.get("algorithm") != right.get("algorithm"):
        return None
    a = [float(v) for v in (left.get("vector") or [])]
    b = [float(v) for v in (right.get("vector") or [])]
    if not a or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if norm <= 0:
        return None
    return max(-1.0, min(1.0, dot / norm))


def voice_factor(
    heard: dict[str, Any] | None,
    enrolled: dict[str, Any] | None,
    *,
    threshold: float = VOICE_MATCH_THRESHOLD,
) -> Factor:
    """Compare this turn's voiceprint against the owner's enrolled one."""

    if not enrolled or not enrolled.get("vector"):
        return Factor(
            FACTOR_VOICE,
            OUTCOME_NOT_ENROLLED,
            detail="No voiceprint enrolled -- say 'learn my voice' once and I will keep it.",
        )
    if not heard or not heard.get("vector"):
        return Factor(
            FACTOR_VOICE,
            OUTCOME_UNAVAILABLE,
            detail="No usable audio this turn (too short, unreadable, or librosa unavailable).",
        )
    similarity = voice_similarity(heard, enrolled)
    if similarity is None:
        return Factor(FACTOR_VOICE, OUTCOME_UNAVAILABLE, detail="The two voiceprints are not comparable.")
    if similarity < threshold:
        return Factor(
            FACTOR_VOICE,
            OUTCOME_FAIL,
            confidence=0.0,
            detail=f"Voice similarity {similarity:.3f} is below {threshold:.2f}.",
        )
    # Scale across the band above the threshold rather than treating 0.921 and
    # 0.999 as the same evidence.
    span = max(1e-6, 1.0 - threshold)
    return Factor(
        FACTOR_VOICE,
        OUTCOME_PASS,
        confidence=round(min(1.0, (similarity - threshold) / span * 0.5 + 0.5), 3),
        detail=f"Voice similarity {similarity:.3f} (timbre only -- not proof on its own).",
    )


# --- Camera ------------------------------------------------------------------


def probe_camera_stack() -> dict[str, bool]:
    present: dict[str, bool] = {}
    for module in ("cv2", "face_recognition", "insightface"):
        try:
            __import__(module)
            present[module] = True
        except Exception:
            present[module] = False
    return present


def camera_factor(*, frame: Any = None, stack: dict[str, bool] | None = None) -> Factor:
    """The camera factor, which today cannot pass.

    This is not a stub waiting to be filled with a guess. No face model is
    installed, so there is nothing that could compare a frame to the owner, and
    the only correct answer is `unavailable`. When a model is added, this function
    is where it goes -- and it should return `fail` on a mismatch, because a face
    that is not the owner's is evidence against, not a missing check.
    """

    installed = probe_camera_stack() if stack is None else stack
    missing = sorted(name for name, ok in installed.items() if not ok)
    if missing:
        return Factor(
            FACTOR_CAMERA,
            OUTCOME_UNAVAILABLE,
            detail="No face model installed (" + ", ".join(missing) + ") -- the camera proves nothing here.",
        )
    if frame is None:
        return Factor(FACTOR_CAMERA, OUTCOME_ABSENT, detail="Camera is off this turn.")
    return Factor(
        FACTOR_CAMERA,
        OUTCOME_UNAVAILABLE,
        detail="A face model is installed but no enrolled face comparison is wired up yet.",
    )


def local_session_factor(*, at_this_machine: bool = True) -> Factor:
    """Someone is at this desk. Weak, and the reason nobody gets locked out."""
    if not at_this_machine:
        return Factor(FACTOR_SESSION, OUTCOME_ABSENT, detail="Request did not come from the local session.")
    return Factor(
        FACTOR_SESSION,
        OUTCOME_PASS,
        detail="Asked from the desktop session on this machine.",
    )


def spoken_verdict(verdict: Verdict) -> str:
    """One sentence to say out loud: what happened, and what would fix it."""
    if verdict.allowed:
        return ""
    if verdict.challenge:
        return " ".join(verdict.reasoning[:1] + [verdict.challenge]).strip()
    return " ".join(verdict.reasoning[:2]).strip() or "I need to be sure it is you before I do that."
