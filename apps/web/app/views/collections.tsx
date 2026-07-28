"use client";

import { useEffect, useState } from "react";
import { Collection, ago, cx, num } from "../data";
import { Button, Card, Chip, Empty, Label, Mono } from "../ui/kit";

export function Collections({
  collections,
  selected,
  onSelect,
  onQuery,
}: {
  collections: Collection[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onQuery: () => void;
}) {
  const active = collections.find((c) => c.id === selected) || null;

  useEffect(() => {
    if (!active && collections.length) onSelect(collections[0].id);
  }, [active, collections, onSelect]);

  if (!collections.length)
    return (
      <div className="mx-auto max-w-6xl px-6 py-7">
        <Empty
          title="No collections in this cluster yet"
          hint="A collection holds vectors of a fixed dimensionality and a distance metric. Create one, then upsert your embeddings."
          action={<Button variant="primary">+ Create collection</Button>}
        />
      </div>
    );

  return (
    <div className="mx-auto flex max-w-6xl gap-5 px-6 py-7">
      {/* List */}
      <div className="w-64 shrink-0 space-y-1.5">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink">Collections</h2>
          <Button size="sm">+ New</Button>
        </div>
        {collections.map((c) => (
          <button
            key={c.id}
            onClick={() => onSelect(c.id)}
            className={cx(
              "w-full rounded-lg border px-3 py-2.5 text-left transition",
              c.id === selected
                ? "border-accent/50 bg-accent/10"
                : "border-edge bg-panel hover:border-edgeStrong"
            )}
          >
            <Mono className="block truncate text-xs font-medium text-ink">{c.name}</Mono>
            <div className="mt-1 flex items-center gap-2 text-2xs text-subtle">
              <span>{num(c.points)} pts</span>
              <span>·</span>
              <span>{c.dims}d</span>
            </div>
          </button>
        ))}
      </div>

      {/* Detail */}
      {active && <CollectionDetail c={active} onQuery={onQuery} />}
    </div>
  );
}

function CollectionDetail({ c, onQuery }: { c: Collection; onQuery: () => void }) {
  const [tab, setTab] = useState<"schema" | "config">("schema");
  return (
    <div className="min-w-0 flex-1 space-y-4">
      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <Mono className="text-lg font-semibold text-ink">{c.name}</Mono>
            <div className="mt-1.5 flex items-center gap-2">
              <Chip>{c.metric}</Chip>
              <Chip>{c.dims} dims</Chip>
              <span className="text-2xs text-subtle">updated {ago(c.updatedAt)}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={onQuery}>
              Query
            </Button>
            <Button size="sm" variant="danger">
              Delete
            </Button>
          </div>
        </div>

        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ["points", num(c.points)],
            ["indexed", `${c.indexedPct}%`],
            ["dims", `${c.dims}`],
            ["size", `${(c.sizeMb / 1024).toFixed(2)}GB`],
          ].map(([l, v]) => (
            <div key={l} className="rounded-lg border border-edge bg-canvas px-3 py-2.5">
              <Mono className="text-base font-semibold tabular-nums text-ink">{v}</Mono>
              <Label className="mt-0.5 block">{l}</Label>
            </div>
          ))}
        </div>

        {c.indexedPct < 100 && (
          <div className="mt-4 flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" />
            <span className="text-2xs text-warn">
              Reindexing — {c.indexedPct}% of {num(c.points)} vectors built. Queries run on the
              indexed portion meanwhile.
            </span>
          </div>
        )}
      </Card>

      <Card className="overflow-hidden">
        <div className="flex gap-1 border-b border-edge px-3 pt-2">
          {(["schema", "config"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cx(
                "rounded-t-lg px-3 py-2 font-mono text-2xs uppercase tracking-wide transition",
                tab === t ? "bg-elevated text-ink" : "text-subtle hover:text-muted"
              )}
            >
              {t}
            </button>
          ))}
        </div>

        {tab === "schema" ? (
          <div className="p-4">
            <Label>payload fields</Label>
            <div className="mt-2 overflow-hidden rounded-lg border border-edge">
              <table className="w-full text-left">
                <thead className="bg-elevated/60">
                  <tr>
                    <th className="px-3 py-2 font-mono text-2xs uppercase tracking-wide text-subtle">field</th>
                    <th className="px-3 py-2 font-mono text-2xs uppercase tracking-wide text-subtle">type</th>
                    <th className="px-3 py-2 font-mono text-2xs uppercase tracking-wide text-subtle">filterable</th>
                  </tr>
                </thead>
                <tbody>
                  {[{ field: "vector", type: `float[${c.dims}]` }, ...c.schema].map((f, i) => (
                    <tr key={i} className="border-t border-edge">
                      <td className="px-3 py-2 font-mono text-xs text-ink">{f.field}</td>
                      <td className="px-3 py-2 font-mono text-xs text-heat-2">{f.type}</td>
                      <td className="px-3 py-2 font-mono text-2xs text-subtle">
                        {f.field === "vector" ? "—" : "yes"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            {[
              ["distance metric", c.metric],
              ["index type", "HNSW"],
              ["m / ef_construct", "16 / 128"],
              ["on-disk payload", "true"],
              ["replication factor", "2"],
              ["quantization", c.dims > 1000 ? "scalar (int8)" : "none"],
            ].map(([l, v]) => (
              <div
                key={l}
                className="flex items-center justify-between rounded-lg border border-edge bg-canvas px-3 py-2.5"
              >
                <Label>{l}</Label>
                <Mono className="text-xs text-ink">{v}</Mono>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
