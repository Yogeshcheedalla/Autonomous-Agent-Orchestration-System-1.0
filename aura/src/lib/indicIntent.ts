'use client';

/**
 * A Latin view of Telugu and Devanagari text, for intent matching only.
 *
 * The mirror of `backend/indic_intent.py`, and it exists for a defect that lived
 * entirely in the browser. `isAutomationIntent` in ./automationCommands decides
 * which endpoint a message goes to, and every pattern it tests is Latin -- so
 * `"ఓపెన్ యూట్యూబ్"` ("open youtube") was classified as ordinary chat and never
 * reached `/api/automation/browser/prompt` at all. The backend had already grown its
 * own Latin view for the same reason, but a message that goes to the chat endpoint
 * never gets near it. The user's transcript is the whole story:
 *
 *     హే హాయ్          -> "Ha, continue cheyyi. Nenu context hold chesthunnanu."
 *     ఓపెన్ యూట్యూబ్    -> "Sare, ardham ayyindi. Ippudu exact ga em cheyyalo cheppu."
 *     I said to open the YouTube  -> ✅ Opened YouTube.
 *
 * Two words in Telugu did nothing; the same command in English worked. That is a
 * whole-language outage in a product offered in EN / TE / HI, and the fix has to be
 * in front of the routing decision rather than behind it.
 *
 * The tables are NOT duplicated here. Both implementations read
 * `aura/shared/indic_intent_tables.json`, because 97 words and 94 skeletons copied
 * into a second language is a copy that drifts silently in the direction of whoever
 * edited last. Only the ~40-line walk below is mirrored, and it is mirrored exactly.
 *
 * This produces a *matching view*, never display text and never model input. What
 * the user said goes to the screen and to the model verbatim -- transliterating
 * someone's words and reading them back is its own kind of rudeness.
 */

import tables from '../../shared/indic_intent_tables.json';

const CONSONANTS: Record<string, string> = tables.consonants;
const VOWELS: Record<string, string> = tables.vowels;
const MATRAS: Record<string, string> = tables.matras;
const MARKS: Record<string, string> = tables.marks;
const VIRAMAS = new Set<string>(tables.viramas);
const WORDS: Record<string, string> = tables.words;
const SKELETONS: Record<string, string> = tables.skeletons;

const RANGES = `${tables.teluguRange[0]}-${tables.teluguRange[1]}${tables.devanagariRange[0]}-${tables.devanagariRange[1]}`;
const INDIC = new RegExp(`[${RANGES}]`, 'u');
const INDIC_WORD = new RegExp(`[${RANGES}]+`, 'gu');

export function hasIndic(text: string): boolean {
  return INDIC.test(text || '');
}

/**
 * A phonetic ASCII skeleton for one Indic word.
 *
 * The inherent vowel is the only subtlety, and it is the reason this is a walk and
 * not a lookup: a bare consonant carries an `a` (`ప` is "pa"), which a matra
 * replaces and a virama cancels -- so `ఓపెన్` walks out as `oopen` rather than
 * `oopena`, which is what makes step 3 of `latinWord` land on `open` as often as it
 * does.
 *
 * Retroflex and dental collapse to the same Latin letter and the three Telugu
 * sibilants all become `s`. That is deliberate: the target is an English keyword,
 * and someone saying "start" writes `స్టార్ట్` or `ష్టార్ట్` interchangeably.
 */
export function transliterate(word: string): string {
  const out: string[] = [];
  let oweA = false;
  for (const char of word || '') {
    if (char in CONSONANTS) {
      if (oweA) out.push('a');
      out.push(CONSONANTS[char]);
      oweA = true;
    } else if (char in MATRAS) {
      out.push(MATRAS[char]);
      oweA = false;
    } else if (VIRAMAS.has(char)) {
      oweA = false;
    } else if (char in VOWELS) {
      if (oweA) {
        out.push('a');
        oweA = false;
      }
      out.push(VOWELS[char]);
    } else if (char in MARKS) {
      if (oweA) {
        out.push('a');
        oweA = false;
      }
      out.push(MARKS[char]);
    } else if (char >= '0' && char <= '9') {
      if (oweA) {
        out.push('a');
        oweA = false;
      }
      out.push(char);
    }
    // Anything else in the block -- ZWJ, avagraha, editorial marks -- carries no
    // sound and is dropped rather than guessed at.
  }
  if (oweA) out.push('a');
  return out.join('');
}

/**
 * `yuutyuub` -> `yutyub`, `oopen` -> `open`.
 *
 * Doubled vowels are an artefact of length marks that English spelling does not
 * have, so collapsing runs is what closes the gap between a skeleton and the word it
 * is trying to be.
 */
export function squeeze(skeleton: string): string {
  return (skeleton || '').replace(/(.)\1+/gu, '$1');
}

/** One Indic word as the closest English token, by the three-step lookup. */
export function latinWord(word: string): string {
  // 1. the Indic word itself -- native commands like `తెరువు` (open) and `खोलो`,
  //    where a phonetic skeleton would be unrecognisable.
  if (word in WORDS) return WORDS[word];
  const skeleton = transliterate(word);
  const squeezed = squeeze(skeleton);
  // 2. its skeleton -- English loanwords, where speakers and STT engines disagree
  //    about the spelling (`ఓపెన్`, `ఒపెన్`, `ఓపన్` all arrive). The trailing
  //    inherent vowel is the Devanagari case: `यूट्यूब` carries no final virama, so
  //    it walks out as `yutyuba` where Telugu's `యూట్యూబ్` gives `yutyub`, and both
  //    have to reach the same entry.
  for (const key of [skeleton, squeezed, squeezed.replace(/a+$/u, '') || squeezed]) {
    if (key in SKELETONS) return SKELETONS[key];
  }
  // 3. an unrecognised word still becomes an ASCII token rather than disappearing,
  //    so word counts and "is this a fragment" stay honest.
  return squeezed;
}

/**
 * `"ఓపెన్ యూట్యూబ్"` -> `"open youtube"`. Latin runs are left exactly as typed.
 *
 * Mixed input is the common case in practice and it is why this substitutes per word
 * rather than translating the sentence: `"Chrome లో ఓపెన్ చేయి"` keeps its English
 * half untouched and gains a readable Telugu half, so one set of patterns covers all
 * three languages instead of three sets drifting apart.
 */
export function intentView(text: string): string {
  if (!text || !INDIC.test(text)) return text || '';
  return text.replace(INDIC_WORD, (match) => latinWord(match));
}
