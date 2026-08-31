'use client';

/**
 * NeuralPresence — photoreal talking head, with the filmed performance as its
 * floor.
 *
 * Phoneme-exact lipsync from a still photograph is possible and used to be what
 * this panel showed, but it is a puppet: one image warped by a viseme timeline
 * has no head turn, no eye movement and no hands. Looking like a person filmed
 * talking needs an audio-driven face model, and that needs a GPU this machine
 * does not have — so the model runs on a free Colab T4 and streams JPEG frames
 * back through `/ws/avatar/{sessionId}`.
 *
 * This component is the consumer of that stream, and the whole design turns on
 * one decision: **the audio is the clock, and this component does not own it.**
 *
 * `useVoice` already fetches `/api/voice/tts`, plays it through an `<audio>`
 * element and runs an `AnalyserNode` on it. So frames are sent to the worker as
 * `speak_audio` — the exact bytes already playing — and drawn by seeking into
 * the buffer with `getElapsedSeconds()`. Nothing here plays audio. The two
 * failure modes that follow are both survivable:
 *
 *   - a frame arrives late  → it is skipped, not queued, so audio never waits;
 *   - the worker dies       → `fallback` swaps back to the footage mid-sentence,
 *                             and the sentence finishes on film.
 *
 * A stale Colab URL is the normal case, not an exception: the tunnel changes on
 * every notebook restart. So "no worker" is a first-class state that renders the
 * footage without so much as a warning in the UI.
 */

import dynamic from 'next/dynamic';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';

import { wsUrl } from '@/lib/apiBase';

/**
 * The floor is the filmed performance, not the warped photograph.
 *
 * `HumanPresence` was the floor until a viewer reported that clicking the voice
 * interface gave them "old akansha": a single still of her, warped from the
 * viseme timeline. It is phoneme-exact, which the film is not — but it is one
 * photograph, so its head never turns, its eyes never move and its hands do not
 * exist. `BodyPresence` plays the 24fps footage of the same woman, cropped to
 * head and shoulders for this panel, so what the panel shows is a person.
 *
 * The trade is stated rather than hidden: without the MuseTalk worker her mouth
 * moves as filmed, not as the words require. It is gated to speech, so it is
 * never a figure mouthing silence — but it is not lipsync. Lipsync is what the
 * canvas below is for, and the canvas needs the GPU.
 *
 * That is also why `viseme` and `emotion` are gone from this component's props:
 * the footage is not driven by a phoneme timeline and the worker's frames are
 * driven by the audio itself, so nothing here could have honoured them.
 */
const BodyPresence = dynamic(() => import('./BodyPresence'), { ssr: false });

/** What the parent can drive from its own speech pipeline. */
export interface NeuralPresenceHandle {
  /**
   * Render a face for audio the parent is already playing.
   *
   * `getElapsedSeconds` is the clock — normally `() => audioEl.currentTime`.
   * Without it the component falls back to `performance.now()` from this call,
   * which is close but drifts on a stalled or re-buffered audio element.
   */
  speak(audio: Blob | ArrayBuffer, opts?: { id?: string; getElapsedSeconds?: () => number }): void;
  /** Barge-in. Stops the GPU, not just the drawing. */
  cancel(): void;
  /** Whether a worker is currently rendering frames. */
  isLive(): boolean;
}

interface Props {
  sessionId: string;
  isListening: boolean;
  isSpeaking: boolean;
  isThinking?: boolean;
  speakingVolume?: number;
  width?: number;
  className?: string;
  /** Told when the neural face becomes available or is lost, for UI copy. */
  onWorkerChange?: (state: 'connected' | 'absent') => void;
}

type WorkerState = 'connecting' | 'connected' | 'absent';

/** Reconnect backoff. Bounded: a missing worker must not become a busy loop. */
const RECONNECT_MS = [1000, 2000, 5000, 15000, 30000] as const;

/**
 * Frames to hold before drawing anything. One frame of lead is enough to avoid
 * a flash of the previous utterance, and more than a few would delay the face
 * behind audio that has already started.
 */
const PREROLL_FRAMES = 2;

