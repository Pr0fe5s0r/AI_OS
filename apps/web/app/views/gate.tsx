"use client";

import { useState } from "react";
import * as api from "../api";
import { cx } from "../data";
import { Logo, VectorField } from "../ui/kit";

/** Sign in, or start a workspace.
 *
 *  The store holds a workspace's own data and every call is scoped by the
 *  credential, so there is nothing meaningful to render before we know whose
 *  it is. The field behind the panel is the same one the console uses — it is
 *  decoration here, and the only place in the app where it is. */
export function Gate({ onIn }: { onIn: () => void }) {
  const [mode, setMode] = useState<"in" | "up">("in");
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
      if (mode === "in") await api.signIn(email, password);
      else await api.signUp(email, password, workspace);
      onIn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative flex h-screen items-center justify-center overflow-hidden bg-canvas px-6">
      <div className="pointer-events-none absolute inset-0 opacity-[0.35]">
        <VectorField height={820} />
      </div>

      <div className="card-in relative w-full max-w-sm rounded-2xl border border-edge bg-panel/90 p-7 backdrop-blur">
        <Logo />
        <h1 className="mt-6 text-lg font-semibold tracking-tight text-ink">
          {mode === "in" ? "Sign in" : "Create a workspace"}
        </h1>
        <p className="mt-1.5 text-xs leading-relaxed text-subtle">
          A vector store with the retrieval written down: every query keeps a
          trace of how it reached its answer.
        </p>

        <form onSubmit={submit} className="mt-6 space-y-3">
          {mode === "up" && (
            <Input label="Workspace" value={workspace} onChange={setWorkspace} placeholder="Acme" />
          )}
          <Input
            label="Email"
            type="email"
            value={email}
            onChange={setEmail}
            placeholder="you@company.com"
          />
          <Input
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            placeholder="••••••••"
          />

          {error && (
            <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-2xs text-danger">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !email || !password}
            className="w-full rounded-lg border border-accent bg-accent px-4 py-2.5 text-sm font-semibold text-canvas transition hover:bg-accentSoft disabled:opacity-40"
          >
            {busy ? "…" : mode === "in" ? "Sign in" : "Create workspace"}
          </button>
        </form>

        <button
          onClick={() => {
            setMode(mode === "in" ? "up" : "in");
            setError(null);
          }}
          className="mt-4 w-full text-center font-mono text-2xs text-subtle transition hover:text-muted"
        >
          {mode === "in" ? "No workspace yet? Create one" : "Already have one? Sign in"}
        </button>
      </div>
    </main>
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
      <span className="mb-1.5 block font-mono text-2xs uppercase tracking-[0.14em] text-subtle">
        {label}
      </span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cx(
          "w-full rounded-lg border border-edge bg-canvas px-3 py-2.5 text-sm text-ink",
          "outline-none transition placeholder:text-subtle focus:border-accent/60"
        )}
      />
    </label>
  );
}
