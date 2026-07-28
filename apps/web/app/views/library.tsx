"use client";

import { useEffect, useRef, useState } from "react";
import {
  Facets,
  Item,
  api,
  confidenceTone,
  cx,
  preview,
  sourceTone,
  when,
} from "../lib";
import { Button, Chip, Empty, Spinner } from "../ui/kit";

/** The library: everything the knowledge base holds, filterable. */
export function Library({
  facets,
  onOpen,
  onIngested,
  filter,
  setFilter,
}: {
  facets: Facets | null;
  onOpen: (id: string) => void;
  onIngested: () => void;
  filter: { class_id?: string; source?: string };
  setFilter: (f: { class_id?: string; source?: string }) => void;
}) {
  const [items, setItems] = useState<Item[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const drop = useRef<HTMLLabelElement>(null);

  async function load() {
    const params = new URLSearchParams();
    if (filter.class_id) params.set("class_id", filter.class_id);
    if (filter.source) params.set("source", filter.source);
    const res = await api<{ items: Item[] }>(`/api/items?${params}`);
    setItems(res.items);
  }

  useEffect(() => {
    setItems(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.class_id, filter.source]);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    for (const file of Array.from(files)) {
      setBusy(file.name);
      const form = new FormData();
      form.append("file", file);
      form.append("source", "upload");
      try {
        await api("/api/items/file", { method: "POST", body: form });
      } catch (e) {
        alert(`${file.name}: ${(e as Error).message}`);
      }
    }
    setBusy(null);
    // Ingestion is asynchronous by design, so the row appears a moment later.
    setTimeout(() => {
      load();
      onIngested();
    }, 1200);
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* filter rail */}
      <div className="w-56 shrink-0 overflow-y-auto border-r border-edge px-4 py-5">
        <Rail
          title="Filed under"
          rows={(facets?.classes || []).map((c) => ({
            key: c.class_id,
            label: c.name,
            count: c.count,
          }))}
          active={filter.class_id}
          onPick={(k) => setFilter({ ...filter, class_id: k })}
        />
        <div className="h-5" />
        <Rail
          title="Source"
          rows={(facets?.sources || []).map((s) => ({
            key: s.source,
            label: s.source,
            count: s.count,
          }))}
          active={filter.source}
          onPick={(k) => setFilter({ ...filter, source: k })}
        />
      </div>

      {/* items */}
      <div className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
        <label
          ref={drop}
          onDragOver={(e) => {
            e.preventDefault();
            drop.current?.classList.add("border-accent/60");
          }}
          onDragLeave={() => drop.current?.classList.remove("border-accent/60")}
          onDrop={(e) => {
            e.preventDefault();
            drop.current?.classList.remove("border-accent/60");
            upload(e.dataTransfer.files);
          }}
          className="mb-5 flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-edge py-4 text-xs text-subtle transition hover:border-edgeStrong hover:text-muted"
        >
          <input
            type="file"
            multiple
            className="hidden"
            onChange={(e) => upload(e.target.files)}
          />
          {busy ? (
            <>
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
              Reading {busy}…
            </>
          ) : (
            <>Drop a document here, or click to choose — PDF, Markdown or text</>
          )}
        </label>

        {(filter.class_id || filter.source) && (
          <div className="mb-4 flex items-center gap-2">
            <span className="text-2xs text-subtle">Filtered by</span>
            {filter.class_id && (
              <Chip
                tone="text-accentSoft border-accent/40 bg-accent/10"
                onClick={() => setFilter({ ...filter, class_id: undefined })}
              >
                {facets?.classes.find((c) => c.class_id === filter.class_id)?.name ||
                  filter.class_id}{" "}
                ✕
              </Chip>
            )}
            {filter.source && (
              <Chip
                tone={sourceTone(filter.source)}
                onClick={() => setFilter({ ...filter, source: undefined })}
              >
                {filter.source} ✕
              </Chip>
            )}
          </div>
        )}

        {items === null ? (
          <Spinner label="Opening the library…" />
        ) : items.length === 0 ? (
          <Empty
            title="Nothing here yet"
            hint="Add a document above and it will be read, filed and made searchable. Nothing is generated for you — what you see is only ever what you put in."
          />
        ) : (
          <ul className="space-y-2">
            {items.map((item) => (
              <li key={item.id}>
                <button
                  onClick={() => onOpen(item.id)}
                  className="w-full animate-rise rounded-xl border border-edge bg-panel px-4 py-3.5 text-left transition hover:border-edgeStrong hover:bg-elevated"
                >
                  <div className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <h3 className="truncate text-sm font-medium text-ink">{item.title}</h3>
                      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-subtle">
                        {preview(item.body)}
                      </p>
                    </div>
                    <span className="shrink-0 text-2xs text-subtle">
                      {when(item.created_at)}
                    </span>
                  </div>
                  <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                    <Chip tone={sourceTone(item.source.source)}>{item.source.source}</Chip>
                    {(item.classes || []).map((c) => (
                      <Chip key={c.class_id} tone={confidenceTone(c.confidence, c.pinned)}>
                        {c.pinned && <span aria-hidden>📌</span>}
                        {c.name}
                      </Chip>
                    ))}
                    {item.version > 1 && (
                      <span className="text-2xs text-subtle">v{item.version}</span>
                    )}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Rail({
  title,
  rows,
  active,
  onPick,
}: {
  title: string;
  rows: { key: string; label: string; count: number }[];
  active?: string;
  onPick: (key: string | undefined) => void;
}) {
  return (
    <div>
      <h3 className="mb-2 text-2xs uppercase tracking-wide text-subtle">{title}</h3>
      {rows.length === 0 && <p className="text-2xs text-subtle">—</p>}
      <ul className="space-y-0.5">
        {rows.map((r) => (
          <li key={r.key}>
            <button
              onClick={() => onPick(active === r.key ? undefined : r.key)}
              className={cx(
                "flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-xs transition",
                active === r.key
                  ? "bg-accent/15 text-accentSoft"
                  : "text-muted hover:bg-elevated hover:text-ink"
              )}
            >
              <span className="truncate capitalize">{r.label}</span>
              <span className="ml-2 shrink-0 tabular-nums text-subtle">{r.count}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
