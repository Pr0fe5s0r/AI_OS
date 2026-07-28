"use client";

import { useState } from "react";
import { api, cx } from "../lib";

/** Sign in, or make a workspace. Deliberately the only thing on screen —
 *  the knowledge base holds client material and there is nothing useful to
 *  show before we know whose it is. */
export function Gate({ onIn }: { onIn: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, string> = { email, password };
      if (mode === "register") {
        body.workspace_name = workspace.trim() || "My agency";
        // The account also carries a display name; default it from the email
        // rather than asking for a third field during sign-up.
        body.name = email.split("@")[0];
      }
      await api(`/api/auth/${mode}`, { method: "POST", body: JSON.stringify(body) });
      onIn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm animate-rise">
        <div className="mb-7">
          <div className="mb-3 flex items-center gap-2">
            <Mark />
            <span className="text-sm font-semibold tracking-tight text-ink">
              Knowledge Base
            </span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-ink">
            {mode === "login" ? "Sign in" : "Create a workspace"}
          </h1>
          <p className="mt-1.5 text-xs leading-relaxed text-subtle">
            Everything your agency knows, in one place your team and your agents can
            both read from.
          </p>
        </div>

        <form onSubmit={submit} className="space-y-3">
          {mode === "register" && (
            <Input
              label="Agency name"
              value={workspace}
              onChange={setWorkspace}
              placeholder="Northwind Studio"
            />
          )}
          <Input
            label="Email"
            type="email"
            value={email}
            onChange={setEmail}
            placeholder="you@agency.com"
          />
          <Input
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            placeholder="••••••••"
          />

          {error && (
            <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-2xs text-danger">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !email || !password}
            className="w-full rounded-lg border border-accent bg-accent px-4 py-2.5 text-sm font-medium text-white transition hover:bg-accentSoft disabled:opacity-40"
          >
            {busy ? "…" : mode === "login" ? "Sign in" : "Create workspace"}
          </button>
        </form>

        <button
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
          className="mt-4 w-full text-center text-2xs text-subtle transition hover:text-muted"
        >
          {mode === "login"
            ? "No workspace yet? Create one"
            : "Already have a workspace? Sign in"}
        </button>
      </div>
    </div>
  );
}

function Input({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-2xs uppercase tracking-wide text-subtle">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cx(
          "w-full rounded-lg border border-edge bg-panel px-3 py-2.5 text-sm text-ink",
          "outline-none transition placeholder:text-subtle focus:border-accent/60"
        )}
      />
    </label>
  );
}

export function Mark() {
  return (
    <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent/15 ring-1 ring-accent/40">
      <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 text-accentSoft" fill="currentColor">
        <path d="M3 2.5A1.5 1.5 0 0 1 4.5 1H10l3 3v9.5A1.5 1.5 0 0 1 11.5 15h-7A1.5 1.5 0 0 1 3 13.5v-11Z" opacity=".45" />
        <path d="M5.5 6.5h5v1h-5v-1Zm0 2.5h5v1h-5V9Zm0-5h3v1h-3v-1Z" />
      </svg>
    </span>
  );
}
