'use client';

import React, { useEffect, useMemo, useState } from 'react';
import AppLayout from '@/components/AppLayout';
import { useTheme } from '@/components/ThemeProvider';
import {
  Bell,
  Check,
  Cpu,
  Download,
  Globe,
  Lock,
  LogOut,
  Moon,
  Save,
  Shield,
  Sun,
  Trash2,
  User,
} from 'lucide-react';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';
import ModelConnections from '@/components/settings/ModelConnections';

type SettingsTab = 'profile' | 'appearance' | 'models' | 'notifications' | 'security' | 'language';
type VoiceLanguage = 'telugu_english' | 'english' | 'hindi';

interface ProfileForm {
  full_name: string;
  email: string;
  bio: string;
  username: string;
  voice_language: VoiceLanguage;
}

interface LocalSettings {
  browserNotifications: boolean;
  desktopNotifications: boolean;
  reminderNotifications: boolean;
  notificationSound: boolean;
  twoFactor: boolean;
  loginAlerts: boolean;
  sessionTimeout: string;
  appLanguage: string;
  dateFormat: string;
}

const DEFAULT_PROFILE: ProfileForm = {
  full_name: 'Arjun Mehta',
  email: 'arjun.mehta@devcraft.io',
  bio: 'B.Tech student and AI enthusiast. Building the future with Akansha.',
  username: '',
  voice_language: 'telugu_english',
};

const DEFAULT_LOCAL_SETTINGS: LocalSettings = {
  browserNotifications: false,
  desktopNotifications: true,
  reminderNotifications: true,
  notificationSound: true,
  twoFactor: false,
  loginAlerts: true,
  sessionTimeout: '30',
  appLanguage: 'english',
  dateFormat: 'dd-mm-yyyy',
};

const LOCAL_SETTINGS_KEY = 'akansha-settings-preferences';
const APP_LANGUAGE_KEY = 'akansha_app_language';

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function normalizeProfile(
  rawProfile: Partial<Record<keyof ProfileForm, unknown>> | null | undefined
): ProfileForm {
  return {
    full_name: stringValue(rawProfile?.full_name, DEFAULT_PROFILE.full_name),
    email: stringValue(rawProfile?.email, DEFAULT_PROFILE.email),
    bio: stringValue(rawProfile?.bio, DEFAULT_PROFILE.bio),
    username: stringValue(rawProfile?.username, DEFAULT_PROFILE.username),
    voice_language:
      rawProfile?.voice_language === 'english' ||
      rawProfile?.voice_language === 'hindi' ||
      rawProfile?.voice_language === 'telugu_english'
        ? rawProfile.voice_language
        : DEFAULT_PROFILE.voice_language,
  };
}

function readLocalSettings(): LocalSettings {
  if (typeof window === 'undefined') return DEFAULT_LOCAL_SETTINGS;

  try {
    const saved = localStorage.getItem(LOCAL_SETTINGS_KEY);
    const appLanguage = localStorage.getItem(APP_LANGUAGE_KEY);
    const parsed = saved
      ? { ...DEFAULT_LOCAL_SETTINGS, ...JSON.parse(saved) }
      : DEFAULT_LOCAL_SETTINGS;
    return appLanguage ? { ...parsed, appLanguage } : parsed;
  } catch {
    return DEFAULT_LOCAL_SETTINGS;
  }
}

function Toggle({
  checked,
  onChange,
  label,
  description,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  description: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-xl border border-border bg-muted/20 p-4">
      <div>
        <p className="text-sm font-semibold text-foreground">{label}</p>
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-7 w-12 rounded-full transition-colors ${
          checked ? 'bg-primary' : 'bg-muted'
        }`}
      >
        <span
          className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow transition-transform ${
            checked ? 'translate-x-6' : 'translate-x-1'
          }`}
        />
      </button>
    </div>
  );
}

