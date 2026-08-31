'use client';

import { useEffect, useState } from 'react';

/**
 * Whether the user has asked the OS to reduce motion.
 *
 * Returns `false` during SSR and on the first client render, then corrects
 * itself in an effect. That order matters: `matchMedia` does not exist on the
 * server, and reading it during render would either crash or produce markup the
 * client disagrees with. Starting at `false` and correcting means the worst case
 * is one frame of motion, not a hydration mismatch.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(query.matches);
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  return reduced;
}

export default useReducedMotion;
