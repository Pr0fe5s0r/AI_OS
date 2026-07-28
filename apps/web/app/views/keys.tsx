"use client";

import { useState } from "react";
import { ApiKey, KEYS, ago, cx } from "../data";
import { Button, Card, Chip, Code, Copy, Label, Mono } from "../ui/kit";

const SCOPE_TONE: Record<ApiKey["scope"], string> = {
  read: "text-heat-2 border-heat-2/40 bg-heat-2/10",
  write: "text-accentSoft border-accent/40 bg-accent/10",
  admin: "text-hot border-hot/40 bg-hot/10",
};

export function Keys({ toast }: { toast: (m: string) => void }) {
  const [keys, setKeys] = useState<ApiKey[]>(KEYS);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [freshKey, setFreshKey] = useState<ApiKey | null>(null);
  const [label, setLabel] = useState("");
  const [scope, setScope] = useState<ApiKey["scope"]>("read");

  function create() {
    const secret =
      "mvk_live_" +
      Array.from({ length: 28 }, () =>
        "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789abcdefghijkmnpqrstuvwxyz"[Math.floor(Math.random() * 57)]
      ).join("");
    const k: ApiKey = {
      id: `key-${Date.now().toString(36)}`,
      label: label.trim() || "untitled-key",
      secret,
      scope,
      createdAt: new Date().toISOString(),
      lastUsed: null,
    };
    setKeys((ks) => [k, ...ks]);
    setFreshKey(k);
    setCreating(false);
    setLabel("");
    setScope("read");
  }

  function revoke(id: string) {
    setKeys((ks) => ks.map((k) => (k.id === id ? { ...k, revoked: true } : k)));
    toast("Key revoked — requests using it now return 401");
  }

  function mask(secret: string, show: boolean) {
    if (show) return secret;
    return secret.slice(0, 12) + "•".repeat(18);
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-7">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-sm font-semibold text-ink">API keys</h1>
          <p className="mt-1 max-w-lg text-xs leading-relaxed text-muted">
            Authenticate every request with a key in the{" "}
            <Mono className="text-hot">Authorization: Bearer</Mono> header. Keys carry a scope —
            read, write, or admin — and can be revoked instantly.
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreating(true)}>
          + Create key
        </Button>
      </div>

      {/* Fresh-key reveal — the only time the full secret is shown */}
      {freshKey && (
        <Card className="mt-5 border-hot/40 bg-hot/5 p-4 card-in">
          <div className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full bg-hot" />
            <Label className="text-hot">copy this now — it won't be shown again</Label>
          </div>
          <div className="mt-3 flex items-center gap-2">
            <code className="flex-1 truncate rounded-lg border border-hot/30 bg-canvas px-3 py-2 font-mono text-xs text-hot">
              {freshKey.secret}
            </code>
            <Copy text={freshKey.secret} label="copy key" />
          </div>
          <div className="mt-3 text-right">
            <Button size="sm" onClick={() => setFreshKey(null)}>
              I've saved it
            </Button>
          </div>
        </Card>
      )}

      {/* Create form */}
      {creating && (
        <Card className="mt-5 p-4 card-in">
          <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
            <label className="block">
              <Label className="mb-1.5 block">label</Label>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="production-server"
                className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
              />
            </label>
            <div>
              <Label className="mb-1.5 block">scope</Label>
              <div className="flex gap-1.5">
                {(["read", "write", "admin"] as const).map((s) => (
                  <Chip key={s} active={scope === s} onClick={() => setScope(s)} tone={SCOPE_TONE[s]}>
                    {s}
                  </Chip>
                ))}
              </div>
            </div>
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <Button size="sm" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" onClick={create}>
              Generate key
            </Button>
          </div>
        </Card>
      )}

      {/* Key table */}
      <div className="mt-5 overflow-hidden rounded-xl border border-edge">
        <div className="grid grid-cols-[1.4fr_2fr_auto_auto_auto] items-center gap-3 bg-elevated/60 px-4 py-2.5">
          {["label", "key", "scope", "last used", ""].map((h) => (
            <Label key={h}>{h}</Label>
          ))}
        </div>
        {keys.map((k) => (
          <div
            key={k.id}
            className={cx(
              "grid grid-cols-[1.4fr_2fr_auto_auto_auto] items-center gap-3 border-t border-edge px-4 py-3",
              k.revoked && "opacity-45"
            )}
          >
            <div className="min-w-0">
              <Mono className="block truncate text-xs text-ink">{k.label}</Mono>
              <span className="text-2xs text-subtle">created {ago(k.createdAt)}</span>
            </div>
            <div className="flex min-w-0 items-center gap-2">
              <code className="truncate font-mono text-2xs text-muted">
                {mask(k.secret, revealed === k.id && !k.revoked)}
              </code>
              {!k.revoked && (
                <button
                  onClick={() => setRevealed(revealed === k.id ? null : k.id)}
                  className="shrink-0 font-mono text-2xs text-subtle hover:text-ink"
                >
                  {revealed === k.id ? "hide" : "reveal"}
                </button>
              )}
            </div>
            <Chip tone={SCOPE_TONE[k.scope]}>{k.scope}</Chip>
            <Mono className="text-2xs text-subtle">{k.lastUsed ? ago(k.lastUsed) : "never"}</Mono>
            <div className="text-right">
              {k.revoked ? (
                <Chip tone="text-danger border-danger/40 bg-danger/10">revoked</Chip>
              ) : (
                <button
                  onClick={() => revoke(k.id)}
                  className="font-mono text-2xs text-subtle transition hover:text-danger"
                >
                  revoke
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6">
        <Label className="mb-2 block">use it</Label>
        <Code
          filename="curl"
          code={`curl https://atlas-prod.us-east-1.markvector.cloud/collections/support_docs/search \\
  -H "Authorization: Bearer mvk_live_7Qp3nR8sZ1vX4bK9wD2fL6hT0aG5eC" \\
  -H "Content-Type: application/json" \\
  -d '{"vector": [0.021, -0.44, 0.17, "…1533 more"], "top_k": 5}'`}
        />
      </div>
    </div>
  );
}
