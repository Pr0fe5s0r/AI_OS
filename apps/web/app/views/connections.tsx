"use client";

import { useState } from "react";
import { Conn, Dot, Empty, OAuthTarget, Page, TextBtn } from "../lib";

/* Connections: the tools this company runs on. The list comes from the
   CONNECTOR REGISTRY (via /api/connections) — deriving it from the profile
   deadlocked a new workspace, which has no profile until events arrive.

   A source that offers OAuth (the API says which — never hardcoded here) is
   connected by signing in: no token is ever typed, pasted or held by this
   page. Sources without an OAuth flow still take a token. */

const FIELDS: Record<string, { key: string; label: string; placeholder: string }[]> = {
  github: [{ key: "repo", label: "Repository", placeholder: "owner/name" }],
  slack: [{ key: "channel", label: "Channel", placeholder: "#incidents" }],
  zendesk: [{ key: "subdomain", label: "Subdomain", placeholder: "acme" }],
};

const TOKEN_HINT: Record<string, string> = {
  github: "Optional. With a token: private repos, deeper history, real merge times.",
  slack: "Required. Bot token (xoxb-…) with channels:history + channels:read.",
  zendesk: "Required. Format: you@company.com/token:API_TOKEN",
};

type Props = {
  conns: Conn[];
  eventsBySource: Record<string, number>;
  busy: string | null;
  onConnect: (source: string, token: string, config: any) => void;
  onDisconnect: (source: string) => void;
  onSync: (source: string) => void;
  onOAuth: (source: string) => void;
  onPickTarget: (source: string, key: string) => void;
  targets: Record<string, OAuthTarget[]>;
};

