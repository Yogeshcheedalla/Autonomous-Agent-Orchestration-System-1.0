/**
 * Wake-phrase detection for "hey Akansha".
 *
 * Deliberately fuzzy on the name, because the Web Speech API almost never
 * returns "akansha" verbatim. Measured mishearings from this project's own
 * recogniser include "akanksha", "akansa", "akasha", "akansh", "a kansha" and
 * "aakansha" -- and that is with a clean microphone. A literal
 * `/hey akansha/` test rejects every one of them, which is the difference
 * between a wake word that works and one that looks like it does.
 *
 * So the name is matched by edit distance rather than equality, and the
 * greeting in front of it is optional: people say "akansha, open chrome" as
 * often as "hey akansha, open chrome".
 *
 * Pure and dependency-free on purpose -- it is the one piece of the voice path
 * that can be unit-tested without a microphone, and the tests in
 * `backend/test_audit_regressions.py` exercise it through the compiled output.
 */

/** The name we are listening for, already normalised. */
const WAKE_NAME = 'akansha';

/**
 * Optional greetings that may precede the name. `hai` and `ఏయ్` are here
 * because this app is used in English and Telugu and both are common openers;
 * `ok`/`okay` because users trained on other assistants reach for them.
 */
const GREETINGS = new Set([
  'hey',
  'hay',
  'hi',
  'hii',
  'hello',
  'helo',
  'ok',
  'okay',
  'okey',
  'yo',
  'hai',
  'hey there',
  'ఏయ్',
  'హే',
  'अरे',
  'हे',
]);

/**
 * Maximum edit distance from `akansha` for a token to count as the name.
 *
 * Two, not one: "akanchaa" and "aakanshaa" are both distance 2 and both are
 * real transcriptions of the name. Not three -- at three the neighbourhood
 * starts to include ordinary words, and a wake word that fires on ordinary
 * speech is worse than one that misses, because it hijacks the microphone
 * mid-sentence.
 */
const MAX_NAME_DISTANCE = 2;

/**
 * Shortest token allowed to be considered as the name at all.
 *
 * Without this, distance-2 matching against a 7-letter target would accept
 * 5-letter noise. Every genuine mishearing observed is at least 6 characters,
 * so this costs nothing and removes the whole class of short-word false
 * positives.
 */
const MIN_NAME_LENGTH = 6;

export interface WakeWordMatch {
  /** Whether the wake phrase was present. */
  matched: boolean;
  /**
   * Whatever the user said *after* the wake phrase, already trimmed.
   *
   * Empty when they only called the name and stopped -- which is the common
   * case and must be distinguishable from a command, because the right
   * response to a bare "hey Akansha" is to answer, not to try to execute "".
   */
  remainder: string;
  /** The token that matched the name, for diagnostics. Empty when no match. */
  matchedToken: string;
  /**
   * Whatever was said *before* the wake phrase, already trimmed.
   *
   * Carried here rather than recomputed by callers because the obvious way to
   * recover it -- `normalized.indexOf(matchedToken)` -- is wrong for the
   * two-token forms: "a kansha" reports a `matchedToken` of "akansha", which
   * never appears literally in the normalised text, so `indexOf` returns -1 and
   * `slice(0, -1)` silently yields the whole string minus its last character
   * instead of an empty prefix.
   */
  leading: string;
}

const NO_MATCH: WakeWordMatch = { matched: false, remainder: '', matchedToken: '', leading: '' };

/**
 * Levenshtein distance, with early exit once the best possible result exceeds
 * `limit`. The limit is not an optimisation here so much as a guard: this runs
 * on every interim recognition result, several times a second.
 */
function editDistanceWithin(a: string, b: string, limit: number): number {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > limit) return limit + 1;

  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);

  for (let i = 1; i <= a.length; i += 1) {
    const current = [i];
    let rowMinimum = i;
    for (let j = 1; j <= b.length; j += 1) {
      const substitution = (previous[j - 1] ?? 0) + (a[i - 1] === b[j - 1] ? 0 : 1);
      const insertion = (current[j - 1] ?? 0) + 1;
      const deletion = (previous[j] ?? 0) + 1;
      const best = Math.min(substitution, insertion, deletion);
      current[j] = best;
      if (best < rowMinimum) rowMinimum = best;
    }
    // Every remaining row can only add to the minimum, so if the whole row is
    // already past the limit the answer is too.
    if (rowMinimum > limit) return limit + 1;
    previous = current;
  }

  return previous[b.length] ?? limit + 1;
}

