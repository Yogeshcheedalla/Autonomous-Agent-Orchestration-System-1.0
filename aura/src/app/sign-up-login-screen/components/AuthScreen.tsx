'use client';

import React, { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  AlertCircle,
  ArrowRight,
  Bot,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Languages,
  Loader2,
  Lock,
  Mail,
  Mic2,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { toast } from 'sonner';
import AppLogo from '@/components/ui/AppLogo';
import { API_BASE_URL } from '@/lib/apiBase';

type AuthMode = 'welcome' | 'sign-in' | 'sign-up' | 'forgot-password';
type AppLanguage = 'english' | 'telugu' | 'hindi';

interface AuthScreenProps {
  initialMode?: AuthMode;
}

const API_BASE = API_BASE_URL;

const copy: Record<AppLanguage, Record<string, string>> = {
  english: {
    eyebrow: 'AKANSHA SECURE ACCESS',
    heroTitle: 'Autonomous voice, automation, and memory in one protected workspace.',
    heroBody:
      'Sign in to control the assistant, connect Google, manage voice profiles, and keep automation permissions under your account.',
    welcomeTitle: 'Welcome dashboard',
    welcomeBody:
      'Choose how you want to enter Akansha. Google connects Gmail and Calendar after OAuth; email mode uses OTP verification.',
    signIn: 'Sign in',
    signUp: 'Sign up',
    forgot: 'Forgot password?',
    google: 'Continue with Google',
    email: 'Email address',
    password: 'Password',
    fullName: 'Full name',
    username: 'Username',
    otp: 'Verification code',
    sendOtp: 'Send code',
    create: 'Create account',
    unlock: 'Unlock workspace',
    reset: 'Reset password',
    newPassword: 'New password',
    back: 'Back to dashboard',
    language: 'Language',
  },
  telugu: {
    eyebrow: 'AKANSHA సురక్షిత ప్రవేశం',
    heroTitle: 'వాయిస్, ఆటోమేషన్, మెమరీ అన్నీ ఒక సురక్షిత వర్క్‌స్పేస్‌లో.',
    heroBody:
      'Akansha ని నియంత్రించడానికి, Google కనెక్ట్ చేయడానికి, వాయిస్ ప్రొఫైల్స్ మరియు ఆటోమేషన్ అనుమతులు నిర్వహించడానికి సైన్ ఇన్ చేయండి.',
    welcomeTitle: 'స్వాగత డాష్‌బోర్డ్',
    welcomeBody:
      'Akansha లోకి ఎలా ప్రవేశించాలో ఎంచుకోండి. Google OAuth Gmail/Calendar ను కలుపుతుంది; email mode OTP ఉపయోగిస్తుంది.',
    signIn: 'సైన్ ఇన్',
    signUp: 'సైన్ అప్',
    forgot: 'పాస్‌వర్డ్ మర్చిపోయారా?',
    google: 'Google తో కొనసాగండి',
    email: 'ఈమెయిల్ అడ్రస్',
    password: 'పాస్‌వర్డ్',
    fullName: 'పూర్తి పేరు',
    username: 'యూజర్‌నేమ్',
    otp: 'ధృవీకరణ కోడ్',
    sendOtp: 'కోడ్ పంపండి',
    create: 'అకౌంట్ సృష్టించండి',
    unlock: 'వర్క్‌స్పేస్ తెరవండి',
    reset: 'పాస్‌వర్డ్ రీసెట్',
    newPassword: 'కొత్త పాస్‌వర్డ్',
    back: 'డాష్‌బోర్డ్‌కు తిరుగు',
    language: 'భాష',
  },
  hindi: {
    eyebrow: 'AKANSHA सुरक्षित प्रवेश',
    heroTitle: 'Voice, automation और memory एक सुरक्षित workspace में.',
    heroBody:
      'Akansha control करने, Google connect करने, voice profiles और automation permissions manage करने के लिए sign in करें.',
    welcomeTitle: 'Welcome dashboard',
    welcomeBody:
      'Akansha में प्रवेश का तरीका चुनें. Google OAuth Gmail/Calendar जोड़ता है; email mode OTP verification इस्तेमाल करता है.',
    signIn: 'Sign in',
    signUp: 'Sign up',
    forgot: 'Password भूल गए?',
    google: 'Google के साथ जारी रखें',
    email: 'Email address',
    password: 'Password',
    fullName: 'Full name',
    username: 'Username',
    otp: 'Verification code',
    sendOtp: 'Code भेजें',
    create: 'Account बनाएं',
    unlock: 'Workspace खोलें',
    reset: 'Password reset',
    newPassword: 'New password',
    back: 'Dashboard पर वापस',
    language: 'Language',
  },
};

function getInitialLanguage(): AppLanguage {
  if (typeof window === 'undefined') return 'english';
  const stored = window.localStorage.getItem('akansha_app_language') as AppLanguage | null;
  return stored === 'telugu' || stored === 'hindi' || stored === 'english' ? stored : 'english';
}

async function readJson(res: Response) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.message || 'Request failed');
  }
  return data;
}

