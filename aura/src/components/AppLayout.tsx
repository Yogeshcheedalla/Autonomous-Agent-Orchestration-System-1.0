'use client';

import React, { useState } from 'react';
import Sidebar from './Sidebar';
import Topbar from './Topbar';
import { ThemeProvider } from './ThemeProvider';
import { Toaster } from 'sonner';

interface AppLayoutProps {
  children: React.ReactNode;
  activePath?: string;
}

export default function AppLayout({ children, activePath }: AppLayoutProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [isNavigating, setIsNavigating] = useState(false);

  // Simple navigation feedback
  React.useEffect(() => {
    setIsNavigating(true);
    const timer = setTimeout(() => setIsNavigating(false), 600);
    return () => clearTimeout(timer);
  }, [activePath]);

  return (
    <ThemeProvider>
      <div className="flex h-screen overflow-hidden bg-background">
        {/* Navigation Progress Bar */}
        {isNavigating && (
          <div className="fixed top-0 left-0 right-0 h-0.5 z-[60] bg-primary overflow-hidden">
            <div className="h-full bg-accent animate-progress-bar w-full origin-left" />
          </div>
        )}
        {/* Mobile overlay */}
        {mobileSidebarOpen && (
          <div
            className="fixed inset-0 z-40 bg-black/50 lg:hidden"
            onClick={() => setMobileSidebarOpen(false)}
          />
        )}

        {/* Sidebar */}
        <div
          className={`
            fixed inset-y-0 left-0 z-50 lg:relative lg:z-auto
            transition-transform duration-300 ease-in-out
            ${mobileSidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
          `}
        >
          <Sidebar
            collapsed={sidebarCollapsed}
            onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
            activePath={activePath}
            onClose={() => setMobileSidebarOpen(false)}
          />
        </div>

        {/* Main content */}
        <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
          <Topbar
            onMobileMenuOpen={() => setMobileSidebarOpen(true)}
            sidebarCollapsed={sidebarCollapsed}
          />
          <main className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">{children}</main>
        </div>
      </div>
      <Toaster
        position="bottom-right"
        toastOptions={{
          classNames: {
            toast: 'bg-card border border-border text-foreground font-sans text-sm',
            error: 'border-red-500/30 bg-red-500/5',
            success: 'border-green-500/30 bg-green-500/5',
          },
        }}
      />
    </ThemeProvider>
  );
}
