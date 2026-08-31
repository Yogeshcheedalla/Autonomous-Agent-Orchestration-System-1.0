# MuseTalk worker — the photoreal face

This is the GPU half of the talking-head pipeline. It exists because the machine
running Akansha has no CUDA device: `nvidia-smi` is absent, `torch` is not
installed, and 12 CPU cores render MuseTalk at roughly 1–2 fps, which is minutes
of compute per sentence. So the face model runs somewhere with a GPU — a free
Colab T4 is enough — and streams finished frames back over a WebSocket.

```
browser ── /ws/avatar/{session} ──▶ FastAPI relay ── ws:// ──▶ worker ──▶ MuseTalk
   ▲                                                                        │
   └──────────────── audio + JPEG frames ◀──────────────────────────────────┘
```

The backend is a relay and never renders a pixel. It owns the TTS voice, the one
shared link to the GPU, and the decision to fall back to the CSS rig.

## Two engines, one transport

| `--engine`  | What it is                            | Needs CUDA |
| ----------- | ------------------------------------- | ---------- |
| `musetalk`  | The real audio-driven face model      | yes        |
| `portrait`  | A **transport stand-in**, not a model | no         |

`portrait` exists so the wire protocol, frame ordering, A/V metadata,
cancellation and fallback can all be tested on a laptop — which is what
`backend/tests/test_avatar_stream.py` does. It darkens an ellipse between the
lips by audio envelope. It is not a face model, it will never look like a person
talking, and it logs a warning at startup saying so. Do not ship it as the face.

## Running it on Colab

Open [`colab_musetalk_worker.ipynb`](colab_musetalk_worker.ipynb) in Colab, set
the runtime to **T4 GPU** (Runtime → Change runtime type), and run the cells top
to bottom. The last cell prints a `wss://…` URL.

Then point the backend at it — from the machine running Akansha:

```bash
curl -X POST http://127.0.0.1:8000/api/avatar/worker -H "Content-Type: application/json" -d "{\"url\":\"wss://PASTE-THE-TUNNEL-URL\",\"avatar\":\"akansha\"}"
```

The response tells you immediately whether the URL works:

```json
{ "worker": "connected", "model": "musetalk", "fps": 25.0, "avatars": ["akansha"] }
```

Check it any time with `GET /api/avatar/status`. There is nothing to configure in
the browser — the voice page opens `/ws/avatar/{session}` on load, is told
`worker: "absent"` until a URL is registered, and starts drawing neural frames
the moment one is.

**The tunnel URL changes every time the notebook restarts.** That is why the URL
is a runtime setting rather than an environment variable, and why "no worker" is
a normal state rather than an error: a Colab runtime is reclaimed after a few
hours of use or ~90 minutes idle, and when it dies the assistant must keep
talking. It does — mid-sentence, on the CSS rig, with the same audio.

## Running it locally (only worth it with a real GPU)

```bash
python tools/musetalk_worker/worker.py --engine musetalk --musetalk-root /path/to/MuseTalk --avatar-image public/assets/images/akansha-presence.webp --avatar-id akansha --port 8765
```

Then `POST /api/avatar/worker` with `{"url": "ws://127.0.0.1:8765"}`.

To exercise the transport without a GPU:

```bash
python tools/musetalk_worker/worker.py --engine portrait --avatar-image public/assets/images/akansha-presence.webp --port 8765
```

## Wire protocol

There are **two** links, and they are not the same protocol. The browser↔backend
one is documented in `backend/avatar/api.py`; the one below is backend↔worker,
which is what this directory implements. Version 1. Text frames are JSON; binary
frames are `uint32be index ‖ JPEG`.

```
→ {"type":"hello","protocol":1}
← {"type":"ready","protocol":1,"model":"musetalk","fps":25.0,"avatars":["akansha"]}

→ {"type":"speak","id":"u7","format":"mp3","audio":"<base64>","avatar":"akansha"}
← {"type":"meta","id":"u7","fps":25.0,"width":512,"height":512,"frames":38}
← <uint32be 0 ‖ JPEG> … <uint32be 37 ‖ JPEG>
← {"type":"end","id":"u7","frames":38}

→ {"type":"cancel","id":"u7"}        no reply; the stream just stops early
← {"type":"error","id":"u7","message":"…"}
```

Note the base64 audio field is `audio` here, but `b64` on the browser↔backend
socket. Mixing them up is the first thing to check if a worker returns
`audio decode:` errors.

Four decisions worth knowing, because they are not arbitrary:

**Frames are binary, not base64 in JSON.** Base64 inflates a 512×512 JPEG by a
third; at 25 fps that is ~200 KB/s of pure overhead across a Colab tunnel that is
already the bottleneck.

**Every frame carries its index.** The browser seeks by
`floor(currentTime × fps)`, so a frame that arrives late is *skipped* rather than
shifting every frame after it. Without an explicit index a single dropped frame
would desync the rest of the sentence.

**Cancellation is worker-side, and stops the inference.** The engine is consumed
as a lazy generator with the cancel flag checked between frames, so barge-in
stops the GPU part way through a sentence. Rendering the whole utterance up front
and then cancelling the *send* loop would look identical from the browser while
leaving MuseTalk finishing a sentence nobody will hear — and on a shared free T4
that stolen time is the next utterance's latency.

**`meta.frames` is an estimate.** It is derived from audio duration
(`ceil(samples ÷ (sr ÷ fps))`) and sent as soon as the first frame exists, rather
than after the render finishes — otherwise the browser would wait for the whole
utterance before drawing anything. The browser sizes its buffer from it and grows
it if a later index overshoots. `end.frames` is the exact count.

## Avatar preparation

MuseTalk's slow step is preparation — face parsing and latent caching — not
inference. The worker does it once per `--avatar-id`, writes it under
`results/avatars/<id>`, and skips it on every subsequent start. Preparing during
the first utterance instead would make that sentence arrive a minute late.

## Limits, stated plainly

- **MuseTalk itself is untested in this repo.** There is no CUDA device on the
  development machine, so `MuseTalkEngine.__post_init__` raises by design and its
  test is skipped. Everything *around* the model — framing, ordering, duration
  matching, fallback, cancellation, base64 validation — is covered by 10 passing
  tests. Two of those pin the properties that make barge-in real rather than
  cosmetic: that a `cancel` stops the render partway through, and that the socket
  keeps answering during one. Both failed silently before they were written.
- One still photograph of the portrait woman exists. MuseTalk animates the mouth
  region of a source image; it does not invent head turns or gaze shifts. Those
  still come from the CSS rig, which is why the rig remains mounted underneath.
- A free T4 shared across a Colab session will not always hit 25 fps. The browser
  drops frames rather than delaying audio, so the failure mode is a slightly
  choppier face, not a desynced one.