function AuthInput({
  icon: Icon,
  label,
  type = 'text',
  value,
  onChange,
  placeholder,
  autoComplete,
}: {
  icon: LucideIcon;
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
}) {
  const [visible, setVisible] = useState(false);
  const isPassword = type === 'password';

  return (
    <label className="block">
      <span className="mb-2 block text-xs font-semibold uppercase tracking-[0.22em] text-slate-400">
        {label}
      </span>
      <span className="relative block">
        <Icon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-cyan-300" />
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          type={isPassword && visible ? 'text' : type}
          autoComplete={autoComplete}
          placeholder={placeholder}
          className="h-12 w-full rounded-md border border-white/10 bg-slate-950/80 px-10 text-sm text-white outline-none transition focus:border-cyan-300 focus:ring-2 focus:ring-cyan-300/20"
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setVisible((next) => !next)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 transition hover:text-white"
            aria-label={visible ? 'Hide password' : 'Show password'}
          >
            {visible ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        )}
      </span>
    </label>
  );
}

export default function AuthScreen({ initialMode = 'welcome' }: AuthScreenProps) {
  const router = useRouter();
  const params = useSearchParams();
  const [language, setLanguage] = useState<AppLanguage>('english');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [username, setUsername] = useState('');
  const [code, setCode] = useState('');
  const [otpSent, setOtpSent] = useState(false);

  const t = copy[language];
  const isFormMode = initialMode !== 'welcome';
  const googleConnected = params?.get('google') === 'connected';

  useEffect(() => {
    setLanguage(getInitialLanguage());
  }, []);

  useEffect(() => {
    if (googleConnected) {
      toast.success('Google account connected. Akansha workspace is ready.');
    }
  }, [googleConnected]);

  const setAppLanguage = (next: AppLanguage) => {
    setLanguage(next);
    window.localStorage.setItem('akansha_app_language', next);
  };

  const connectGoogle = async () => {
    setError('');
    setLoading(true);
    try {
      const data = await readJson(await fetch(`${API_BASE}/api/google/auth-url`));
      if (!data.auth_url) {
        toast.info(data.message ?? 'Google OAuth is not configured yet.');
        return;
      }
      window.location.href = data.auth_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Google sign-in failed.');
    } finally {
      setLoading(false);
    }
  };

  const sendOtp = async (purpose: 'login' | 'signup' | 'reset') => {
    setError('');
    setNotice('');
    setLoading(true);
    try {
      const data = await readJson(
        await fetch(`${API_BASE}/api/auth/send-otp`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, purpose }),
        })
      );
      setOtpSent(true);
      setNotice(data.dev_code ? `Local dev OTP: ${data.dev_code}` : data.message);
      toast.success(data.message || 'OTP sent.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send OTP.');
    } finally {
      setLoading(false);
    }
  };

  /**
   * The shape all three auth handlers return: /api/auth/login, /api/auth/signup and
   * /api/auth/verify-otp each answer `{success, token, user: auth_user_payload(...)}`
   * (backend/main.py::auth_user_payload). Only the two fields read here are
   * declared, and both stay optional -- an error response omits them, and typing
   * them as required would make `data.token` look guaranteed at the one call site
   * where it is not.
   */
  const finishAuth = (data: { token?: string; user?: { email?: string } }) => {
    if (data.token) {
      window.localStorage.setItem('akansha_auth_token', data.token);
    }
    if (data.user?.email) {
      window.localStorage.setItem('akansha_auth_email', data.user.email);
    }
    router.push('/voice-assistant');
  };

  const signIn = async () => {
    setError('');
    setLoading(true);
    try {
      const data = await readJson(
        await fetch(`${API_BASE}/api/auth/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
        })
      );
      finishAuth(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed.');
    } finally {
      setLoading(false);
    }
  };

  const register = async () => {
    setError('');
    setLoading(true);
    try {
      const data = await readJson(
        await fetch(`${API_BASE}/api/auth/register`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ full_name: fullName, email, username, password, code }),
        })
      );
      finishAuth(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign up failed.');
    } finally {
      setLoading(false);
    }
  };

  const resetPassword = async () => {
    setError('');
    setLoading(true);
    try {
      await readJson(
        await fetch(`${API_BASE}/api/auth/reset-password`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, code, new_password: newPassword }),
        })
      );
      toast.success('Password reset. Sign in with your new password.');
      router.push('/sign-up-login-screen/sign-in');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Password reset failed.');
    } finally {
      setLoading(false);
    }
  };

  const title = useMemo(() => {
    if (initialMode === 'sign-in') return t.signIn;
    if (initialMode === 'sign-up') return t.signUp;
    if (initialMode === 'forgot-password') return t.forgot;
    return t.welcomeTitle;
  }, [initialMode, t]);

  return (
    <main className="min-h-screen overflow-hidden bg-[#050814] text-white">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_12%,rgba(103,232,249,0.18),transparent_30%),radial-gradient(circle_at_80%_0%,rgba(108,71,255,0.2),transparent_32%),linear-gradient(180deg,#050814_0%,#0b1020_100%)]" />
      <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(103,232,249,0.05)_1px,transparent_1px),linear-gradient(180deg,rgba(103,232,249,0.04)_1px,transparent_1px)] bg-[size:48px_48px] opacity-40" />

      <section className="relative mx-auto grid min-h-screen w-full max-w-7xl grid-cols-1 gap-8 px-5 py-6 lg:grid-cols-[1.05fr_0.95fr] lg:px-8">
        <div className="flex flex-col justify-between rounded-md border border-white/10 bg-slate-950/40 p-6 backdrop-blur-xl lg:p-9">
          <div>
            <div className="mb-10 flex items-center justify-between gap-4">
              <Link href="/sign-up-login-screen" className="flex items-center gap-3">
                <AppLogo size={34} />
                <span className="text-lg font-semibold">Akansha</span>
              </Link>
              <div className="flex items-center gap-2 rounded-md border border-white/10 bg-slate-950/70 p-1">
                <Languages size={15} className="ml-2 text-cyan-200" />
                {(['english', 'telugu', 'hindi'] as AppLanguage[]).map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setAppLanguage(item)}
                    className={`rounded px-3 py-1.5 text-xs font-medium transition ${
                      language === item
                        ? 'bg-primary text-white'
                        : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    {item === 'english' ? 'EN' : item === 'telugu' ? 'TE' : 'HI'}
                  </button>
                ))}
              </div>
            </div>

            <p className="mb-4 text-xs font-semibold uppercase tracking-[0.32em] text-cyan-200">
              {t.eyebrow}
            </p>
            <h1 className="max-w-2xl text-4xl font-semibold leading-tight tracking-normal text-white lg:text-6xl">
              {t.heroTitle}
            </h1>
            <p className="mt-5 max-w-xl text-base leading-7 text-slate-300">{t.heroBody}</p>
          </div>

          <div className="mt-10 grid gap-3 sm:grid-cols-3">
            {[
              {
                icon: Mic2,
                title: 'Voice agent',
                body: 'Continuous listening with interruption control.',
              },
              {
                icon: Bot,
                title: 'Automation',
                body: 'Browser, desktop, forms, media, and tasks.',
              },
              {
                icon: ShieldCheck,
                title: 'Secure access',
                body: 'OTP, password, Google, and reset flows.',
              },
            ].map(({ icon: Icon, title: cardTitle, body }) => (
              <div
                key={cardTitle}
                className="rounded-md border border-white/10 bg-white/[0.04] p-4"
              >
                <Icon className="mb-3 h-5 w-5 text-cyan-200" />
                <p className="text-sm font-semibold">{cardTitle}</p>
                <p className="mt-2 text-xs leading-5 text-slate-400">{body}</p>
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-center">
          <div className="w-full max-w-[500px] rounded-md border border-white/10 bg-[#090d1a]/90 p-6 shadow-[0_24px_90px_rgba(0,0,0,0.45)] backdrop-blur-xl lg:p-8">
            <div className="mb-6 flex items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.28em] text-slate-500">Access console</p>
                <h2 className="mt-2 text-2xl font-semibold">{title}</h2>
              </div>
              <div className="rounded-md border border-cyan-300/20 bg-cyan-300/10 p-3 text-cyan-200">
                <Sparkles size={20} />
              </div>
            </div>

            {error && (
              <div className="mb-4 flex gap-3 rounded-md border border-red-400/30 bg-red-500/10 p-3 text-sm text-red-100">
                <AlertCircle size={16} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {notice && (
              <div className="mb-4 flex gap-3 rounded-md border border-cyan-300/30 bg-cyan-300/10 p-3 text-sm text-cyan-100">
                <Check size={16} className="mt-0.5 shrink-0" />
                <span>{notice}</span>
              </div>
            )}

            {initialMode === 'welcome' && (
              <div className="space-y-4">
                <p className="text-sm leading-6 text-slate-300">{t.welcomeBody}</p>
                <button
                  onClick={connectGoogle}
                  disabled={loading}
                  className="flex h-12 w-full items-center justify-center gap-3 rounded-md border border-white/10 bg-white text-sm font-semibold text-slate-950 transition hover:bg-cyan-100"
                >
                  {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  {t.google}
                </button>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Link
                    href="/sign-up-login-screen/sign-in"
                    className="flex h-12 items-center justify-center gap-2 rounded-md bg-primary text-sm font-semibold text-white transition hover:bg-primary-hover"
                  >
                    {t.signIn} <ArrowRight size={16} />
                  </Link>
                  <Link
                    href="/sign-up-login-screen/sign-up"
                    className="flex h-12 items-center justify-center rounded-md border border-white/10 text-sm font-semibold text-white transition hover:border-cyan-300/50"
                  >
                    {t.signUp}
                  </Link>
                </div>
              </div>
            )}

            {initialMode === 'sign-in' && (
              <form
                className="space-y-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void signIn();
                }}
              >
                <AuthInput
                  icon={Mail}
                  label={t.email}
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="email"
                  placeholder="you@example.com"
                />
                <AuthInput
                  icon={Lock}
                  label={t.password}
                  type="password"
                  value={password}
                  onChange={setPassword}
                  autoComplete="current-password"
                  placeholder="••••••••"
                />
                <button
                  disabled={loading}
                  className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-primary text-sm font-semibold text-white transition hover:bg-primary-hover disabled:opacity-60"
                >
                  {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound size={16} />}
                  {t.unlock}
                </button>
                <button
                  type="button"
                  onClick={connectGoogle}
                  className="h-12 w-full rounded-md border border-white/10 text-sm font-semibold transition hover:border-cyan-300/50"
                >
                  {t.google}
                </button>
                <div className="flex items-center justify-between text-sm">
                  <Link
                    href="/sign-up-login-screen/forgot-password"
                    className="text-cyan-200 hover:text-cyan-100"
                  >
                    {t.forgot}
                  </Link>
                  <Link
                    href="/sign-up-login-screen/sign-up"
                    className="text-slate-300 hover:text-white"
                  >
                    {t.signUp}
                  </Link>
                </div>
              </form>
            )}

            {initialMode === 'sign-up' && (
              <form
                className="space-y-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (otpSent) {
                    void register();
                  } else {
                    void sendOtp('signup');
                  }
                }}
              >
                <AuthInput
                  icon={Bot}
                  label={t.fullName}
                  value={fullName}
                  onChange={setFullName}
                  autoComplete="name"
                  placeholder="Yogesh"
                />
                <AuthInput
                  icon={Mail}
                  label={t.email}
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="email"
                  placeholder="you@example.com"
                />
                <AuthInput
                  icon={KeyRound}
                  label={t.username}
                  value={username}
                  onChange={setUsername}
                  autoComplete="username"
                  placeholder="akansha_owner"
                />
                <AuthInput
                  icon={Lock}
                  label={t.password}
                  type="password"
                  value={password}
                  onChange={setPassword}
                  autoComplete="new-password"
                  placeholder="Minimum 8 characters"
                />
                {otpSent && (
                  <AuthInput
                    icon={ShieldCheck}
                    label={t.otp}
                    value={code}
                    onChange={setCode}
                    autoComplete="one-time-code"
                    placeholder="000000"
                  />
                )}
                <button
                  disabled={loading}
                  className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-primary text-sm font-semibold text-white transition hover:bg-primary-hover disabled:opacity-60"
                >
                  {loading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <ArrowRight size={16} />
                  )}
                  {otpSent ? t.create : t.sendOtp}
                </button>
                <button
                  type="button"
                  onClick={connectGoogle}
                  className="h-12 w-full rounded-md border border-white/10 text-sm font-semibold transition hover:border-cyan-300/50"
                >
                  {t.google}
                </button>
                <Link
                  href="/sign-up-login-screen/sign-in"
                  className="block text-center text-sm text-slate-300 hover:text-white"
                >
                  {t.signIn}
                </Link>
              </form>
            )}

            {initialMode === 'forgot-password' && (
              <form
                className="space-y-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (otpSent) {
                    void resetPassword();
                  } else {
                    void sendOtp('reset');
                  }
                }}
              >
                <AuthInput
                  icon={Mail}
                  label={t.email}
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="email"
                  placeholder="you@example.com"
                />
                {otpSent && (
                  <>
                    <AuthInput
                      icon={ShieldCheck}
                      label={t.otp}
                      value={code}
                      onChange={setCode}
                      autoComplete="one-time-code"
                      placeholder="000000"
                    />
                    <AuthInput
                      icon={Lock}
                      label={t.newPassword}
                      type="password"
                      value={newPassword}
                      onChange={setNewPassword}
                      autoComplete="new-password"
                      placeholder="Minimum 8 characters"
                    />
                  </>
                )}
                <button
                  disabled={loading}
                  className="flex h-12 w-full items-center justify-center gap-2 rounded-md bg-primary text-sm font-semibold text-white transition hover:bg-primary-hover disabled:opacity-60"
                >
                  {loading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <ArrowRight size={16} />
                  )}
                  {otpSent ? t.reset : t.sendOtp}
                </button>
                <Link
                  href="/sign-up-login-screen/sign-in"
                  className="block text-center text-sm text-slate-300 hover:text-white"
                >
                  {t.signIn}
                </Link>
              </form>
            )}

            {isFormMode && (
              <Link
                href="/sign-up-login-screen"
                className="mt-6 block text-center text-sm text-slate-400 transition hover:text-white"
              >
                {t.back}
              </Link>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