/**
 * Lowercase, strip punctuation, collapse whitespace. Keeps Indic scripts.
 *
 * `\p{M}` is in the keep-set alongside letters and numbers, and it has to be:
 * viramas and dependent vowel signs are combining marks, not letters, so a
 * letters-only filter turns "ఏయ్" into "ఏయ" and "హే" into "హ". Every Telugu and
 * Hindi greeting in `GREETINGS` is written with its marks intact, so dropping
 * them meant none of those four entries could ever match -- the bilingual
 * support the list documents was inert.
 */
export function normalizeForWakeWord(text: string): string {
  return (text || '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\p{M}\s]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function isName(token: string): boolean {
  if (token.length < MIN_NAME_LENGTH) return false;
  return editDistanceWithin(token, WAKE_NAME, MAX_NAME_DISTANCE) <= MAX_NAME_DISTANCE;
}

/**
 * Look for the wake phrase anywhere in `text`.
 *
 * Anywhere, not just at the start: with continuous recognition the transcript
 * routinely opens with a fragment of whatever the user was saying before they
 * addressed the assistant, and requiring a prefix match threw those turns away.
 */
export function matchWakeWord(text: string): WakeWordMatch {
  const normalized = normalizeForWakeWord(text);
  if (!normalized) return NO_MATCH;

  const tokens = normalized.split(' ');

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i] ?? '';

    // The name as one token: "akansha", "akanksha", "hey akansha ...".
    if (isName(token)) {
      return {
        matched: true,
        remainder: tokens.slice(i + 1).join(' '),
        matchedToken: token,
        leading: tokens.slice(0, i).join(' '),
      };
    }

    // The name split across two tokens: "a kansha", "aa kansha". The recogniser
    // does this often enough that ignoring it loses real wake-ups, and joining
    // is safe because the joined form still has to pass the distance test.
    //
    // Not when the first token is a greeting, though. "ok" + "akansha" joins to
    // "okakansha", which is edit distance 2 from the name and therefore passed --
    // so the greeting was swallowed into `matchedToken` and "ok akansha" stopped
    // being recognised as a bare call, while "hey akansha" still was (only
    // because "heyakansha" is three characters too long to survive the distance
    // limit). Two spellings of the same utterance behaving differently is the
    // bug; the greeting is a separate word and stays one.
    const next = tokens[i + 1];
    if (next && !GREETINGS.has(token)) {
      const joined = token + next;
      if (isName(joined)) {
        return {
          matched: true,
          remainder: tokens.slice(i + 2).join(' '),
          matchedToken: joined,
          leading: tokens.slice(0, i).join(' '),
        };
      }
    }
  }

  return NO_MATCH;
}

/**
 * Whether `text` is *only* the wake phrase (with an optional greeting), i.e. the
 * user called the assistant and stopped.
 *
 * Separate from `matchWakeWord` because the correct behaviour differs: a bare
 * call should be acknowledged and the floor handed back, while a call with a
 * command attached should execute it without a chatty "yes?" in between.
 */
export function isBareWakeCall(text: string): boolean {
  const match = matchWakeWord(text);
  if (!match.matched) return false;
  if (match.remainder) return false;
  if (!match.leading) return true;
  // Anything in front of the name must be a greeting, or the user was mid-
  // sentence and the name was incidental rather than an address.
  return match.leading.split(' ').every((word) => GREETINGS.has(word));
}

/**
 * Strip a leading wake phrase from a command.
 *
 * Used on the way into the intent router: "hey akansha open chrome" must reach
 * it as "open chrome", or the wake words become part of the command and the
 * router tries to find an app called "hey".
 */
export function stripWakeWord(text: string): string {
  const match = matchWakeWord(text);
  return match.matched ? match.remainder : text.trim();
}
