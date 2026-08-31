'use client';

import { intentView } from './indicIntent';

const AUTOMATION_TRIGGER_PATTERNS = [
  /\bopen\b/i,
  /\bplay\b/i,
  /\bsearch\b/i,
  /\brun\b/i,
  /\bclose\b/i,
  /\bdelete\b/i,
  /\bremove\b/i,
  /\bclear\b/i,
  /\bscroll\b/i,
  /\bfill\b/i,
  /\benter\b/i,
  /\bcomplete\b/i,
  /\bedit\b/i,
  /\btype\b/i,
  /\bwrite\b/i,
  /\bclick\b/i,
  /\bsubmit\b/i,
  /\bselect\b/i,
  /\bcopy\b/i,
  /\bpaste\b/i,
  /\bcut\b/i,
  /\bundo\b/i,
  /\bredo\b/i,
  /\bsave\b/i,
  /\blaunch\b/i,
  /\bstart\b/i,
  /\bgo to\b/i,
  /\bincrease\b/i,
  /\bdecrease\b/i,
  /\bturn\b/i,
  /\bset\b/i,
  /\bmute\b/i,
  /\bunmute\b/i,
  /\bstop\b/i,
  /\bpause\b/i,
  /\bresume\b/i,
  /\bnext\b/i,
  /\bprevious\b/i,
  /\bskip\b/i,
  /\bwait\b/i,
  // Window and pointer verbs that were missing entirely. Every one of these was
  // measured falling through to the language model, which then *describes* doing
  // the thing instead of doing it -- `minimize chrome` had the target but no
  // trigger, so the pair test could never fire. British spellings included because
  // the app is used in English and Telugu and `maximise` is the common form here.
  /\bminimi[sz]e\b/i,
  /\bmaximi[sz]e\b/i,
  /\brestore\b/i,
  /\bswitch\b/i,
  /\bpress\b/i,
  /\bhover\b/i,
  /\bdrag\b/i,
  /\bdrop\b/i,
  /\bfocus\b/i,
  /\brefresh\b/i,
  /\breload\b/i,
  /\bzoom\b/i,
  /\btoggle\b/i,
  /\bexpand\b/i,
  /\bcollapse\b/i,
  /\brename\b/i,
  /\bdownload\b/i,
  /\bupload\b/i,
  /\battach\b/i,
  /\bscreenshot\b/i,
  /\bcapture\b/i,
  /\bcheck\b/i,
  /\buncheck\b/i,
  /\bnavigate\b/i,
  /\bvisit\b/i,
  /\bsend\b/i,
  /\bscreen\s?shot\b/i,
];

