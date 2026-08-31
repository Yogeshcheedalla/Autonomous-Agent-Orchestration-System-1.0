'use client';

import { useCallback, useEffect, useRef, useState, type RefCallback } from 'react';

/**
 * The live height of an element.
 *
 * This exists for one specific reason. `HumanPresence` composes the portrait at a
 * fixed pixel size derived from its `width` prop, so a `max-height` on any wrapper
 * *crops* her rather than shrinking her — 85px of chin and shoulders disappeared
 * behind `overflow-hidden` before this was measured. Scaling has to go through the
 * prop, and nothing but a measurement can tell the page what to pass.
 *
 * The observed element must take its height from something other than this value.
 * Here it is a `flex-1` box, so its height is whatever the siblings left over and
 * the reported number cannot feed back into itself. Observing an element that
 * grows with its contents would loop.
 */
export function useElementHeight<T extends HTMLElement>(): [RefCallback<T>, number] {
  const [height, setHeight] = useState(0);
  const observerRef = useRef<ResizeObserver | null>(null);

  const ref = useCallback((node: T | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) return;

    // Seed from the layout that already exists. `ResizeObserver` does fire on
    // observe, but not until the next frame, and one paint at height 0 would show
    // a collapsed portrait before correcting itself.
    setHeight(node.getBoundingClientRect().height);

    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box) setHeight(box.height);
    });
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  return [ref, height];
}