export default forwardRef<NeuralPresenceHandle, Props>(function NeuralPresence(
  {
    sessionId,
    isListening,
    isSpeaking,
    isThinking = false,
    speakingVolume = 0,
    width = 286,
    className,
    onWorkerChange,
  },
  ref
) {
  const [worker, setWorker] = useState<WorkerState>('connecting');
  const [rendering, setRendering] = useState(false);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const closedRef = useRef(false);

  // Per-utterance state. Refs rather than state: these change at frame rate and
  // a re-render per frame would cost more than the decode.
  const framesRef = useRef<(ImageBitmap | null)[]>([]);
  const fpsRef = useRef(25);
  const utteranceRef = useRef<string | null>(null);
  const clockRef = useRef<(() => number) | null>(null);
  const startedAtRef = useRef(0);
  const drawnRef = useRef(-1);
  const rafRef = useRef<number | null>(null);
  /**
   * Gate on `meta`. Binary frames carry an index but no utterance id, so a
   * frame still in flight when barge-in happens would otherwise be written into
   * the *next* utterance's buffer at the same index — a wrong mouth shape at a
   * plausible position, which is worse than a dropped frame. Since the wire
   * order is audio → meta → frames, "meta seen since the last speak" is exactly
   * the condition that a frame belongs to the current utterance.
   */
  const acceptFramesRef = useRef(false);

  // Held in a ref so the socket effect does not list a prop in its deps. An
  // inline arrow from the parent would otherwise tear down and reconnect the
  // WebSocket on every render of the voice page.
  const onWorkerChangeRef = useRef(onWorkerChange);
  onWorkerChangeRef.current = onWorkerChange;

  const announce = useCallback((next: WorkerState) => {
    setWorker((prev) => {
      if (prev !== next && next !== 'connecting') onWorkerChangeRef.current?.(next);
      return next;
    });
  }, []);

  const releaseFrames = useCallback(() => {
    // ImageBitmaps hold GPU memory until closed; a dropped reference is a leak
    // that survives until GC decides otherwise.
    for (const bmp of framesRef.current) bmp?.close();
    framesRef.current = [];
    drawnRef.current = -1;
  }, []);

  const stopDrawing = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setRendering(false);
  }, []);

  /** Draw whichever frame the clock selects. Late frames are skipped. */
  const drawLoop = useCallback(() => {
    rafRef.current = requestAnimationFrame(drawLoop);
    const canvas = canvasRef.current;
    if (!canvas) return;

    const elapsed = clockRef.current
      ? clockRef.current()
      : (performance.now() - startedAtRef.current) / 1000;
    const wanted = Math.floor(elapsed * fpsRef.current);
    if (wanted === drawnRef.current) return;

    const frames = framesRef.current;
    // Seek backwards to the newest frame that has actually arrived, so a gap in
    // the stream holds the last good face instead of blanking.
    let index = Math.min(wanted, frames.length - 1);
    while (index >= 0 && !frames[index]) index -= 1;
    if (index < 0) return;

    const bmp = frames[index];
    if (!bmp) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
      canvas.width = bmp.width;
      canvas.height = bmp.height;
    }
    ctx.drawImage(bmp, 0, 0);
    drawnRef.current = index;
  }, []);

  const startDrawing = useCallback(() => {
    if (rafRef.current !== null) return;
    setRendering(true);
    rafRef.current = requestAnimationFrame(drawLoop);
  }, [drawLoop]);

  /**
   * End the current utterance, worker included.
   *
   * The `cancel` message is the important half: dropping the frames locally
   * would leave MuseTalk rendering a sentence nobody will ever see, and on a
   * shared free T4 that stolen GPU time is the next utterance's latency.
   */
  const endUtterance = useCallback(() => {
    const socket = wsRef.current;
    if (socket?.readyState === WebSocket.OPEN && utteranceRef.current) {
      socket.send(JSON.stringify({ type: 'cancel', id: utteranceRef.current }));
    }
    utteranceRef.current = null;
    acceptFramesRef.current = false;
    stopDrawing();
    releaseFrames();
  }, [releaseFrames, stopDrawing]);

  // — socket ---------------------------------------------------------------

  useEffect(() => {
    closedRef.current = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closedRef.current) return;
      let socket: WebSocket;
      try {
        socket = new WebSocket(wsUrl(`/ws/avatar/${encodeURIComponent(sessionId)}`));
      } catch {
        announce('absent');
        return;
      }
      socket.binaryType = 'arraybuffer';
      wsRef.current = socket;

      socket.onmessage = async (event) => {
        if (event.data instanceof ArrayBuffer) {
          // uint32be index ‖ JPEG. The index is why a dropped frame is a gap
          // rather than a desync: position is carried, not inferred.
          if (!acceptFramesRef.current) return;
          const view = new DataView(event.data);
          if (event.data.byteLength < 5) return;
          const index = view.getUint32(0, false);
          const blob = new Blob([new Uint8Array(event.data, 4)], { type: 'image/jpeg' });
          try {
            const bmp = await createImageBitmap(blob);
            // Re-check: the decode is a suspension point, so barge-in can land
            // between the gate above and this write. Closing the bitmap rather
            // than storing it keeps the leak closed on that path too.
            if (!acceptFramesRef.current) {
              bmp.close();
              return;
            }
            if (framesRef.current.length <= index) framesRef.current.length = index + 1;
            framesRef.current[index] = bmp;
            if (index >= PREROLL_FRAMES - 1) startDrawing();
          } catch {
            /* a corrupt frame is a skipped frame */
          }
          return;
        }

        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(String(event.data));
        } catch {
          return;
        }

        switch (msg.type) {
          case 'status':
            announce(msg.worker === 'connected' ? 'connected' : 'absent');
            break;
          case 'meta':
            releaseFrames();
            fpsRef.current = Number(msg.fps) || 25;
            framesRef.current = new Array(Number(msg.frames) || 0).fill(null);
            acceptFramesRef.current = true;
            break;
          case 'fallback':
            // Expected several times a day: stale tunnel, dead runtime, OOM.
            acceptFramesRef.current = false;
            announce('absent');
            stopDrawing();
            releaseFrames();
            break;
          case 'end':
            // Hold the last frame; the rig takes over on the next state change.
            acceptFramesRef.current = false;
            break;
          case 'error':
            acceptFramesRef.current = false;
            stopDrawing();
            break;
          default:
            break;
        }
      };

      socket.onopen = () => {
        attemptRef.current = 0;
      };

      socket.onclose = () => {
        wsRef.current = null;
        stopDrawing();
        announce('absent');
        if (closedRef.current) return;
        const delay = RECONNECT_MS[Math.min(attemptRef.current, RECONNECT_MS.length - 1)];
        attemptRef.current += 1;
        reconnectTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => {
        announce('absent');
      };
    };

    connect();

    return () => {
      closedRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      stopDrawing();
      releaseFrames();
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [sessionId, announce, releaseFrames, startDrawing, stopDrawing]);

  // — imperative surface ---------------------------------------------------

  useImperativeHandle(
    ref,
    () => ({
      speak(audio, opts) {
        const socket = wsRef.current;
        if (!socket || socket.readyState !== WebSocket.OPEN || worker !== 'connected') return;
        const id = opts?.id ?? `u${Date.now().toString(36)}`;
        endUtterance();
        utteranceRef.current = id;
        clockRef.current = opts?.getElapsedSeconds ?? null;
        startedAtRef.current = performance.now();

        const send = (buf: ArrayBuffer) => {
          let binary = '';
          const bytes = new Uint8Array(buf);
          // Chunked so a long utterance does not blow the argument limit that
          // String.fromCharCode(...bytes) would hit on a few hundred KB.
          for (let i = 0; i < bytes.length; i += 0x8000) {
            binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          }
          socket.send(
            JSON.stringify({ type: 'speak_audio', id, format: 'mp3', b64: btoa(binary) })
          );
        };

        if (audio instanceof Blob) {
          void audio.arrayBuffer().then(send);
        } else {
          send(audio);
        }
      },
      cancel() {
        endUtterance();
      },
      isLive() {
        return worker === 'connected' && rendering;
      },
    }),
    [worker, rendering, endUtterance]
  );

  // Stop the moment speech ends, so a held frame does not stare — and so a
  // barge-in stops the worker rather than only the drawing.
  useEffect(() => {
    if (!isSpeaking) endUtterance();
  }, [isSpeaking, endUtterance]);

  // — render ---------------------------------------------------------------
  //
  // The rig stays mounted underneath whenever the neural face is not actually
  // drawing. Unmounting it would mean a blank panel during the gap between
  // "worker connected" and "first frame decoded", which is exactly when the
  // assistant is starting to speak and most obviously alive.

  const showCanvas = worker === 'connected' && rendering;

  const state = isSpeaking
    ? 'speaking'
    : isThinking
      ? 'thinking'
      : isListening
        ? 'listening'
        : 'idle';

  /** The state ring the warped-still renderer used to draw for itself. */
  const RING: Record<typeof state, string> = {
    speaking: 'shadow-[0_0_90px_-18px_rgba(0,201,167,0.55)] ring-accent/40',
    listening: 'shadow-[0_0_90px_-18px_rgba(108,71,255,0.55)] ring-primary/40',
    thinking: 'shadow-[0_0_90px_-18px_rgba(245,158,11,0.45)] ring-amber-400/40',
    idle: 'shadow-[0_0_70px_-30px_rgba(108,71,255,0.35)] ring-white/10',
  };

  return (
    <div className={className} style={{ position: 'relative', width, lineHeight: 0 }}>
      {/* 2:3, which is the source's portrait crop, so the footage fills the panel
          without a letterbox and without stretching. */}
      <div
        className={`relative overflow-hidden rounded-[2rem] bg-slate-950 ring-1 transition-shadow duration-500 ${RING[state]}`}
        style={{ width, height: width * 1.5, visibility: showCanvas ? 'hidden' : 'visible' }}
        role="img"
        aria-label={`Akansha is ${state}`}
      >
        <BodyPresence
          isListening={isListening}
          isSpeaking={isSpeaking}
          isThinking={isThinking}
          speakingVolume={speakingVolume}
          framing="portrait"
        />
      </div>
      <canvas
        ref={canvasRef}
        aria-hidden={!showCanvas}
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          borderRadius: '2rem',
          opacity: showCanvas ? 1 : 0,
          // Slow enough to hide a decode hiccup, fast enough not to cross-fade
          // through a visibly doubled face on every utterance.
          transition: 'opacity 140ms linear',
          pointerEvents: 'none',
        }}
      />
    </div>
  );
});