const AUTOMATION_TARGET_PATTERNS = [
  /\byoutube\b/i,
  /\bgoogle\b/i,
  /\bchrome\b/i,
  /\bbrave\b/i,
  /\bedge\b/i,
  /\bcodechef\b/i,
  /\blinkedin\b/i,
  /\binstagram\b/i,
  /\bdiscord\b/i,
  /\btelegram\b/i,
  /\bwhatsapp\b/i,
  /\bdownloads?\b/i,
  /\bdesktop\b/i,
  /\bdocuments?\b/i,
  /\bnotepad\b/i,
  /\bcalculator\b/i,
  /\bexplorer\b/i,
  /\bpowershell\b/i,
  /\bcmd\b/i,
  /\bfile\b/i,
  /\bfolder\b/i,
  /\bpath\b/i,
  /\bwebsite\b/i,
  /\bpage\b/i,
  /\bup\b/i,
  /\bdown\b/i,
  /\bform\b/i,
  /\bfields?\b/i,
  /\bdetails?\b/i,
  /\bsubmit\b/i,
  /\bthis\b/i,
  /\ball\b/i,
  /\bselection\b/i,
  /\bcontent\b/i,
  /\btext\b/i,
  /\bdraft\b/i,
  /\bhistory\b/i,
  /\baccount\b/i,
  /\beverything\b/i,
  /\bactive\b/i,
  /\bpresent\b/i,
  /\bcurrent\b/i,
  /\btab\b/i,
  /\bvolume\b/i,
  /\bsound\b/i,
  /\bsong\b/i,
  /\bsongs\b/i,
  /\btrack\b/i,
  /\btracks\b/i,
  /\bvideo\b/i,
  /\bvideos\b/i,
  /\bresult\b/i,
  /\bresults\b/i,
  /\bmedia\b/i,
  /\bmusic\b/i,
  /\bmovie\b/i,
  /\bmovies\b/i,
  /\bbrightness\b/i,
  /\bwindow\b/i,
  /\bscreen\b/i,
  // UI nouns the pair test had no way to see. `click the submit button` matched only
  // because `submit` happens to be in both lists; `click on the login link` missed,
  // because `link` was in neither. Deliberately excludes bare `code`, `email` and
  // `message`: those pair with the existing `write` trigger and would route "write
  // code for me" to the automation endpoint instead of answering it.
  /\blinks?\b/i,
  /\bbuttons?\b/i,
  /\bicons?\b/i,
  /\bbox\b/i,
  /\bmenu\b/i,
  /\bdropdown\b/i,
  /\bcheckbox\b/i,
  /\boptions?\b/i,
  /\bkeys?\b/i,
  /\bkeyboard\b/i,
  /\bmouse\b/i,
  /\bapps?\b/i,
  /\bapplication\b/i,
  /\bimages?\b/i,
  /\bscreen\s?shot\b/i,
  /\bhere\b/i,
  /\bthere\b/i,
  /\bsearch\s?(?:bar|box)\b/i,
  /\baddress\s?bar\b/i,
  /\btoolbar\b/i,
  /\btaskbar\b/i,
  /\bsidebar\b/i,
  /\bdialog\b/i,
  /\bpopup\b/i,
  /\bmodal\b/i,
  /\bnotifications?\b/i,
  /\bsettings\b/i,
  /\bprofile\b/i,
  // Named applications, so `switch to vs code` and `open outlook` resolve.
  /\bvs\s?code\b/i,
  /\bvscode\b/i,
  /\bterminal\b/i,
  /\bexcel\b/i,
  /\boutlook\b/i,
  /\bgmail\b/i,
  /\bslack\b/i,
  /\bspotify\b/i,
  /\bteams\b/i,
  /\bfigma\b/i,
  /\bfirefox\b/i,
  /\bsafari\b/i,
  /\bvlc\b/i,
];

const ARTIFACT_GENERATION_PATTERNS = [
  /\b(generate|create|make|export|build|prepare)\b.*\b(pdf|pptx?|powerpoints?|presentations?|slides?|excel|xlsx|spreadsheet|csv|json|docx|word document|document|png|jpe?g|jpc|image|zip|all formats|all file formats|all types of files?)\b/i,
  /\b(pdf|pptx?|powerpoints?|presentations?|slides?|excel|xlsx|spreadsheet|csv|json|docx|word document|document|png|jpe?g|jpc|image|zip|all formats|all file formats|all types of files?)\b.*\b(generate|create|make|export|build|prepare)\b/i,
  /\b(study plan|formula sheet|notes|quiz|flashcards?|invoice|report)\b.*\b(pdf|docx|pptx?|excel|xlsx|csv|json|zip)\b/i,
];

