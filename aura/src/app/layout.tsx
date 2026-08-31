import React from 'react';
import type { Metadata, Viewport } from 'next';
import { Geist, JetBrains_Mono } from 'next/font/google';
import Script from 'next/script';
import '../styles/tailwind.css';

import PlannerReminderBridge from '../components/planner/PlannerReminderBridge';
import SmartSearchWindow from '../components/search/SmartSearchWindow';

/**
 * Fonts are self-hosted through `next/font` rather than imported in CSS.
 *
 * `src/styles/tailwind.css` used to open with
 * `@import url('https://fonts.googleapis.com/css2?family=Geist...')`, which is
 * the worst place to request a font: the browser has to download and parse the
 * stylesheet before it discovers the `@import`, then make a second trip to
 * fonts.googleapis.com, which itself returns another stylesheet pointing at
 * fonts.gstatic.com — three serial round trips to a third party, all of them
 * blocking first paint.
 *
 * These declarations inline the `@font-face` rules into the build, add
 * `<link rel="preload">` for the files, and expose each family as a CSS
 * variable that `tailwind.config.js` and the critical shell CSS below both
 * read. Both families are variable fonts, so a weight range costs one file
 * rather than the five the old query string asked for.
 */
const geistSans = Geist({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-sans',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-mono',
});
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export const metadata: Metadata = {
  title: 'Akansha — Multi-Model AI Chat with Persistent Memory',
  description:
    'Akansha is a multi-model AI chat platform with persistent memory, RAG document support, prompt libraries, and voice I/O — built for developers and power users.',
  icons: {
    icon: [{ url: '/favicon.ico', type: 'image/x-icon' }],
  },
};

/**
 * First-paint shell, inlined in <head> so the app frame is not unstyled while
 * Next's stylesheet is still in flight.
 *
 * Everything below sits inside `@layer critical` for a load-bearing reason. This
 * <style> lands *after* Next's CSS <link> in document order, and the rules here
 * duplicate ~90 Tailwind utilities at the same specificity — so before layering,
 * every one of them beat Tailwind's own copy. Base utilities are identical so
 * that was invisible, but responsive variants carry no extra specificity, which
 * silently killed `md:`/`lg:`/`xl:`/`2xl:` overrides of every property named
 * here, across the whole app: `flex-col xl:flex-row` never turned into a row,
 * `hidden md:block` stayed hidden, `p-4 lg:p-8` stayed at 4.
 *
 * An unlayered declaration always outranks a layered one regardless of order, so
 * a single layer inverts that: during first paint these rules are the only ones
 * present and apply normally; the moment Tailwind's sheet arrives it wins
 * everything, variants included. Do not remove the layer, and do not "fix" a
 * broken responsive class by adding another rule here.
 */
