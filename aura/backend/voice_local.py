"""Hearing, turn-taking and speech that never leave this machine.

Three things Akansha did not own before this module:

1. **Transcription.** `voice_engine.py` has documented `Raw audio → /api/voice/stt`
   since the day it was written, and that endpoint never existed. The browser's
   `webkitSpeechRecognition` did the hearing, which means Chrome only, audio
   posted to Google, and no say whatsoever in how a Telugu-English sentence gets
   segmented. faster-whisper runs the same Whisper weights locally through
   CTranslate2 at int8 -- no torch, no GPU.

2. **Turn-taking read off the waveform.** `EndpointDetector` decides a turn ended
   from the *words*, and the `silence_ms` it scores was whatever the client
   guessed between recognition events. Silero VAD reads the audio, so a pause
   mid-sentence is silence and an indrawn breath is not the end of a turn. The
   model is the real snakers4 `silero_vad_v6.onnx`, which ships inside
   faster-whisper -- nothing extra to fetch.

3. **Speaking without a round trip.** `/api/voice/tts` calls edge-tts, so every
   reply pays a WAN hop and going offline makes Akansha mute. Piper synthesises
   locally in tens of milliseconds.

Four rules this module is built around, and they are the reason it is a separate
file rather than more code in `main.py`:

- **A missing model reports `unavailable`. It never fakes a pass.** The same rule
  `owner_verify` follows. Callers degrade to the path that worked before instead
  of raising, so installing this cannot make a working system worse.
- **Load once, never inside a request.** Whisper costs seconds to construct and
  microseconds to reuse. Every engine is a lazy singleton behind a lock, and
  `warm()` exists so that cost is paid at startup by choice rather than by the
  first person who speaks.
- **Nothing here reaches the network.** Not to a model host, not to a CDN. If a
  model is absent, that is a fact to report, not a download to start mid-turn --
  a surprise 400 MB fetch during a voice turn is the latency bug this module was
  written to remove.
- **The audio is decoded here, once.** Callers hand over the bytes a browser
  produced; they do not have to know about sample rates. Everything downstream
  works on mono float32 at 16 kHz because that is what both models want.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

#: Both Whisper and Silero are trained at 16 kHz mono. Resampling once here is
#: cheaper than every caller guessing, and guessing wrong makes Silero read a
#: 48 kHz stream as speech three times too fast.
SAMPLE_RATE = 16_000

#: Measured on this machine (12 logical cores, CPU int8, 3.37 s clip, greedy):
#:
#:     small  10548 ms  RTF 3.42x  "message AMMA that I will be late tonight"
#:     base    3316 ms  RTF 1.07x  "Open what's App and Message Amma ..."
#:     tiny    2151 ms  RTF 0.70x  "message, Emma that I will be late"
#:
#: So `base` is the default, not `small`: it is the largest model that keeps up
#: with speech on this CPU. Read the honest consequence -- none of these beat the
#: browser recogniser on latency, which is why local hearing is the offline,
#: non-Chrome and second-opinion path rather than a replacement for it. Set
#: AKANSHA_WHISPER_MODEL=small on a machine with a GPU or spare cores; `small` is
#: the one that gets "Kukatpally" and code-switched Telugu right.
WHISPER_MODEL = os.getenv("AKANSHA_WHISPER_MODEL", "base")
WHISPER_COMPUTE = os.getenv("AKANSHA_WHISPER_COMPUTE", "int8")
WHISPER_DEVICE = os.getenv("AKANSHA_WHISPER_DEVICE", "cpu")
#: 0 lets CTranslate2 pick. Measured no better than explicit, and explicit lets a
#: busy machine cap it so transcription cannot starve the web server.
WHISPER_THREADS = int(os.getenv("AKANSHA_WHISPER_THREADS", "0"))

#: Where Piper voices live. Kept inside the repo so a fresh clone can see what is
#: missing, and gitignored so 60 MB of weights never enters a commit.
VOICE_MODEL_DIR = os.getenv(
    "AKANSHA_PIPER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_models"),
)
PIPER_VOICE = os.getenv("AKANSHA_PIPER_VOICE", "en_US-lessac-medium")

ENGINE_WHISPER = "faster-whisper"
ENGINE_SILERO = "silero-vad-v6"
ENGINE_PIPER = "piper"


@dataclass(frozen=True)
class Capability:
    """What one engine can do right now, and why it cannot if it cannot.

    `installed` is about the library; `ready` is about the weights being in RAM.
    They are separate because the honest answer to "can you hear me" differs
    before and after the first load, and a caller choosing a fallback needs to
    know which of the two is missing.
    """

    name: str
    installed: bool
    ready: bool
    detail: str
    model: str = ""

    @property
    def usable(self) -> bool:
        return self.installed

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed,
            "ready": self.ready,
            "usable": self.usable,
            "detail": self.detail,
            "model": self.model,
        }


# --- Decoding ----------------------------------------------------------------
#
# A browser's MediaRecorder hands over webm/opus, an `<input type=file>` hands
# over whatever the user had, and a test hands over a wav it built in memory. All
# three arrive here as bytes and leave as mono float32 at 16 kHz.


class AudioUnreadable(ValueError):
    """The bytes were not audio this machine can decode.

    Its own exception type because the caller's response differs from every other
    failure in this module: an unreadable upload is the client's problem to fix
    and deserves a 400, whereas a missing model is Akansha's problem and must not
    be reported as the user's mistake.
    """


def decode_audio(data: bytes) -> "Any":
    """`data` as a mono float32 numpy array at :data:`SAMPLE_RATE`.

    PyAV does the work -- it arrives as a faster-whisper dependency, so this adds
    no weight and covers webm/opus, ogg, mp3, m4a and wav through one path. The
    stdlib `wave` fallback exists so the tests can exercise everything downstream
    of here without PyAV being importable.
    """
    if not data:
        raise AudioUnreadable("No audio was sent.")
    try:
        from faster_whisper.audio import decode_audio as _decode  # type: ignore

        return _decode(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
    except AudioUnreadable:
        raise
    except Exception as exc:  # pragma: no cover - depends on the container
        logger.debug("PyAV could not decode the upload (%s); trying wav.", exc)
        return _decode_wav(data)


def _decode_wav(data: bytes) -> "Any":
    """A PCM wav, resampled by decimation and averaged down to one channel.

    Deliberately crude: this is the path for the case where PyAV is unavailable,
    and a nearest-sample resample of a 48 kHz wav is enough for VAD and for a
    test. Anything that cares about fidelity gets the PyAV path above.
    """
    import numpy as np

    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except Exception as exc:
        raise AudioUnreadable(f"That audio could not be read: {exc}") from exc

    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise AudioUnreadable(f"{width * 8}-bit audio is not supported.")
    samples = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    samples /= float(np.iinfo(dtype).max)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and rate > 0:
        index = (np.arange(int(len(samples) * SAMPLE_RATE / rate)) * rate / SAMPLE_RATE)
        samples = samples[index.astype(np.int64).clip(0, len(samples) - 1)]
    return samples


# --- Voice activity ----------------------------------------------------------


@dataclass(frozen=True)
class SpeechWindow:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(frozen=True)
class VadReading:
    """What the waveform says about who is talking.

    `trailing_silence_ms` is the field this whole module exists to produce: it is
    the number `/api/voice/turn-boundary` was previously handed by a client
    stopwatch that could not tell a thinking pause from a finished sentence.
    """

    speaking: bool
    windows: tuple[SpeechWindow, ...]
    speech_ms: float
    trailing_silence_ms: float
    leading_silence_ms: float
    duration_ms: float
    engine: str = ENGINE_SILERO

    @property
    def any_speech(self) -> bool:
        return bool(self.windows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "speaking": self.speaking,
            "any_speech": self.any_speech,
            "windows": [{"start_s": w.start_s, "end_s": w.end_s} for w in self.windows],
            "speech_ms": round(self.speech_ms, 1),
            "trailing_silence_ms": round(self.trailing_silence_ms, 1),
            "leading_silence_ms": round(self.leading_silence_ms, 1),
            "duration_ms": round(self.duration_ms, 1),
            "engine": self.engine,
        }


_vad_lock = threading.Lock()
_vad_options: Any = None


def vad_available() -> bool:
    try:
        import faster_whisper.vad  # noqa: F401
    except Exception:
        return False
    return True


def _options() -> Any:
    """Silero's thresholds, tuned for a person giving instructions out loud.

    `min_silence_duration_ms=280` is the load-bearing one. Silero's own default is
    2000 ms, which is a transcription setting -- it exists so a sentence is not
    chopped into fragments. Used for turn-taking it would mean nearly two seconds
    of dead air before Akansha noticed a question had ended. 280 ms is short
    enough to feel immediate and long enough to survive the gap between words,
    and the *decision* still belongs to `EndpointDetector`, which combines this
    with the words. This only reports what the microphone heard.
    """
    global _vad_options
    if _vad_options is None:
        from faster_whisper.vad import VadOptions

        _vad_options = VadOptions(
            threshold=float(os.getenv("AKANSHA_VAD_THRESHOLD", "0.5")),
            min_speech_duration_ms=120,
            min_silence_duration_ms=int(os.getenv("AKANSHA_VAD_SILENCE_MS", "280")),
            speech_pad_ms=90,
        )
    return _vad_options


def speech_activity(audio: bytes | Any) -> VadReading | None:
    """Where the speech is in `audio`, or None when Silero is not installed.

    None rather than a raise, and None rather than an empty reading: "I could not
    listen" and "I listened and heard nothing" are opposite facts, and a caller
    that cannot tell them apart will treat a missing model as silence and cut the
    user off mid-sentence.
    """
    if not vad_available():
        return None
    import numpy as np

    samples = audio if not isinstance(audio, (bytes, bytearray)) else decode_audio(bytes(audio))
    samples = np.asarray(samples, dtype=np.float32)
    duration_ms = len(samples) / SAMPLE_RATE * 1000.0
    if not len(samples):
        return VadReading(False, (), 0.0, 0.0, 0.0, 0.0)

    from faster_whisper.vad import get_speech_timestamps

    with _vad_lock:
        stamps = get_speech_timestamps(samples, _options(), sampling_rate=SAMPLE_RATE)

    windows = tuple(
        SpeechWindow(float(s["start"]) / SAMPLE_RATE, float(s["end"]) / SAMPLE_RATE)
        for s in stamps
    )
    speech_ms = sum(w.duration_s for w in windows) * 1000.0
    trailing = duration_ms - (windows[-1].end_s * 1000.0) if windows else duration_ms
    leading = windows[0].start_s * 1000.0 if windows else duration_ms
    # "Speaking" means the audio ends inside speech, not that speech occurred:
    # a clip that ended 900 ms ago is a finished turn, not a live one.
    speaking = bool(windows) and trailing <= _options().min_silence_duration_ms
    return VadReading(
        speaking=speaking,
        windows=windows,
        speech_ms=speech_ms,
        trailing_silence_ms=max(0.0, trailing),
        leading_silence_ms=max(0.0, leading),
        duration_ms=duration_ms,
    )


# --- Transcription -----------------------------------------------------------


@dataclass(frozen=True)
class TranscriptSegment:
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    language_confidence: float
    segments: tuple[TranscriptSegment, ...]
    duration_s: float
    latency_ms: float
    engine: str = ENGINE_WHISPER
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "language_confidence": round(self.language_confidence, 3),
            "segments": [
                {"start_s": round(s.start_s, 2), "end_s": round(s.end_s, 2), "text": s.text}
                for s in self.segments
            ],
            "duration_s": round(self.duration_s, 2),
            "latency_ms": round(self.latency_ms, 1),
            "engine": self.engine,
            "model": self.model,
        }


_whisper_lock = threading.Lock()
_whisper: Any = None
_whisper_error = ""


def whisper_installed() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def whisper_ready() -> bool:
    return _whisper is not None


def _whisper_model() -> Any:
    """The loaded model, constructed at most once per process.

    Under a lock because two people speaking at once used to be the only way to
    make this expensive: without it, both requests build their own copy of a
    ~500 MB model and the machine swaps. The second caller waits a few seconds
    once, then never again.
    """
    global _whisper, _whisper_error
    if _whisper is not None:
        return _whisper
    with _whisper_lock:
        if _whisper is not None:
            return _whisper
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        _whisper = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
            cpu_threads=WHISPER_THREADS,
            download_root=os.path.join(VOICE_MODEL_DIR, "whisper"),
        )
        _whisper_error = ""
        logger.info(
            "Whisper %s (%s/%s) loaded in %.1fs",
            WHISPER_MODEL,
            WHISPER_DEVICE,
            WHISPER_COMPUTE,
            time.perf_counter() - started,
        )
        return _whisper


def transcribe(
    audio: bytes | Any,
    *,
    language: str | None = None,
    hint: str = "",
) -> Transcript | None:
    """`audio` as words, or None when faster-whisper is not installed.

    `hint` is passed as Whisper's `initial_prompt` and it is worth more than it
    looks: names Akansha already knows -- "Amma", "Akansha", "Kukatpally" --
    stop coming back as the nearest English word once the decoder has seen them
    spelled correctly in context.

    `language=None` means autodetect, which is the right default here. Pinning
    `en` is what makes a Telugu-English sentence come back as nonsense, and
    pinning `te` does the same in the other direction.
    """
    if not whisper_installed():
        return None
    import numpy as np

    samples = audio if not isinstance(audio, (bytes, bytearray)) else decode_audio(bytes(audio))
    samples = np.asarray(samples, dtype=np.float32)
    started = time.perf_counter()
    segments, info = _whisper_model().transcribe(
        samples,
        language=language or None,
        initial_prompt=hint or None,
        beam_size=int(os.getenv("AKANSHA_WHISPER_BEAM", "1")),
        vad_filter=True,
        vad_parameters=_options() if vad_available() else None,
        condition_on_previous_text=False,
    )
    collected = tuple(
        TranscriptSegment(float(s.start), float(s.end), (s.text or "").strip())
        for s in segments
    )
    return Transcript(
        text=" ".join(s.text for s in collected if s.text).strip(),
        language=str(getattr(info, "language", "") or ""),
        language_confidence=float(getattr(info, "language_probability", 0.0) or 0.0),
        segments=collected,
        duration_s=float(getattr(info, "duration", len(samples) / SAMPLE_RATE)),
        latency_ms=(time.perf_counter() - started) * 1000.0,
        model=WHISPER_MODEL,
    )


# --- Speech ------------------------------------------------------------------
#
# Piper handles English. It is not a replacement for edge-tts, it is the fast
# path: `/api/voice/tts` tries here first and falls back to the network when the
# reply is Telugu or code-switched, because there is no Piper voice that says a
# Telugu clause without mangling it. Choosing per utterance rather than per
# install is the whole point -- the common case gets 30 ms and the hard case
# still sounds right.


@dataclass(frozen=True)
class Speech:
    wav: bytes
    sample_rate: int
    latency_ms: float
    engine: str = ENGINE_PIPER
    voice: str = ""

    @property
    def seconds(self) -> float:
        #: 16-bit mono, so two bytes a frame, minus the 44-byte RIFF header.
        return max(0.0, (len(self.wav) - 44) / 2 / max(1, self.sample_rate))


_piper_lock = threading.Lock()
_piper: Any = None


def piper_voice_path() -> str:
    """The .onnx for :data:`PIPER_VOICE`, or "" when it has not been fetched.

    Checked as a file rather than trusted from config: an absent voice is the
    normal state of a fresh clone, and the fix is a documented one-line download,
    not a crash on the first spoken reply.
    """
    candidate = os.path.join(VOICE_MODEL_DIR, f"{PIPER_VOICE}.onnx")
    return candidate if os.path.isfile(candidate) else ""


def piper_installed() -> bool:
    try:
        import piper  # noqa: F401
    except Exception:
        return False
    return bool(piper_voice_path())


def piper_ready() -> bool:
    return _piper is not None


#: Anything outside Latin-1 means a script Piper's English phonemiser will spell
#: out letter by letter -- Telugu, Devanagari, CJK. Those go to edge-tts.
def _is_latin(text: str) -> bool:
    return all(ord(character) < 0x250 for character in text)


def speakable_by_piper(text: str) -> bool:
    """Whether Piper should be trusted with this particular sentence."""
    return bool(text.strip()) and _is_latin(text) and piper_installed()


def _piper_model() -> Any:
    global _piper
    if _piper is not None:
        return _piper
    with _piper_lock:
        if _piper is not None:
            return _piper
        from piper import PiperVoice

        path = piper_voice_path()
        if not path:
            raise RuntimeError(f"No Piper voice at {VOICE_MODEL_DIR}/{PIPER_VOICE}.onnx")
        started = time.perf_counter()
        _piper = PiperVoice.load(path)
        logger.info("Piper %s loaded in %.2fs", PIPER_VOICE, time.perf_counter() - started)
        return _piper


def synthesize(text: str, *, rate: float = 1.0, volume: float = 1.0) -> Speech | None:
    """`text` as a wav, or None when Piper cannot or should not say it.

    None covers both "not installed" and "not this sentence", because the caller
    does the same thing in either case: fall through to edge-tts. Distinguishing
    them would only invite a caller to treat one as an error.
    """
    cleaned = (text or "").strip()
    if not speakable_by_piper(cleaned):
        return None
    from piper import SynthesisConfig

    started = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        _piper_model().synthesize_wav(
            cleaned,
            handle,
            # Piper's `length_scale` is duration, so it runs opposite to rate:
            # 0.5 would be twice as slow, not twice as fast.
            syn_config=SynthesisConfig(
                length_scale=1.0 / max(0.5, min(rate, 2.0)),
                volume=max(0.1, min(volume, 2.0)),
                normalize_audio=True,
            ),
        )
    payload = buffer.getvalue()
    return Speech(
        wav=payload,
        sample_rate=getattr(_piper_model().config, "sample_rate", 22_050),
        latency_ms=(time.perf_counter() - started) * 1000.0,
        voice=PIPER_VOICE,
    )


# --- Reporting and warm-up ---------------------------------------------------


def capabilities() -> list[Capability]:
    """What the local stack can do, phrased so a missing piece names its own fix."""
    voice_path = piper_voice_path()
    return [
        Capability(
            name="hearing",
            installed=whisper_installed(),
            ready=whisper_ready(),
            model=WHISPER_MODEL if whisper_installed() else "",
            detail=(
                f"Whisper {WHISPER_MODEL} on {WHISPER_DEVICE}/{WHISPER_COMPUTE}"
                if whisper_installed()
                else "Not installed — `pip install faster-whisper` to hear without Chrome."
            ),
        ),
        Capability(
            name="turn_taking",
            installed=vad_available(),
            ready=_vad_options is not None,
            model=ENGINE_SILERO if vad_available() else "",
            detail=(
                "Silero VAD v6, bundled with faster-whisper — no separate download."
                if vad_available()
                else "Not installed — turn-taking is scoring words only, not audio."
            ),
        ),
        Capability(
            name="speech",
            installed=piper_installed(),
            ready=piper_ready(),
            model=PIPER_VOICE if voice_path else "",
            detail=(
                f"Piper {PIPER_VOICE}, local, no network"
                if voice_path
                else "No voice model — `python -m piper.download_voices "
                f"{PIPER_VOICE} --data-dir {VOICE_MODEL_DIR}`"
            ),
        ),
    ]


def warm(*, hearing: bool = True, speech: bool = True) -> dict[str, Any]:
    """Load the models now, so nobody's first sentence pays for it.

    Measured, and the reason this function is not optional: the first call to
    `/api/voice/stt` took 35 s wall against 2.7 s of actual Whisper work. The
    other 32 s were one-time costs -- CTranslate2 building the model, PyAV
    opening its first container, and numba JIT-compiling librosa's internals for
    the voiceprint (5.15 s on the first call, 0.00 s on every one after). Every
    one of those is paid here instead, at startup, by choice.

    Never raises. A machine that cannot load Whisper should still boot, serve
    text chat and say so in `capabilities()`; refusing to start over a missing
    optional model would turn an upgrade into an outage.
    """
    report: dict[str, Any] = {}
    if hearing and whisper_installed():
        started = time.perf_counter()
        try:
            _whisper_model()
            report["hearing"] = {"loaded": True, "ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            report["hearing"] = {"loaded": False, "error": str(exc)}
    if vad_available():
        try:
            _options()
            report["turn_taking"] = {"loaded": True}
        except Exception as exc:
            report["turn_taking"] = {"loaded": False, "error": str(exc)}
    if speech and piper_installed():
        started = time.perf_counter()
        try:
            _piper_model()
            report["speech"] = {"loaded": True, "ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            report["speech"] = {"loaded": False, "error": str(exc)}
    report["voiceprint"] = _warm_voiceprint()
    return report


def _warm_voiceprint() -> dict[str, Any]:
    """Trigger librosa's numba compilation on throwaway audio.

    Two seconds of noise, not a real recording: the point is to compile the code
    path, and nothing about the result is kept. Skipped silently when librosa is
    absent, because the voice factor already reports itself `unavailable` then.
    """
    started = time.perf_counter()
    try:
        import numpy as np

        from .owner_verify import voiceprint_from_samples

        noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE * 2) * 0.05).astype("float32")
        made = voiceprint_from_samples(noise, SAMPLE_RATE) is not None
        return {"loaded": made, "ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"loaded": False, "error": str(exc)}
