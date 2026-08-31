'use client';

import { apiUrl } from '@/lib/apiBase';

/**
 * Her voice, in one place.
 *
 * This exists because the app had two speech engines with two different voices.
 * The voice page synthesised through `/api/voice/tts`; the chat page built its
 * own `SpeechSynthesisUtterance` and picked whatever Windows voice matched a
 * name list (Samantha, Heera, Neerja...). Both were correctly arbitrated so they
 * never overlapped, so this never sounded like two people talking at once — it
 * sounded like the *same assistant being a different person* depending on which
 * page you were on, which is worse, because it is not obviously a bug and so it
 * never gets reported as one.
 *
 * A persona with a name only holds together if the voice is part of the identity
 * rather than a property of the screen you happen to be looking at.
 *
 * Deliberately a plain function and not a hook: the chat page needs it from
 * inside an existing `useCallback` that already owns its own playback state, and
 * wrapping it in a hook would have meant duplicating that state machine.
 */

/** What the TTS route understands. Mirrors the body `useVoice` already sends. */
export type AkanshaLanguageMode = 'english' | 'telugu' | 'hindi' | 'mixed';

export interface AkanshaSpeechOptions {
  voiceGender?: 'female' | 'male';
  voiceTone?: 'friendly' | 'professional' | 'energetic' | 'calm';
  languageMode?: AkanshaLanguageMode;
}

/**
 * Map a BCP-47 tag to the mode the TTS route expects.
 *
 * The chat page detects a speech language as `te-IN`/`hi-IN`/`en-IN` for the
 * browser synthesiser, which wants a locale; the server route wants a language
 * name. Without this the chat page would ask for `te-IN` and silently get the
 * English default back.
 */
export function languageModeFromTag(tag: string | undefined | null): AkanshaLanguageMode {
  const normalized = (tag || '').toLowerCase();
  if (normalized.startsWith('te')) return 'telugu';
  if (normalized.startsWith('hi')) return 'hindi';
  return 'english';
}

/**
 * Fetch one utterance as audio, or `null` if the server cannot produce it.
 *
 * `null` rather than a throw, because every caller's correct response to a
 * failure is the same — fall back to the browser voice — and an exception would
 * make that the exceptional path instead of the ordinary one. A caller that
 * gets `null` has heard nothing yet and is free to fall back; a caller whose
 * audio has already started must not.
 */
export async function fetchAkanshaSpeech(
  text: string,
  options: AkanshaSpeechOptions = {}
): Promise<Blob | null> {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;

  try {
    const response = await fetch(apiUrl('/api/voice/tts'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: trimmed,
        voice_gender: options.voiceGender ?? 'female',
        voice_tone: options.voiceTone ?? 'friendly',
        language_mode: options.languageMode ?? 'english',
      }),
    });

    if (!response.ok) return null;

    const blob = await response.blob();
    // A zero-length body is a failure that arrived with a 200. Treating it as
    // success would play silence and suppress the fallback, so the assistant
    // would appear to have answered and said nothing.
    return blob.size > 0 ? blob : null;
  } catch {
    return null;
  }
}