const DIRECT_ACTIVE_WINDOW_COMMAND_PATTERNS = [
  /^\s*(?:single\s+)?click(?:\s+(?:this|that|there|here|current|selected|active|button|link|item))?\s*$/i,
  /^\s*double\s+click(?:\s+(?:this|that|there|here|current|selected|active|button|link|item))?\s*$/i,
  /^\s*(?:play\s+pause|play\/pause|pause|resume)\s*$/i,
  /^\s*(?:scroll|page)\s+(?:up|down)(?:\s+(?:slowly|fast|one by one|by\s+\d+|for\s+\d+|\d+(?:\.\d+)?\s*(?:cm|centimeters?|centimetres?|mm|millimeters?|millimetres?|in|inch|inches|px|pixels?)))?\s*$/i,
  /^\s*scroll\s+(?:\d+(?:\.\d+)?\s*(?:cm|centimeters?|centimetres?|mm|millimeters?|millimetres?|in|inch|inches|px|pixels?)|little|small|slowly|fast|quickly|more|half\s+(?:page|screen)|one\s+page|full\s+page)\s*(?:up|down)?\s*$/i,
  /^\s*(?:smart\s+scroll|scroll\s+smart)(?:\s+(?:for|on|in|to|through)\s+[\w\s]+)?\s*$/i,
  /^\s*(?:normal\s+scroll|medium\s+scroll)\s*$/i,
  /^\s*(?:open\s+)?(?:new\s+tab|tab\s+new)\s*$/i,
  /^\s*close\s+(?:this\s+|current\s+|present\s+)?(?:tab|window|app)\s*$/i,
  // Standalone forms that carry no target noun for the pair test to find, so they
  // need an exact match to route at all. `press enter` is the clearest case: `press`
  // is a trigger and `enter` is *also* only a trigger, so the pair test could never
  // satisfy both halves from those two words.
  /^\s*press\s+(?:and\s+hold\s+)?(?:the\s+)?[\w+\s-]{1,30}?(?:\s+key)?\s*$/i,
  /^\s*(?:hit|tap)\s+(?:the\s+)?[\w+\s-]{1,30}?(?:\s+key)?\s*$/i,
  /^\s*(?:right|middle)[\s-]?click\b.*$/i,
  /^\s*(?:take|grab|capture)\s+(?:a\s+|the\s+)?screen\s?shot\b.*$/i,
  /^\s*screen\s?shot\s*$/i,
  /^\s*(?:minimi[sz]e|maximi[sz]e|restore)\b.*$/i,
  /^\s*switch\s+(?:to|between)\b.*$/i,
  /^\s*(?:refresh|reload)\b.*$/i,
  /^\s*(?:go\s+)?(?:back|forward)\s*$/i,
  /^\s*zoom\s+(?:in|out)\b.*$/i,
  /^\s*select\s+all\s*$/i,
];

