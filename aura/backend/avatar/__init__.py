"""
avatar — photoreal talking-head streaming (MuseTalk worker).

The voice stack can already decide what to say and speak it (`edge_tts`), and
`HumanPresence` can puppet a still photograph from the viseme timeline. What it
cannot do is look like a real person filmed talking: that needs an audio-driven
neural face model, and this machine has no GPU (no CUDA, no torch), so the model
cannot run in-process.

So the model runs somewhere that *does* have a GPU — a free Colab T4 is the
intended host — and this package is the seam between it and the app:

  * `worker_client` holds one long-lived WebSocket to that worker and turns
    "here is a wav" into "here is a stream of JPEG frames";
  * `api` exposes `/ws/avatar/{session_id}` to the browser and relays, plus
    the small settings API the Colab workflow needs.

Two properties are deliberate.

**The worker is optional.** With no worker configured or reachable, the browser
is told `worker: "absent"` and falls back to the CSS rig. A missing GPU degrades
the face; it never breaks the conversation.

**The worker URL is settable at runtime.** Colab tunnel URLs change every time
the notebook restarts, so baking it into `.env` would mean editing a file and
restarting the backend several times a day. `POST /api/avatar/worker` instead.
"""

from .worker_client import (
    AvatarWorkerClient,
    WorkerFrame,
    WorkerMeta,
    WorkerUnavailable,
    get_worker_client,
    worker_settings,
)
from .api import router, set_tts_provider

__all__ = [
    "AvatarWorkerClient",
    "WorkerFrame",
    "WorkerMeta",
    "WorkerUnavailable",
    "get_worker_client",
    "worker_settings",
    "router",
    "set_tts_provider",
]