const criticalShellCss = `
@layer critical {
html,body{margin:0;min-height:100%;background:hsl(240 10% 6%);color:hsl(0 0% 94%);font-family:var(--font-sans),system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}
a{color:inherit}
button,input,textarea,select{font:inherit}
button{cursor:pointer}
.flex{display:flex}.grid{display:grid}.hidden{display:none}.fixed{position:fixed}.relative{position:relative}.absolute{position:absolute}
.h-screen{height:100vh}.h-full{height:100%}.w-full{width:100%}.min-w-0{min-width:0}.flex-1{flex:1 1 0%}.shrink-0{flex-shrink:0}.overflow-hidden{overflow:hidden}.overflow-y-auto{overflow-y:auto}
.items-center{align-items:center}.items-start{align-items:flex-start}.justify-between{justify-content:space-between}.justify-center{justify-content:center}.flex-col{flex-direction:column}.flex-wrap{flex-wrap:wrap}
.gap-1{gap:.25rem}.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-4{gap:1rem}.space-y-1>*+*{margin-top:.25rem}.space-y-2>*+*{margin-top:.5rem}.space-y-3>*+*{margin-top:.75rem}.space-y-4>*+*{margin-top:1rem}
.p-1{padding:.25rem}.p-2{padding:.5rem}.p-3{padding:.75rem}.p-4{padding:1rem}.p-6{padding:1.5rem}.px-2{padding-left:.5rem;padding-right:.5rem}.px-3{padding-left:.75rem;padding-right:.75rem}.px-4{padding-left:1rem;padding-right:1rem}.py-1{padding-top:.25rem;padding-bottom:.25rem}.py-2{padding-top:.5rem;padding-bottom:.5rem}.py-3{padding-top:.75rem;padding-bottom:.75rem}
.mt-1{margin-top:.25rem}.mt-2{margin-top:.5rem}.mt-3{margin-top:.75rem}.mt-4{margin-top:1rem}.mt-6{margin-top:1.5rem}.mb-2{margin-bottom:.5rem}.mb-4{margin-bottom:1rem}.mx-auto{margin-left:auto;margin-right:auto}
.rounded-md{border-radius:.375rem}.rounded-lg{border-radius:.5rem}.rounded-xl{border-radius:.75rem}.rounded-2xl{border-radius:1rem}.rounded-3xl{border-radius:1.5rem}.rounded-full{border-radius:9999px}
.border{border-width:1px;border-style:solid}.border-r{border-right-width:1px;border-right-style:solid}.border-b{border-bottom-width:1px;border-bottom-style:solid}.border-white\\/10{border-color:rgba(255,255,255,.1)}.border-border{border-color:hsl(240 8% 16%)}
.bg-background{background:hsl(240 10% 6%)}.bg-card{background:hsl(240 10% 9%)}.bg-muted{background:hsl(240 8% 14%)}.bg-slate-950{background:#020617}.bg-slate-900{background:#0f172a}.bg-\\[hsl\\(var\\(--sidebar-bg\\)\\)\\]{background:hsl(240 10% 7%)}
.text-foreground{color:hsl(0 0% 95%)}.text-muted-foreground{color:hsl(240 5% 65%)}.text-white{color:#fff}.text-slate-100{color:#f1f5f9}.text-slate-200{color:#e2e8f0}.text-slate-300{color:#cbd5e1}.text-slate-400{color:#94a3b8}.text-xs{font-size:.75rem;line-height:1rem}.text-sm{font-size:.875rem;line-height:1.25rem}.text-base{font-size:1rem;line-height:1.5rem}.text-lg{font-size:1.125rem;line-height:1.75rem}.text-2xl{font-size:1.5rem;line-height:2rem}.text-3xl{font-size:1.875rem;line-height:2.25rem}.font-medium{font-weight:500}.font-semibold{font-weight:600}.font-bold{font-weight:700}.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.transition-colors{transition-property:color,background-color,border-color,text-decoration-color,fill,stroke;transition-duration:.15s}.transition-all{transition-property:all;transition-duration:.15s}
@media (min-width:1024px){.lg\\:relative{position:relative}.lg\\:hidden{display:none}.lg\\:flex{display:flex}.lg\\:translate-x-0{transform:translateX(0)}}
}
`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${jetbrainsMono.variable}`}
      suppressHydrationWarning
    >
      <head>
        <style
          id="akansha-critical-shell-css"
          dangerouslySetInnerHTML={{ __html: criticalShellCss }}
        />
        <Script
          id="akansha-clean-extension-hydration-attrs"
          strategy="beforeInteractive"
          dangerouslySetInnerHTML={{
            __html: `
(function () {
  var PREFIX = 'rtrvr-';
  var stopAt = Date.now() + 12000;

  function cleanElement(element) {
    if (!element || !element.attributes) return;
    for (var index = element.attributes.length - 1; index >= 0; index -= 1) {
      var name = element.attributes[index].name;
      if (name && name.indexOf(PREFIX) === 0) {
        element.removeAttribute(name);
      }
    }
  }

  function cleanTree(root) {
    if (!root) return;
    if (root.nodeType === 1) cleanElement(root);
    if (!document.createTreeWalker) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    var node = walker.nextNode();
    while (node) {
      cleanElement(node);
      node = walker.nextNode();
    }
  }

  function cleanDocument() {
    cleanTree(document.documentElement);
  }

  cleanDocument();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', cleanDocument, { once: true });
  }
  window.addEventListener('load', cleanDocument, { once: true });

  if (!window.MutationObserver || !document.documentElement) return;
  var observer = new MutationObserver(function (mutations) {
    if (Date.now() > stopAt) {
      cleanDocument();
      observer.disconnect();
      return;
    }
    for (var i = 0; i < mutations.length; i += 1) {
      var mutation = mutations[i];
      if (mutation.type === 'attributes') {
        cleanElement(mutation.target);
      }
      if (mutation.addedNodes) {
        for (var j = 0; j < mutation.addedNodes.length; j += 1) {
          cleanTree(mutation.addedNodes[j]);
        }
      }
    }
  });
  observer.observe(document.documentElement, {
    attributes: true,
    childList: true,
    subtree: true
  });
  window.setTimeout(function () {
    cleanDocument();
    observer.disconnect();
  }, 12000);
})();
          `,
          }}
        />
      </head>
      <body suppressHydrationWarning>
        {children}
        <PlannerReminderBridge />
        <SmartSearchWindow />
      </body>
    </html>
  );
}
