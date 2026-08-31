'use client';

import React from 'react';
import { Menu, Bell, Sun, Moon, Monitor, ChevronDown, Zap } from 'lucide-react';
import { useTheme } from './ThemeProvider';

interface TopbarProps {
  onMobileMenuOpen: () => void;
  sidebarCollapsed?: boolean;
}

export default function Topbar({ onMobileMenuOpen }: TopbarProps) {
  const { theme, setTheme } = useTheme();
  const [themeMenuOpen, setThemeMenuOpen] = React.useState(false);

  const themeOptions = [
    { key: 'theme-light', value: 'light' as const, icon: Sun, label: 'Light' },
    { key: 'theme-dark', value: 'dark' as const, icon: Moon, label: 'Dark' },
    { key: 'theme-system', value: 'system' as const, icon: Monitor, label: 'System' },
  ];

  const currentThemeIcon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor;
  const CurrentIcon = currentThemeIcon;

  return (
    <header className="h-14 border-b border-border bg-card/80 backdrop-blur-sm flex items-center px-4 gap-3 shrink-0 relative z-40">
      <button
        onClick={onMobileMenuOpen}
        className="lg:hidden p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Open menu"
      >
        <Menu size={18} />
      </button>

      <div className="flex-1" />

      {/* Stable app status indicator. Keep this SSR-safe to avoid hydration mismatches. */}
      <div className="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-muted text-xs font-medium text-muted-foreground">
        <Zap size={12} className="text-amber-400" />
        <span>Fast ready</span>
      </div>

      {/* Notifications */}
      <button
        className="relative p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Notifications"
      >
        <Bell size={17} />
        <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-primary" />
      </button>

      {/* Theme toggle */}
      <div className="relative">
        <button
          onClick={() => setThemeMenuOpen(!themeMenuOpen)}
          className="flex items-center gap-1 p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
          aria-label="Toggle theme"
        >
          <CurrentIcon size={17} />
          <ChevronDown size={12} />
        </button>

        {themeMenuOpen && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setThemeMenuOpen(false)} />
            <div className="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-xl shadow-lg shadow-black/10 py-1 min-w-[120px] animate-fade-in">
              {themeOptions.map(({ key, value, icon: Icon, label }) => (
                <button
                  key={key}
                  onClick={() => {
                    setTheme(value);
                    setThemeMenuOpen(false);
                  }}
                  className={`flex items-center gap-2 w-full px-3 py-2 text-sm transition-colors ${
                    theme === value
                      ? 'text-primary bg-primary/5'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                  }`}
                >
                  <Icon size={14} />
                  {label}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </header>
  );
}
