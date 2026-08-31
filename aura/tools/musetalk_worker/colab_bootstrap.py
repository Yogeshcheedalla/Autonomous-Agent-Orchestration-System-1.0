"""
Bring a GPU runtime to the point where `worker.py --engine musetalk` can run.

Three things in the MuseTalk path do not work out of the box. Each one is
invisible on this repo's development machine, because `MuseTalkEngine.__post_init__`
raises on its CUDA check before reaching any of them — so none of them show up in
the test suite, and all three would surface only on the first GPU run:

1. **`Avatar` has no `inference_stream`.** `MuseTalkEngine.render` calls it;
   upstream MuseTalk only has `inference()`, which renders every frame to
   `tmp/%08d.png` and then runs two ffmpeg passes to mux an mp4. That is useless
   for a talking head: the first frame would arrive after the whole sentence had
   finished rendering, and the browser needs frame 0 while the audio is still
   starting. `patch_avatar()` adds the generator variant.

2. **`scripts.realtime_inference` expects module globals that only its
   `__main__` block creates.** It reads `vae`, `unet`, `pe`, `whisper`,
   `audio_processor`, `timesteps`, `weight_dtype`, `fp`, `device` and `args` as
   globals. `worker.py` *imports* `Avatar` from it, which never executes that
   block, so every one of those names is undefined and the first render dies with
   `NameError`. `init_runtime()` builds them and installs them into that module's
   namespace, which is the only place `Avatar`'s methods look.

3. **`Avatar.init()` is interactive.** It calls `input()` when asked to prepare an
   avatar whose directory already exists, and `sys.exit()` when told not to
   prepare one that does not. Either one kills a headless worker — the first
   hangs it forever on a stdin read that will never be answered.
   `prepare_avatar()` does the preparation once, up front, with `input` stubbed
   to a fixed answer.

There is also a **layout mismatch** worth knowing about: `MuseTalkEngine._prepared()`
checks `results/avatars/<id>`, which is MuseTalk's *v1* path. v15 uses
`results/<version>/avatars/<id>`. Under v15 `_prepared()` would answer False
forever, so the engine would pass `preparation=True` on every start and hit the
`input()` in point 3. `prepare_avatar()` reports the path it actually used so the
caller can see which layout is live.

None of this is exercised by the test suite and none of it has been run here —
there is no CUDA device on the development machine. Treat the version adaptation
below as best-effort against an upstream that moves: it probes signatures rather
than assuming them, and raises with the offending signature rather than failing
obscurely three frames into a render.
"""

from __future__ import annotations

import copy
import inspect
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("colab_bootstrap")

MUSETALK_REPO = "https://github.com/TMElyralab/MuseTalk.git"


# — install ---------------------------------------------------------------


def clone_musetalk(root: str = "/content/MuseTalk", ref: Optional[str] = None) -> Path:
    """Clone MuseTalk if it is not already there. Returns the checkout path."""
    path = Path(root)
    if not (path / "scripts" / "realtime_inference.py").exists():
        subprocess.run(["git", "clone", "--depth", "1", MUSETALK_REPO, str(path)], check=True)
        if ref:
            subprocess.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", ref], check=True)
            subprocess.run(["git", "-C", str(path), "checkout", ref], check=True)
    else:
        log.info("MuseTalk already present at %s", path)
    return path


def download_weights(root: str = "/content/MuseTalk") -> None:
    """
    Run MuseTalk's own weight downloader.

    Preferred over a hand-written list of URLs: the set of checkpoints has
    changed between releases, and a stale hardcoded list fails as a missing-file
    error deep inside model loading rather than here.
    """
    path = Path(root)
    for candidate in ("download_weights.sh", "scripts/download_weights.sh"):
        script = path / candidate
        if script.exists():
            subprocess.run(["bash", str(script)], cwd=str(path), check=True)
            return
    raise FileNotFoundError(
        f"No download_weights.sh under {path}. Check the MuseTalk README for the "
        "current weight-fetch step; this bootstrap deliberately does not hardcode "
        "checkpoint URLs."
    )


# — module globals (breaking point 2) -------------------------------------


