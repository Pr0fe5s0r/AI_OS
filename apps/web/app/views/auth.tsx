"use client";

import { useState } from "react";
import { Session, api } from "../lib";

/* Sign in / create an account.
   Shown instead of the app whenever there is no session — the API rejects
   unauthenticated requests outright, so rendering the shell full of empty
   panels would just be a lie with a spinner on it. */

type Mode = "login" | "register";

function Field({ label, hint, ...props }: {
  label: string; hint?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[12px] text-muted">{label}</span>
      <input
        {...props}
        className="rounded-md border border-edge bg-elevated px-3 py-2 text-[13.5px] text-ink outline-none placeholder:text-subtle focus:border-edgeStrong"
      />
      {hint && <span className="text-[11.5px] text-subtle">{hint}</span>}
    </label>
  );
}

export default function AuthView({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [mode, setMode] = useState<Mode>("register");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [invite, setInvite] = useState("");
  const [joining, setJoining] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const body =
        mode === "login"
          ? { email, password }
          : {
              email, name, password,
              ...(joining ? { invite_token: invite.trim() } : { workspace_name: workspace }),
            };
      const session: Session = await api(`/api/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onSignedIn(session);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const registering = mode === "register";

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-6">
      <div className="w-full max-w-[380px]">
        <div className="mb-7 flex items-center gap-2.5">
          <span className="h-[9px] w-[9px] rounded-full bg-accent" />
          <span className="text-[16px] font-semibold text-ink">MarkOS</span>
        </div>

        <h1 className="m-0 text-[20px] font-semibold text-ink">
          {registering ? "Create your account" : "Welcome back"}
        </h1>
        <p className="mb-6 mt-1.5 text-[13px] leading-relaxed text-muted">
          {registering
            ? "One engine that learns how your team works, from the tools you already use."
            : "Sign in to your workspace."}
        </p>

        <form onSubmit={submit} className="flex flex-col gap-3.5">
          {registering && (
            <Field
              label="Your name" value={name} required autoComplete="name"
              onChange={(e) => setName(e.target.value)} placeholder="Alex Chen"
            />
          )}
          <Field
            label="Email" type="email" value={email} required
            autoComplete="email" onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
          />
          <Field
            label="Password" type="password" value={password} required
            autoComplete={registering ? "new-password" : "current-password"}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••"
            hint={registering ? "At least 10 characters." : undefined}
          />

          {registering && !joining && (
            <Field
              label="Workspace name" value={workspace} required
              onChange={(e) => setWorkspace(e.target.value)}
              placeholder="Platform Team"
              hint="You'll be its owner, and can invite the rest of the team."
            />
          )}
          {registering && joining && (
            <Field
              label="Invite code" value={invite} required
              onChange={(e) => setInvite(e.target.value)}
              placeholder="paste the code you were sent"
            />
          )}

          {registering && (
            <button
              type="button"
              onClick={() => setJoining((v) => !v)}
              className="self-start border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink"
            >
              {joining ? "Start a new workspace instead" : "I have an invite code"}
            </button>
          )}

          {error && (
            <div className="rounded-md border px-3 py-2 text-[12.5px]" style={{ borderColor: "#8b3a3a", color: "#f85149" }}>
              {error}
            </div>
          )}

          <button
            type="submit" disabled={busy}
            className="mt-1 rounded-md border-none bg-accent px-4 py-2.5 text-[13.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40"
          >
            {busy ? "Working…" : registering ? "Create account" : "Sign in"}
          </button>
        </form>

        <div className="mt-5 text-[12.5px] text-muted">
          {registering ? "Already have an account? " : "New here? "}
          <button
            onClick={() => { setMode(registering ? "login" : "register"); setError(null); }}
            className="border-none bg-transparent p-0 text-[12.5px] text-accent underline"
          >
            {registering ? "Sign in" : "Create one"}
          </button>
        </div>
      </div>
    </div>
  );
}
