"use client";

import { useState } from "react";
import * as api from "../api";
import { cx, ms } from "../data";
import { Button, Card, Chip, Code, Label, Mono } from "../ui/kit";

type Endpoint = "search" | "answer";

const ENDPOINTS: { id: Endpoint; label: string; path: string; blurb: string }[] = [
  {
    id: "search",
    label: "Search",
    path: "/api/search",
    blurb: "Ranked passages only — the contract other software builds on. No generated prose.",
  },
  {
    id: "answer",
    label: "Answer",
    path: "/api/answer",
    blurb: "Retrieval plus a written answer built only from what was retrieved, with citations.",
  },
];

/** Playground — the API, run by hand.
 *
 *  Not another chat window: this is the developer's console. It sends the exact
 *  request the SDK would, against the collection selected in the sidebar, and
 *  shows what came back verbatim — status, latency and the raw JSON — next to
 *  the curl that reproduces it. The key is never interpolated into the curl; it
 *  reads from the environment, because this text gets pasted into tickets. */
export function Playground({ active }: { active: string | null }) {
  const [endpoint, setEndpoint] = useState<Endpoint>("search");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(8);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<api.RawRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  const chosen = ENDPOINTS.find((e) => e.id === endpoint)!;
  const path = `${chosen.path}?q=${encodeURIComponent(q || "")}&limit=${limit}`;
  const base = api.API;

  const curl = [
    `curl -H "Authorization: Bearer $KB_API_KEY" \\`,
    active ? `     -H "X-Collection: ${active}" \\` : null,
    `     "${base}${chosen.path}?q=${encodeURIComponent(q || "your question")}&limit=${limit}"`,
  ]
    .filter(Boolean)
    .join("\n");

  async function go() {
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.run(path, active ?? undefined));
    } catch (e) {
      setError((e as Error).message);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-6 py-6">
      <div className="mb-5">
        <h1 className="text-base font-semibold text-ink">Playground</h1>
        <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
          Run the read API by hand and see exactly what it returns. Requests go to the
          collection selected in the sidebar —{" "}
          {active ? (
            <>
              scoped to <Mono className="text-accentSoft">{active}</Mono>
            </>
          ) : (
            "across the whole workspace"
          )}
          .
        </p>
      </div>

      {/* endpoint + scope */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
          {ENDPOINTS.map((e) => (
            <button
              key={e.id}
              onClick={() => setEndpoint(e.id)}
              className={cx(
                "rounded-md px-3 py-1 font-mono text-2xs transition",
                endpoint === e.id ? "bg-accent/15 text-ink" : "text-subtle hover:text-muted"
              )}
            >
              {e.label}
            </button>
          ))}
        </div>
        <Chip tone="text-heat-2 border-heat-2/30 bg-heat-2/10">
          GET {chosen.path}
        </Chip>
        <Chip>{active ? `collection: ${active}` : "workspace-wide"}</Chip>
      </div>

      <p className="mb-4 max-w-xl text-2xs leading-relaxed text-subtle">{chosen.blurb}</p>

      {/* request */}
      <Card className="mb-4 p-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            go();
          }}
          className="flex flex-wrap items-end gap-3"
        >
          <label className="min-w-[18rem] flex-1">
            <Label className="mb-1.5 block">q</Label>
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="refund policy"
              className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent/60"
            />
          </label>
          <label className="w-24">
            <Label className="mb-1.5 block">limit</Label>
            <input
              type="number"
              min={1}
              max={endpoint === "answer" ? 20 : 100}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
              className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
            />
          </label>
          <Button variant="primary" onClick={go} disabled={busy || !q.trim()}>
            {busy ? "Running…" : "Run"}
          </Button>
        </form>
      </Card>

      {/* the curl that reproduces it */}
      <div className="mb-4">
        <Label className="mb-2 block">reproduce</Label>
        <Code code={curl} filename="curl" />
      </div>

      {/* response */}
      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-2xs text-danger">
          {error}
        </p>
      )}
      {result && (
        <div>
          <div className="mb-2 flex items-center gap-2">
            <Label>response</Label>
            <Chip
              tone={
                result.ok
                  ? "text-success border-success/30 bg-success/10"
                  : "text-danger border-danger/30 bg-danger/10"
              }
            >
              {result.status}
            </Chip>
            <Mono className="text-2xs text-subtle">{ms(result.ms)}</Mono>
          </div>
          <Code code={JSON.stringify(result.body, null, 2)} filename="response.json" lang="json" />
        </div>
      )}
    </div>
  );
}