def _call_adapting(fn: Callable[..., Any], **candidates: Any) -> Any:
    """
    Call `fn` with whichever of `candidates` its signature actually accepts.

    MuseTalk's loader helpers gained and renamed keyword arguments between v1 and
    v15. Passing the union blindly raises `TypeError`; probing lets one bootstrap
    serve both. Anything the signature does not name is dropped rather than
    guessed at.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**candidates)
    accepted = {k: v for k, v in candidates.items() if k in params}
    return fn(**accepted)


def init_runtime(root: str = "/content/MuseTalk", version: str = "v1") -> Any:
    """
    Build the globals `Avatar` reads, and install them into its module.

    Returns the `scripts.realtime_inference` module with its namespace populated.
    Call this before constructing any `Avatar`.
    """
    root_path = str(Path(root).resolve())
    if root_path not in sys.path:
        sys.path.insert(0, root_path)
    # MuseTalk resolves `./models`, `./results` and config paths relative to the
    # process working directory, not to the module. Running the worker from
    # anywhere else silently produces "checkpoint not found".
    os.chdir(root_path)

    import torch
    from transformers import WhisperModel

    from musetalk.utils.audio_processor import AudioProcessor
    from musetalk.utils.blending import get_image_blending, get_image_prepare_material
    from musetalk.utils.face_parsing import FaceParsing
    from musetalk.utils.utils import datagen, load_all_model

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        raise RuntimeError(
            "init_runtime needs a CUDA device. On Colab: Runtime → Change runtime "
            "type → T4 GPU. MuseTalk on CPU renders at 1-2 fps, which is minutes "
            "of compute per sentence."
        )
    weight_dtype = torch.float16

    loaded = _call_adapting(
        load_all_model,
        unet_model_path="./models/musetalkV15/unet.pth" if version == "v15" else "./models/musetalk/pytorch_model.bin",
        unet_config="./models/musetalkV15/musetalk.json" if version == "v15" else "./models/musetalk/musetalk.json",
        vae_type="sd-vae",
        device=device,
    )
    # v1 returns (audio_processor, vae, unet, pe); v15 returns (vae, unet, pe).
    # Discriminated by length rather than by version string, because the version
    # flag is the caller's claim and this is the actual object.
    if len(loaded) == 4:
        _legacy_audio, vae, unet, pe = loaded
    else:
        vae, unet, pe = loaded

    # Half precision on all three, matching MuseTalk's own __main__ setup. Skip
    # it and the unet gets float32 latents against float16 weights.
    pe = pe.half().to(device)
    vae.vae = vae.vae.half().to(device)
    unet.model = unet.model.half().to(device)

    audio_processor = _call_adapting(
        AudioProcessor,
        feature_extractor_path="./models/whisper",
    )
    whisper = WhisperModel.from_pretrained("./models/whisper")
    whisper = whisper.to(device=device, dtype=weight_dtype).eval()
    whisper.requires_grad_(False)

    fp = _call_adapting(
        FaceParsing,
        left_cheek_width=90,
        right_cheek_width=90,
    )

    module = sys.modules.get("scripts.realtime_inference")
    if module is None:
        import scripts.realtime_inference as module  # type: ignore

    # `args` is an argparse.Namespace in MuseTalk's __main__. Avatar reads
    # .version, .extra_margin, .parsing_mode, .audio_padding_length_left/right
    # and .skip_save_images off it. A namespace with those fields is all it needs.
    from argparse import Namespace

    module.args = Namespace(
        version=version,
        extra_margin=10,
        parsing_mode="jaw",
        audio_padding_length_left=2,
        audio_padding_length_right=2,
        skip_save_images=True,
        fps=25,
        batch_size=4,
    )
    module.device = device
    module.weight_dtype = weight_dtype
    module.vae = vae
    module.unet = unet
    module.pe = pe
    module.whisper = whisper
    module.audio_processor = audio_processor
    module.fp = fp
    module.timesteps = torch.tensor([0], device=device)
    module.datagen = datagen
    module.get_image_blending = get_image_blending
    module.get_image_prepare_material = get_image_prepare_material
    log.info("musetalk runtime ready on %s (version=%s)", device, version)
    return module


# — the streaming generator (breaking point 1) ----------------------------


def patch_avatar(module: Any) -> None:
    """
    Add `Avatar.inference_stream`, the generator `MuseTalkEngine.render` calls.

    This is `inference()` with the file-output half removed. Upstream overlaps GPU
    work with PNG encoding by handing frames to a worker thread through a
    `queue.Queue`; yielding makes both unnecessary, so the thread and the queue
    are gone and the compositing is inlined.

    Two deliberate differences from upstream's `process_frames`:

    - It yields all `n` frames. `process_frames` stops at `self.idx >= video_len - 1`
      and so drops the last one, which upstream can afford because a video is one
      frame short and nobody notices. Here the browser sizes its buffer from
      `meta.frames` and a missing final frame leaves the mouth frozen open on the
      last syllable of every sentence.

    - It counts in a local rather than `self.idx`. The worker serves one utterance
      at a time per connection, but `self.idx` is instance state on an `Avatar`
      shared by every connection, so a second speaker would resume mid-cycle
      through the first one's head pose.
    """
    import cv2
    import numpy as np
    import torch

    Avatar = module.Avatar

    if hasattr(Avatar, "inference_stream"):
        log.info("Avatar.inference_stream already present; leaving it alone")
        return

    @torch.no_grad()
    def inference_stream(self: Any, audio_path: str, fps: float = 25.0) -> Iterator[Any]:
        args = module.args
        features, librosa_length = module.audio_processor.get_audio_feature(
            audio_path, weight_dtype=module.weight_dtype
        )
        chunks = module.audio_processor.get_whisper_chunk(
            features,
            module.device,
            module.weight_dtype,
            module.whisper,
            librosa_length,
            fps=fps,
            audio_padding_length_left=args.audio_padding_length_left,
            audio_padding_length_right=args.audio_padding_length_right,
        )

        # Frame count is driven purely by audio length — which is exactly the
        # property `meta.frames` on the wire relies on.
        total = len(chunks)
        emitted = 0

        for whisper_batch, latent_batch in module.datagen(
            chunks, self.input_latent_list_cycle, self.batch_size
        ):
            cond = module.pe(whisper_batch.to(module.device))
            latents = latent_batch.to(device=module.device, dtype=module.unet.model.dtype)
            pred = module.unet.model(latents, module.timesteps, encoder_hidden_states=cond).sample
            recon = module.vae.decode_latents(pred.to(device=module.device, dtype=module.vae.vae.dtype))

            for res_frame in recon:
                if emitted >= total:
                    return
                index = emitted
                emitted += 1

                bbox = self.coord_list_cycle[index % len(self.coord_list_cycle)]
                # deepcopy because `get_image_blending` composites onto the frame
                # and the cycle list is reused on every pass through.
                base = copy.deepcopy(self.frame_list_cycle[index % len(self.frame_list_cycle)])
                mask = self.mask_list_cycle[index % len(self.mask_list_cycle)]
                mask_box = self.mask_coords_list_cycle[index % len(self.mask_coords_list_cycle)]

                x1, y1, x2, y2 = bbox
                try:
                    patch = cv2.resize(
                        np.asarray(res_frame).astype("uint8"), (int(x2 - x1), int(y2 - y1))
                    )
                except Exception:
                    # Upstream swallows this too. A degenerate bbox on one frame
                    # must not abort the sentence; the browser skips a missing
                    # index rather than desyncing, which is why frames carry one.
                    continue
                yield module.get_image_blending(base, patch, bbox, mask, mask_box)

    Avatar.inference_stream = inference_stream
    log.info("patched Avatar.inference_stream")


# — avatar preparation (breaking point 3) ---------------------------------


def prepare_avatar(
    module: Any,
    avatar_image: str,
    avatar_id: str = "akansha",
    bbox_shift: int = 0,
    batch_size: int = 4,
) -> dict[str, Any]:
    """
    Prepare the avatar once, non-interactively, and report where it landed.

    Preparation — face parsing and VAE latent caching — is MuseTalk's slow step,
    not inference. Doing it here rather than on the first utterance is the
    difference between a warm worker and a first sentence that arrives a minute
    late.

    `input` is stubbed for the duration because `Avatar.init()` prompts when the
    avatar directory already exists. Unstubbed in a headless worker that is not a
    prompt, it is a permanent hang on a stdin read nobody will answer.
    """
    import builtins

    if not Path(avatar_image).exists():
        raise FileNotFoundError(f"avatar image not found: {avatar_image}")

    version = getattr(module.args, "version", "v1")
    expected = (
        Path("results") / version / "avatars" / avatar_id
        if version == "v15"
        else Path("results") / "avatars" / avatar_id
    )

    original_input = builtins.input
    # "n" = do not rebuild an avatar that is already prepared; "c" = continue
    # when the stored bbox_shift differs. Both are the non-destructive answer.
    builtins.input = lambda *_a, **_k: "n"  # type: ignore[assignment]
    try:
        avatar = module.Avatar(
            avatar_id=avatar_id,
            video_path=avatar_image,
            bbox_shift=bbox_shift,
            batch_size=batch_size,
            preparation=not expected.is_dir(),
        )
    finally:
        builtins.input = original_input  # type: ignore[assignment]

    frames = len(getattr(avatar, "frame_list_cycle", []) or [])
    if not frames:
        raise RuntimeError(
            f"Avatar prepared but frame_list_cycle is empty at {expected}. "
            "Nothing will render. Usually means face detection found no face in "
            f"{avatar_image} — try a larger, front-facing crop."
        )

    v1_path = Path("results") / "avatars" / avatar_id
    return {
        "avatar_id": avatar_id,
        "version": version,
        "path": str(expected),
        "frames": frames,
        # `MuseTalkEngine._prepared()` only ever looks at the v1 path. Under v15
        # it answers False forever, so the engine passes preparation=True on every
        # start and walks into the `input()` above. Surfacing the mismatch here
        # beats debugging a silent hang on the first connection.
        "worker_prepared_check_passes": v1_path.is_dir(),
    }


def bootstrap(
    avatar_image: str,
    root: str = "/content/MuseTalk",
    avatar_id: str = "akansha",
    version: str = "v1",
    ref: Optional[str] = None,
    skip_weights: bool = False,
) -> dict[str, Any]:
    """Clone, fetch weights, build the runtime, patch, prepare. One call."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    clone_musetalk(root, ref=ref)
    if not skip_weights:
        download_weights(root)
    module = init_runtime(root, version=version)
    patch_avatar(module)
    info = prepare_avatar(module, avatar_image, avatar_id=avatar_id)
    log.info("bootstrap complete: %s", info)
    return info