export function normalizeAutomationPrompt(text: string) {
  // The Latin view first, so everything below can stay Latin. Every rewrite in this
  // function and every pattern above is `\b`-anchored English, so a Telugu or Hindi
  // command arrived with nothing to match and `isAutomationIntent` sent it to the
  // chat model instead of the automation route -- `ఓపెన్ యూట్యూబ్` was answered
  // "tell me exactly what to do" while the same words in English opened YouTube.
  //
  // Safe to apply to the value this function returns, and only because of what that
  // value is used for: the caller keeps the raw text for the transcript and for the
  // model, and sends this to `/api/automation/browser/prompt`, whose job is to parse
  // a command rather than to read it back to anyone. This function already rewrote
  // that payload heavily -- "desktop chrome" becomes "open chrome in the desktop
  // app" -- so a Latin view of it is the same kind of change, not a new one.
  const normalized = intentView(text)
    // Word order, once the view has done its job. Telugu and Hindi put the verb
    // last and both use a light verb to carry it: `యూట్యూబ్ ఓపెన్ చేయి` is
    // literally "youtube open do", and `చేయి`/`चालू करो` is that "do". Measured in
    // the browser, that is exactly what the view produced -- correct word for word,
    // and not a command any pattern below can read.
    //
    // So: drop the light verb when a real one is already present, then move a
    // trailing verb to the front. English is unharmed because these two shapes do
    // not occur in it -- "youtube open" is not how anyone types it, and the cases
    // that do match ("google search" -> "search google") mean the same thing either
    // way.
    .replace(
      /^(.*?\b(?:open|play|search|close|send|show|start|stop|type|write|click|delete|scroll|refresh|reload|download)\b.*?)\s+do(?:\s+it)?\s*$/i,
      '$1'
    )
    .replace(
      /^\s*([\w\s.'-]+?)\s+(open|play|search|close|send|show|start|stop|type|write|click|delete)\s*$/i,
      '$2 $1'
    )
    .replace(/\bdsktop\b/gi, 'desktop')
    .replace(/\bdesk top\b/gi, 'desktop')
    .replace(/\bwebiste\b/gi, 'website')
    .replace(/\bwebs?te\b/gi, 'website')
    .replace(/\bsite app\b/gi, 'website')
    .replace(/\bwebsite app\b/gi, 'website')
    .replace(/\bapp version\b/gi, 'desktop app')
    .replace(/\bdesktop side\b/gi, 'desktop app')
    .replace(/\bwebsite side\b/gi, 'website')
    .replace(/\bweb side\b/gi, 'website')
    .replace(/^(desktop|desktop app|desktop application)\s+(.+)$/i, 'open $2 in the desktop app')
    .replace(/^(website|web|browser|site)\s+(.+)$/i, 'open $2 in the web browser')
    .replace(
      /^(desktop|desktop app|desktop application)\s+(open|launch|start|use)\s+(.+)$/i,
      'open $3 in the desktop app'
    )
    .replace(
      /^(website|web|browser|site)\s+(open|launch|start|use)\s+(.+)$/i,
      'open $3 in the web browser'
    )
    .replace(/^open\s+(.+?)\s+desktop$/i, 'open $1 in the desktop app')
    .replace(/^open\s+(.+?)\s+website$/i, 'open $1 in the web browser')
    .replace(/^open\s+(.+?)\s+web$/i, 'open $1 in the web browser')
    .replace(/\bweb\s+site\b/gi, 'website')
    .replace(/\bwebsite version\b/gi, 'website')
    .replace(/\bweb version\b/gi, 'website')
    .replace(/\bbrowser version\b/gi, 'website')
    .replace(/\bdesktop version\b/gi, 'desktop app')
    .replace(/\bdesktop client\b/gi, 'desktop app')
    .replace(/\bweb client\b/gi, 'website')
    .replace(/\bin\s+website\b/gi, 'in the web browser')
    .replace(/\bon\s+website\b/gi, 'in the web browser')
    .replace(/\bin\s+web\b/gi, 'in the web browser')
    .replace(/\bin\s+desktop\b/gi, 'in the desktop app')
    .replace(/\bwhats?\s*up\b/gi, 'whatsapp')
    .replace(/\bwats?\s*up\b/gi, 'whatsapp')
    .replace(/\bwhats?\s*ap+p?\b/gi, 'whatsapp')
    .replace(/\bwhats\s+app\b/gi, 'whatsapp')
    .replace(/\bwhatsup\b/gi, 'whatsapp')
    .replace(/\bwhatsup\b/gi, 'whatsapp')
    .replace(/\bwhastapp\b/gi, 'whatsapp')
    .replace(/\bmicro\s*soft edge\b/gi, 'microsoft edge')
    .replace(/\bmicrosoftedge\b/gi, 'microsoft edge')
    .replace(/\bms edge\b/gi, 'microsoft edge')
    .replace(/\bedge browser\b/gi, 'microsoft edge')
    .replace(/\bmicrosoft edge browser\b/gi, 'microsoft edge')
    .replace(/\bedge app\b/gi, 'microsoft edge')
    .replace(/\bmicrosoft edge app\b/gi, 'microsoft edge')
    .replace(/\bchrome browser\b/gi, 'google chrome')
    .replace(/\bchrome app\b/gi, 'google chrome')
    .replace(/\bgoogle chrome app\b/gi, 'google chrome')
    .replace(
      /\bop(?:en)?\s*(notepad|calculator|calc|file explorer|explorer|vscode|visual studio code|chrome|brave|edge|microsoft edge|whatsapp|telegram|discord|word|excel|powerpoint|settings|terminal|control panel|youtube|google|codechef|linkedin|instagram|twitter|x)\b/gi,
      'open $1'
    )
    .replace(/\bdesktop\s+open\b/gi, 'open')
    .replace(/\bwebsite\s+open\b/gi, 'open')
    .replace(/\bweb\s+open\b/gi, 'open')
    .replace(/\byoutub\b/gi, 'youtube')
    .replace(/\bvarsham songs?\b/gi, 'Varsham songs')
    .replace(/\bcaland(ar|er)?\b/gi, 'calendar')
    .replace(/\s+/g, ' ')
    .trim();

  if (
    /\bplay\b/i.test(normalized) &&
    /\b(song|songs|music|movie|movies)\b/i.test(normalized) &&
    !/\byoutube\b/i.test(normalized)
  ) {
    return `open youtube and ${normalized}`;
  }

  return normalized;
}

export function isAutomationIntent(text: string) {
  const normalized = normalizeAutomationPrompt(text);
  if (ARTIFACT_GENERATION_PATTERNS.some((pattern) => pattern.test(normalized))) {
    return false;
  }
  if (DIRECT_ACTIVE_WINDOW_COMMAND_PATTERNS.some((pattern) => pattern.test(normalized))) {
    return true;
  }
  const hasTrigger = AUTOMATION_TRIGGER_PATTERNS.some((pattern) => pattern.test(normalized));
  const hasTarget = AUTOMATION_TARGET_PATTERNS.some((pattern) => pattern.test(normalized));
  return hasTrigger && hasTarget;
}
