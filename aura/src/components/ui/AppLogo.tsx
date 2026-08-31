'use client';

import React, { memo, useMemo } from 'react';
import AppIcon from './AppIcon';
import AppImage from './AppImage';

interface AppLogoProps {
  src?: string; // Image source (optional)
  iconName?: string; // Icon name when no image
  size?: number; // Size for icon/image
  className?: string; // Additional classes
  onClick?: () => void; // Click handler
  /**
   * Emit a `<link rel="preload">` for the logo. Off by default.
   *
   * This was unconditionally `true`, which had the browser logging
   * "resource was preloaded using link preload but not used" on every page
   * load: `priority` preloads a single URL, but a fixed-size `next/image`
   * renders a `srcset` and the candidate the browser picks depends on device
   * pixel ratio, so the preloaded `w=32` went unused on any non-1x display.
   *
   * A 28px sidebar mark is never the LCP element, so the preload was competing
   * with genuinely critical resources to fetch something twice. `eager` below
   * still loads it immediately — it just does not jump the queue.
   */
  priority?: boolean;
}

const AppLogo = memo(function AppLogo({
  src = '/assets/images/app_logo.png',
  iconName = 'SparklesIcon',
  size = 64,
  className = '',
  onClick,
  priority = false,
}: AppLogoProps) {
  // Memoize className calculation
  const containerClassName = useMemo(() => {
    const classes = ['flex items-center'];
    if (onClick) classes.push('cursor-pointer hover:opacity-80 transition-opacity');
    if (className) classes.push(className);
    return classes.join(' ');
  }, [onClick, className]);

  return (
    <div className={containerClassName} onClick={onClick}>
      {/* Show image if src provided, otherwise show icon */}
      {src ? (
        <AppImage
          src={src}
          alt="Logo"
          width={size}
          height={size}
          className="flex-shrink-0"
          style={{ width: size, height: size }}
          priority={priority}
          loading={priority ? undefined : 'eager'}
          unoptimized={src.endsWith('.svg')}
        />
      ) : (
        <AppIcon name={iconName} size={size} className="flex-shrink-0" />
      )}
    </div>
  );
});

export default AppLogo;