function SettingsContent() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('profile');
  const [profile, setProfile] = useState<ProfileForm>(DEFAULT_PROFILE);
  const [savedProfile, setSavedProfile] = useState<ProfileForm>(DEFAULT_PROFILE);
  const [localSettings, setLocalSettings] = useState<LocalSettings>(DEFAULT_LOCAL_SETTINGS);
  const [saving, setSaving] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ current: '', next: '', confirm: '' });
  const { theme, resolvedTheme, setTheme } = useTheme();

  const tabs = [
    { id: 'profile' as const, label: 'Profile', icon: User },
    { id: 'appearance' as const, label: 'Appearance', icon: Moon },
    { id: 'models' as const, label: 'Models', icon: Cpu },
    { id: 'notifications' as const, label: 'Notifications', icon: Bell },
    { id: 'security' as const, label: 'Security', icon: Shield },
    { id: 'language' as const, label: 'Language', icon: Globe },
  ];

  const profileDirty = useMemo(
    () => JSON.stringify(profile) !== JSON.stringify(savedProfile),
    [profile, savedProfile]
  );

  useEffect(() => {
    setLocalSettings(readLocalSettings());

    void fetch(apiUrl('/api/profile'))
      .then((res) => {
        if (!res.ok) throw new Error('Could not load profile');
        return res.json();
      })
      .then((data) => {
        const nextProfile = normalizeProfile(data.profile);
        setProfile(nextProfile);
        setSavedProfile(nextProfile);
      })
      .catch(() => toast.error('Could not load saved profile settings'));
  }, []);

  const updateLocalSettings = (patch: Partial<LocalSettings>) => {
    setLocalSettings((previous) => {
      const next = { ...previous, ...patch };
      localStorage.setItem(LOCAL_SETTINGS_KEY, JSON.stringify(next));
      if (patch.appLanguage) {
        localStorage.setItem(APP_LANGUAGE_KEY, patch.appLanguage);
      }
      return next;
    });
  };

  const saveProfile = async (patch: Partial<ProfileForm> = profile) => {
    setSaving(true);
    try {
      const res = await fetch(apiUrl('/api/profile'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });

      if (!res.ok) throw new Error('Profile save failed');
      const data = await res.json();
      const nextProfile = normalizeProfile(data.profile);
      setProfile(nextProfile);
      setSavedProfile(nextProfile);
      if (nextProfile.voice_language) {
        window.localStorage.setItem('akansha_voice_language', nextProfile.voice_language);
      }
      toast.success('Settings saved');
    } catch {
      toast.error('Could not save settings');
    } finally {
      setSaving(false);
    }
  };

  const requestBrowserNotifications = async () => {
    if (typeof window === 'undefined' || !('Notification' in window)) {
      toast.error('Browser notifications are not available here');
      updateLocalSettings({ browserNotifications: false });
      return;
    }

    const permission = await Notification.requestPermission();
    const allowed = permission === 'granted';
    updateLocalSettings({ browserNotifications: allowed });
    toast[allowed ? 'success' : 'error'](
      allowed ? 'Browser notifications enabled' : 'Browser notifications were not allowed'
    );
  };

  const sendTestDesktopNotification = async () => {
    try {
      const res = await fetch(apiUrl('/api/system/notify'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: 'Akansha notification test',
          body: 'Desktop notifications are working.',
        }),
      });

      if (!res.ok) throw new Error('Notification failed');
      toast.success('Test notification sent');
    } catch {
      toast.error('Could not send desktop notification');
    }
  };

  const savePassword = async () => {
    if (!passwordForm.next || passwordForm.next.length < 6) {
      toast.error('New password must be at least 6 characters');
      return;
    }
    if (passwordForm.next !== passwordForm.confirm) {
      toast.error('Passwords do not match');
      return;
    }

    await saveProfile({ password: passwordForm.next } as Partial<ProfileForm>);
    setPasswordForm({ current: '', next: '', confirm: '' });
  };

  return (
    <div className="flex-1 overflow-y-auto bg-background p-6 scrollbar-thin lg:p-10">
      <div className="mx-auto max-w-4xl">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-foreground">Settings</h1>
          <p className="mt-2 text-muted-foreground">
            Manage your account preferences and application settings.
          </p>
        </div>

        <div className="flex flex-col gap-8 md:flex-row">
          <div className="w-full shrink-0 space-y-1 md:w-64">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex w-full items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium transition-all ${
                    activeTab === tab.id
                      ? 'bg-primary text-white shadow-lg shadow-primary/20'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                  }`}
                >
                  <Icon size={18} />
                  {tab.label}
                </button>
              );
            })}
            <div className="mt-4 border-t border-border pt-4">
              <button
                onClick={() => toast.info('Signed out locally')}
                className="flex w-full items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium text-red-500 transition-all hover:bg-red-500/5"
              >
                <LogOut size={18} />
                Sign Out
              </button>
            </div>
          </div>

          <div className="flex-1 space-y-6">
            {activeTab === 'profile' && (
              <div className="space-y-6">
                <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                  <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                    <User size={18} className="text-primary" />
                    Profile Information
                  </h3>
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                      <div className="space-y-2">
                        <label
                          className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                          htmlFor="settings-full-name"
                        >
                          Full Name
                        </label>
                        <input
                          id="settings-full-name"
                          type="text"
                          value={profile.full_name}
                          onChange={(event) =>
                            setProfile((prev) => ({ ...prev, full_name: event.target.value }))
                          }
                          className="w-full rounded-xl border-0 bg-muted px-4 py-2.5 text-sm transition-all focus:ring-2 focus:ring-primary/40"
                        />
                      </div>
                      <div className="space-y-2">
                        <label
                          className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                          htmlFor="settings-email"
                        >
                          Email Address
                        </label>
                        <input
                          id="settings-email"
                          type="email"
                          value={profile.email}
                          onChange={(event) =>
                            setProfile((prev) => ({ ...prev, email: event.target.value }))
                          }
                          className="w-full rounded-xl border-0 bg-muted px-4 py-2.5 text-sm transition-all focus:ring-2 focus:ring-primary/40"
                        />
                      </div>
                    </div>
                    <div className="space-y-2">
                      <label
                        className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                        htmlFor="settings-username"
                      >
                        Username
                      </label>
                      <input
                        id="settings-username"
                        type="text"
                        value={profile.username}
                        onChange={(event) =>
                          setProfile((prev) => ({ ...prev, username: event.target.value }))
                        }
                        className="w-full rounded-xl border-0 bg-muted px-4 py-2.5 text-sm transition-all focus:ring-2 focus:ring-primary/40"
                      />
                    </div>
                    <div className="space-y-2">
                      <label
                        className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                        htmlFor="settings-bio"
                      >
                        Bio
                      </label>
                      <textarea
                        id="settings-bio"
                        rows={3}
                        value={profile.bio}
                        onChange={(event) =>
                          setProfile((prev) => ({ ...prev, bio: event.target.value }))
                        }
                        className="w-full resize-none rounded-xl border-0 bg-muted px-4 py-2.5 text-sm transition-all focus:ring-2 focus:ring-primary/40"
                      />
                    </div>
                  </div>
                  <div className="mt-8 flex justify-end">
                    <button
                      onClick={() => void saveProfile()}
                      disabled={saving || !profileDirty}
                      className="flex items-center gap-2 rounded-xl bg-primary px-6 py-2.5 text-sm font-medium text-white shadow-lg shadow-primary/20 transition-all hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-55"
                    >
                      <Save size={16} />
                      {saving ? 'Saving...' : profileDirty ? 'Save Changes' : 'Saved'}
                    </button>
                  </div>
                </div>

                <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                  <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold text-red-500">
                    <Trash2 size={18} />
                    Danger Zone
                  </h3>
                  <p className="mb-4 text-sm text-muted-foreground">
                    This clears local app preferences on this device. Your backend profile is not
                    deleted.
                  </p>
                  <button
                    onClick={() => {
                      localStorage.removeItem(LOCAL_SETTINGS_KEY);
                      setLocalSettings(DEFAULT_LOCAL_SETTINGS);
                      toast.success('Local settings reset');
                    }}
                    className="rounded-lg border border-red-500/30 px-4 py-2 text-sm font-medium text-red-500 transition-all hover:bg-red-500/5"
                  >
                    Reset Local Settings
                  </button>
                </div>
              </div>
            )}

            {activeTab === 'appearance' && (
              <div className="space-y-6">
                <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                  <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                    <Moon size={18} className="text-primary" />
                    Theme Preferences
                  </h3>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    {[
                      { id: 'light' as const, icon: Sun, label: 'Light' },
                      { id: 'dark' as const, icon: Moon, label: 'Dark' },
                      { id: 'system' as const, icon: Globe, label: 'System' },
                    ].map((option) => {
                      const Icon = option.icon;
                      const active = theme === option.id;
                      return (
                        <button
                          key={option.id}
                          onClick={() => {
                            setTheme(option.id);
                            toast.success(`${option.label} theme applied`);
                          }}
                          className={`flex flex-col items-center gap-3 rounded-2xl border p-6 transition-all ${
                            active
                              ? 'border-primary bg-primary/10 text-primary'
                              : 'border-border bg-muted/30 hover:border-primary/40'
                          }`}
                        >
                          <Icon size={24} />
                          <span className="text-sm font-medium">{option.label}</span>
                          {active && <Check size={15} />}
                        </button>
                      );
                    })}
                  </div>
                  <p className="mt-4 text-sm text-muted-foreground">
                    Current resolved theme: {resolvedTheme}
                  </p>
                </div>
              </div>
            )}

            {activeTab === 'models' && <ModelConnections />}

            {activeTab === 'notifications' && (
              <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                  <Bell size={18} className="text-primary" />
                  Notification Preferences
                </h3>
                <div className="space-y-4">
                  <Toggle
                    checked={localSettings.browserNotifications}
                    onChange={(next) =>
                      next
                        ? void requestBrowserNotifications()
                        : updateLocalSettings({ browserNotifications: false })
                    }
                    label="Browser notifications"
                    description="Ask this browser for permission to show app alerts."
                  />
                  <Toggle
                    checked={localSettings.desktopNotifications}
                    onChange={(next) => updateLocalSettings({ desktopNotifications: next })}
                    label="Desktop notifications"
                    description="Allow Akansha to send Windows desktop alerts through the backend."
                  />
                  <Toggle
                    checked={localSettings.reminderNotifications}
                    onChange={(next) => updateLocalSettings({ reminderNotifications: next })}
                    label="Planner reminder alerts"
                    description="Show notifications when planner reminders become due."
                  />
                  <Toggle
                    checked={localSettings.notificationSound}
                    onChange={(next) => updateLocalSettings({ notificationSound: next })}
                    label="Notification sound"
                    description="Play a subtle sound for important alerts."
                  />
                </div>
                <div className="mt-6 flex justify-end">
                  <button
                    onClick={() => void sendTestDesktopNotification()}
                    className="rounded-xl bg-primary px-5 py-2.5 text-sm font-medium text-white transition-all hover:bg-primary-hover"
                  >
                    Send Test Notification
                  </button>
                </div>
              </div>
            )}

            {activeTab === 'security' && (
              <div className="space-y-6">
                <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                  <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                    <Shield size={18} className="text-primary" />
                    Security Controls
                  </h3>
                  <div className="space-y-4">
                    <Toggle
                      checked={localSettings.twoFactor}
                      onChange={(next) => updateLocalSettings({ twoFactor: next })}
                      label="Two-step verification"
                      description="Require an extra verification step on future sign-ins."
                    />
                    <Toggle
                      checked={localSettings.loginAlerts}
                      onChange={(next) => updateLocalSettings({ loginAlerts: next })}
                      label="Login alerts"
                      description="Notify you when a new sign-in is detected."
                    />
                    <div className="rounded-xl border border-border bg-muted/20 p-4">
                      <label
                        className="text-sm font-semibold text-foreground"
                        htmlFor="session-timeout"
                      >
                        Session timeout
                      </label>
                      <select
                        id="session-timeout"
                        value={localSettings.sessionTimeout}
                        onChange={(event) =>
                          updateLocalSettings({ sessionTimeout: event.target.value })
                        }
                        className="mt-3 w-full rounded-xl border border-border bg-background px-4 py-2.5 text-sm"
                      >
                        <option value="15">15 minutes</option>
                        <option value="30">30 minutes</option>
                        <option value="60">1 hour</option>
                        <option value="never">Never on this device</option>
                      </select>
                    </div>
                  </div>
                </div>

                <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                  <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                    <Lock size={18} className="text-primary" />
                    Change Password
                  </h3>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <input
                      type="password"
                      placeholder="Current password"
                      value={passwordForm.current}
                      onChange={(event) =>
                        setPasswordForm((prev) => ({ ...prev, current: event.target.value }))
                      }
                      className="rounded-xl border-0 bg-muted px-4 py-2.5 text-sm"
                    />
                    <input
                      type="password"
                      placeholder="New password"
                      value={passwordForm.next}
                      onChange={(event) =>
                        setPasswordForm((prev) => ({ ...prev, next: event.target.value }))
                      }
                      className="rounded-xl border-0 bg-muted px-4 py-2.5 text-sm"
                    />
                    <input
                      type="password"
                      placeholder="Confirm password"
                      value={passwordForm.confirm}
                      onChange={(event) =>
                        setPasswordForm((prev) => ({ ...prev, confirm: event.target.value }))
                      }
                      className="rounded-xl border-0 bg-muted px-4 py-2.5 text-sm"
                    />
                  </div>
                  <div className="mt-6 flex justify-end">
                    <button
                      onClick={() => void savePassword()}
                      className="rounded-xl bg-primary px-5 py-2.5 text-sm font-medium text-white transition-all hover:bg-primary-hover"
                    >
                      Update Password
                    </button>
                  </div>
                </div>
              </div>
            )}

            {activeTab === 'language' && (
              <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
                <h3 className="mb-6 flex items-center gap-2 text-lg font-semibold">
                  <Globe size={18} className="text-primary" />
                  Language Preferences
                </h3>
                <div className="space-y-5">
                  <div>
                    <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      App Language
                    </label>
                    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                      {[
                        { id: 'english', label: 'English' },
                        { id: 'telugu', label: 'Telugu' },
                        { id: 'hindi', label: 'Hindi' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          onClick={() => updateLocalSettings({ appLanguage: option.id })}
                          className={`rounded-xl border px-4 py-3 text-sm font-medium transition-all ${
                            localSettings.appLanguage === option.id
                              ? 'border-primary bg-primary/10 text-primary'
                              : 'border-border bg-muted/20 hover:border-primary/40'
                          }`}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Voice Language
                    </label>
                    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                      {[
                        { id: 'telugu_english' as const, label: 'Telugu + English' },
                        { id: 'english' as const, label: 'English' },
                        { id: 'hindi' as const, label: 'Hindi' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          onClick={() => {
                            window.localStorage.setItem('akansha_voice_language', option.id);
                            setProfile((prev) => ({ ...prev, voice_language: option.id }));
                          }}
                          className={`rounded-xl border px-4 py-3 text-sm font-medium transition-all ${
                            profile.voice_language === option.id
                              ? 'border-primary bg-primary/10 text-primary'
                              : 'border-border bg-muted/20 hover:border-primary/40'
                          }`}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div>
                    <label
                      className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                      htmlFor="date-format"
                    >
                      Date Format
                    </label>
                    <select
                      id="date-format"
                      value={localSettings.dateFormat}
                      onChange={(event) => updateLocalSettings({ dateFormat: event.target.value })}
                      className="mt-3 w-full rounded-xl border border-border bg-background px-4 py-2.5 text-sm"
                    >
                      <option value="dd-mm-yyyy">DD-MM-YYYY</option>
                      <option value="mm-dd-yyyy">MM-DD-YYYY</option>
                      <option value="yyyy-mm-dd">YYYY-MM-DD</option>
                    </select>
                  </div>
                </div>
                <div className="mt-8 flex justify-end gap-3">
                  <button
                    onClick={() => {
                      const payload = JSON.stringify(
                        { profile, preferences: localSettings },
                        null,
                        2
                      );
                      const blob = new Blob([payload], { type: 'application/json' });
                      const url = URL.createObjectURL(blob);
                      const link = document.createElement('a');
                      link.href = url;
                      link.download = 'akansha-settings.json';
                      link.click();
                      URL.revokeObjectURL(url);
                    }}
                    className="flex items-center gap-2 rounded-xl border border-border px-5 py-2.5 text-sm font-medium text-foreground transition-all hover:bg-muted"
                  >
                    <Download size={16} />
                    Export
                  </button>
                  <button
                    onClick={() => void saveProfile({ voice_language: profile.voice_language })}
                    className="rounded-xl bg-primary px-5 py-2.5 text-sm font-medium text-white transition-all hover:bg-primary-hover"
                  >
                    Save Language
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AppLayout activePath="/settings">
      <SettingsContent />
    </AppLayout>
  );
}