function ToolCard({ c, count, busy, onConnect, onDisconnect, onSync, onOAuth, onPickTarget, targets }: {
  c: Conn; count: number; busy: string | null;
  onConnect: Props["onConnect"]; onDisconnect: Props["onDisconnect"]; onSync: Props["onSync"];
  onOAuth: Props["onOAuth"]; onPickTarget: Props["onPickTarget"]; targets: Record<string, OAuthTarget[]>;
}) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [cfg, setCfg] = useState<any>(c.config ?? {});
  const fields = FIELDS[c.source] ?? [];
  const picks = targets[c.source] ?? [];
  // which config field names the thing being watched (repo / channel / …), so
  // the "what's connected" label and the picker aren't hardcoded to GitHub
  const targetKey = fields[0]?.key ?? "repo";

  return (
    <div className="rounded-lg border border-edge bg-panel p-4">
      <div className="mb-3 flex h-7 w-7 items-center justify-center rounded-md border border-edge bg-elevated font-mono text-[13px] text-muted">
        {c.source[0].toUpperCase()}
      </div>
      <div className="mb-2 text-[14px] font-semibold capitalize">{c.source}</div>

      {c.connected ? (
        <>
          <div className="flex items-center gap-[7px] text-[12.5px] text-muted">
            <Dot color="#3fb950" size={6} />
            <span>
              {c.config?.[targetKey] ? `${c.config[targetKey]}` : "Connected"}
              {count > 0 ? ` · ${count.toLocaleString()} events` : ""}
            </span>
          </div>

          {/* after an OAuth grant the account is connected but nothing is
              chosen yet — pick what to watch from what the token can see */}
          {picks.length > 0 && (
            <select
              value={c.config?.[targetKey] ?? ""}
              onChange={(e) => onPickTarget(c.source, e.target.value)}
              className="mt-2.5 w-full rounded-md border border-edge bg-elevated px-2 py-1.5 text-[12px] text-ink outline-none focus:border-edgeStrong"
            >
              <option value="">Choose what to watch…</option>
              {picks.map((t) => (
                <option key={t.key} value={t.key}>
                  {t.label}{t.private ? " (private)" : ""}
                </option>
              ))}
            </select>
          )}

          <div className="mt-2.5 flex items-center gap-3 text-[12.5px]">
            <TextBtn onClick={() => onSync(c.source)}>
              {busy === `sync:${c.source}` ? "Syncing…" : "Sync"}
            </TextBtn>
            <TextBtn onClick={() => onDisconnect(c.source)}>Remove</TextBtn>
          </div>
        </>
      ) : c.oauth && c.oauth_configured ? (
        <>
          <button
            onClick={() => onOAuth(c.source)}
            className="flex-none whitespace-nowrap rounded-md bg-accent px-3 py-[5px] text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90"
          >
            Sign in with {c.source[0].toUpperCase() + c.source.slice(1)}
          </button>
          {/* No "use a token instead" escape hatch: where sign-in exists it is
              the only way in, and the API refuses a pasted token for exactly
              this source. Offering a door that the server slams is worse than
              not offering it. */}
          <div className="mt-2 text-[11px] leading-snug text-subtle">
            You approve access on {c.source}&apos;s own page — no token to copy, and MarkOS
            never sees your password. Revoke it any time from {c.source}&apos;s settings.
          </div>
        </>
      ) : open ? (
        <div className="flex flex-col gap-2.5">
          {fields.map((f) => (
            <div key={f.key}>
              <label className="mb-1 block text-[11.5px] text-muted">{f.label}</label>
              <input
                value={cfg[f.key] ?? ""}
                onChange={(e) => setCfg({ ...cfg, [f.key]: e.target.value })}
                placeholder={f.placeholder}
                className="w-full rounded-md border border-edge bg-elevated px-2.5 py-1.5 font-mono text-[12.5px] text-ink outline-none placeholder:text-subtle focus:border-edgeStrong"
              />
            </div>
          ))}
          <div>
            <label className="mb-1 block text-[11.5px] text-muted">API token</label>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="paste token"
              className="w-full rounded-md border border-edge bg-elevated px-2.5 py-1.5 font-mono text-[12.5px] text-ink outline-none placeholder:text-subtle focus:border-edgeStrong"
            />
            <div className="mt-1 text-[11px] leading-snug text-subtle">{TOKEN_HINT[c.source]}</div>
          </div>
          <div className="flex items-center gap-2.5">
            <button
              onClick={() => { onConnect(c.source, token, cfg); setOpen(false); setToken(""); }}
              disabled={!!busy}
              className="flex-none whitespace-nowrap rounded-md bg-accent px-3 py-[5px] text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-50"
            >
              {busy === `connect:${c.source}` ? "Saving…" : "Save"}
            </button>
            <TextBtn onClick={() => setOpen(false)}>Cancel</TextBtn>
          </div>
        </div>
      ) : (
        <>
          <button
            onClick={() => setOpen(true)}
            className="flex-none whitespace-nowrap rounded-md border border-edgeStrong bg-transparent px-3 py-[5px] text-[12.5px] text-muted hover:bg-elevated hover:text-ink"
          >
            Connect
          </button>
          {c.oauth && !c.oauth_configured && (
            <div className="mt-2 text-[11px] leading-snug text-subtle">
              Sign-in isn&apos;t set up yet — register an OAuth app and set
              {" "}{c.source.toUpperCase()}_CLIENT_ID/SECRET to enable it.
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function ConnectionsView({
  conns, eventsBySource, busy, onConnect, onDisconnect, onSync, onOAuth, onPickTarget, targets,
}: Props) {
  const live = conns.filter((c) => c.connected);
  const records = Object.values(eventsBySource).reduce((a, b) => a + b, 0);

  return (
    <Page
      title="Connections"
      purpose="The tools this workspace reads from. Connect one and MarkOS learns your work from it — nothing is sent back until you allow it."
      stats={[
        { label: live.length === 1 ? "tool connected" : "tools connected", value: live.length,
          tone: live.length ? "ok" : "idle" },
        { label: "records read", value: records, tone: records ? "ok" : "idle" },
      ]}
    >
      {live.length === 0 && (
        <div className="mb-4">
          <Empty
            title="Nothing is connected yet"
            next="Pick a tool below and sign in. MarkOS reads its recent history, works out what your work looks like, and starts watching for what needs you."
          />
        </div>
      )}
      <div className="grid grid-cols-3 gap-3">
        {conns.map((c) => (
          <ToolCard
            key={c.source}
            c={c}
            count={eventsBySource[c.source] ?? 0}
            busy={busy}
            onConnect={onConnect}
            onDisconnect={onDisconnect}
            onSync={onSync}
            onOAuth={onOAuth}
            onPickTarget={onPickTarget}
            targets={targets}
          />
        ))}
        <div className="rounded-lg border border-edge bg-panel p-4">
          <div className="mb-3 flex h-7 w-7 items-center justify-center rounded-md border border-edge bg-elevated font-mono text-[13px] text-muted">
            ＋
          </div>
          <div className="mb-0.5 text-[14px] font-semibold">Anything else</div>
          <div className="mb-2 text-[12px] text-subtle">push events · POST /api/ingest/push</div>
          <div className="text-[11.5px] leading-snug text-subtle">
            Any system can push raw payloads into the feed — the profile&apos;s mapping normalizes them.
          </div>
        </div>
      </div>
    </Page>
  );
}
