'use client';

/**
 * AppIcon — name-addressed icon lookup, explicitly registered.
 *
 * This component used to do:
 *
 *     import * as HeroIcons from '@heroicons/react/24/outline';
 *     import * as HeroIconsSolid from '@heroicons/react/24/solid';
 *     const IconComponent = iconSet[name];
 *
 * Two namespace imports plus a runtime lookup by string is unshakeable by
 * design: the bundler cannot know which of the 648 icons `name` will hold, so
 * it keeps all of them. That was 476 kB of raw JS (~90 kB gzipped) in the
 * First Load of every route rendering the sidebar — measured as the entire gap
 * between the routes that mount `Sidebar` (205-249 kB) and the two that do not
 * (112 kB, 122 kB). The app referenced exactly three of those icons.
 *
 * The registry below keeps the same `name: string` API — callers pass dynamic
 * strings and `AppLogo` still forwards a prop — but the set is enumerated, so
 * only these icons are bundled. Adding an icon means adding a line here, which
 * is the intended cost: it makes the bundle a deliberate decision rather than a
 * side effect of a lookup.
 */

import React from 'react';
import {
  ArrowLeftIcon,
  HomeIcon,
  QuestionMarkCircleIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';
import {
  ArrowLeftIcon as ArrowLeftIconSolid,
  HomeIcon as HomeIconSolid,
  QuestionMarkCircleIcon as QuestionMarkCircleIconSolid,
  SparklesIcon as SparklesIconSolid,
} from '@heroicons/react/24/solid';

type IconVariant = 'outline' | 'solid';
type IconComponent = React.ComponentType<React.SVGProps<SVGSVGElement>>;

/**
 * Every icon the app can render, by the name callers pass.
 *
 * To add one: import it from both `24/outline` and `24/solid` above and add a
 * row here. Keep both variants populated — a row present in only one map falls
 * back to the question mark when that variant is requested, which looks like a
 * typo at the call site rather than a gap here.
 */
const OUTLINE: Record<string, IconComponent> = {
  ArrowLeftIcon,
  HomeIcon,
  QuestionMarkCircleIcon,
  SparklesIcon,
};

const SOLID: Record<string, IconComponent> = {
  ArrowLeftIcon: ArrowLeftIconSolid,
  HomeIcon: HomeIconSolid,
  QuestionMarkCircleIcon: QuestionMarkCircleIconSolid,
  SparklesIcon: SparklesIconSolid,
};

/** Names that resolve, for tests and for the dev-time warning below. */
export const REGISTERED_ICONS = Object.keys(OUTLINE);

const warned = new Set<string>();

function warnOnce(name: string, variant: IconVariant): void {
  // Silent fallbacks are how an icon goes missing in production without
  // anyone noticing. Dev-only and deduped so it stays readable.
  if (process.env.NODE_ENV === 'production' || warned.has(`${variant}:${name}`)) return;
  warned.add(`${variant}:${name}`);
  console.warn(
    `[AppIcon] "${name}" (${variant}) is not registered, rendering the fallback. ` +
      `Add it to src/components/ui/AppIcon.tsx. Registered: ${REGISTERED_ICONS.join(', ')}`
  );
}

interface IconProps extends Omit<React.SVGProps<SVGSVGElement>, 'onClick' | 'name'> {
  /** Heroicon component name, e.g. "HomeIcon". Registered names only. */
  name: string;
  variant?: IconVariant;
  size?: number;
  className?: string;
  onClick?: () => void;
  disabled?: boolean;
}

function Icon({
  name,
  variant = 'outline',
  size = 24,
  className = '',
  onClick,
  disabled = false,
  ...props
}: IconProps) {
  const iconSet = variant === 'solid' ? SOLID : OUTLINE;
  let IconComponent = iconSet[name];

  if (!IconComponent) {
    warnOnce(name, variant);
    IconComponent = variant === 'solid' ? QuestionMarkCircleIconSolid : QuestionMarkCircleIcon;
  }

  const interaction = disabled
    ? 'opacity-50 cursor-not-allowed'
    : onClick
      ? 'cursor-pointer hover:opacity-80 transition-opacity'
      : '';

  return (
    <IconComponent
      width={size}
      height={size}
      aria-hidden={props['aria-label'] ? undefined : true}
      className={`${interaction} ${className}`.trim()}
      onClick={disabled ? undefined : onClick}
      {...props}
    />
  );
}

export default Icon;
