"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Facets, Item, api, confidenceTone, cx, sourceTone, when } from "../lib";
import { Empty } from "../ui/kit";

/** The index, as a table.
 *
 *  This is a store of records, so it is shown the way a store of records is
 *  read: dense rows, sortable columns, counts you can trust. A feed of cards
 *  would imply the newest thing matters most, which is true of an activity
 *  log and false of a knowledge base — here the question is almost always
 *  "what do we hold about X", not "what happened last". */

type Pending = { name: string; state: "reading" | "failed"; detail?: string };

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
  const [pending, setPending] = useState<Pending[]>([]);
  const [q, setQ] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  async function load(): Promise<Item[]> {
    const params = new URLSearchParams();
    if (filter.class_id) params.set("class_id", filter.class_id);
    if (filter.source) params.set("source", filter.source);
    params.set("limit", "200");
    const res = await api<{ items: Item[] }>(`/api/items?${params}`);
    setItems(res.items);
    return res.items;
  }

  useEffect(() => {
    setItems(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.class_id, filter.source]);

  /** Ingestion is asynchronous, so the row does not exist the moment the
   *  upload returns. Waiting a fixed second and reloading made a successful
   *  upload look like nothing happened — so the file is shown as a row from
   *  the start, and we poll until it really lands. */
  async function upload(files: FileList | null) {
    if (!files?.length) return;
    const chosen = Array.from(files);
    setPending((p) => [...p, ...chosen.map((f) => ({ name: f.name, state: "reading" as const }))]);

    const before = new Set((items || []).map((i) => i.id));

    await Promise.all(
      chosen.map(async (file) => {
        const form = new FormData();
        form.append("file", file);
        form.append("source", "upload");
        try {
          await api("/api/items/file", { method: "POST", body: form });
        } catch (e) {
          setPending((p) =>
            p.map((x) =>
              x.name === file.name
                ? { ...x, state: "failed", detail: (e as Error).message }
                : x
            )
          );
        }
      })
    );

    // Poll for the rows rather than guessing at a delay — and keep polling
    // past the moment they appear. Writing the item and filing it are separate
    // jobs, so stopping at "the row exists" showed every new document as
    // uncategorised and never corrected itself.
    for (let attempt = 0; attempt < 16; attempt++) {
      await new Promise((r) => setTimeout(r, 1200));
      const fresh = await load();
      const arrived = fresh.filter((i) => !before.has(i.id));
      if (arrived.length >= chosen.length) {
        setPending((p) => p.filter((x) => x.state === "failed"));
        if (arrived.every((i) => (i.classes || []).length > 0)) break;
      }
    }
    setPending((p) => p.filter((x) => x.state === "failed"));
    onIngested();
    if (fileRef.current) fileRef.current.value = ""; // same file can be re-picked
  }

  const rows = useMemo(() => {
    if (!items) return null;
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (i) =>
        i.title.toLowerCase().includes(needle) ||
        i.source.locator.toLowerCase().includes(needle)
    );
  }, [items, q]);

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDragging(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        upload(e.dataTransfer.files);
      }}
    >
      {/* counts across the top: the shape of the store, at a glance */}
      <div className="flex shrink-0 items-stretch gap-px border-b border-edge bg-edge">
        <Metric label="Documents" value={facets?.total ?? "—"} />
        <Metric label="Categories in use" value={facets?.classes.length ?? "—"} />
        <Metric label="Sources" value={facets?.sources.length ?? "—"} />
        <Metric
          label="Assignments"
          value={facets ? facets.classes.reduce((a, c) => a + c.count, 0) : "—"}
        />
      </div>

      {/* toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge px-4 py-2.5">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by title or path…"
          className="w-64 rounded-lg border border-edge bg-canvas px-3 py-1.5 text-xs text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
        />
        <Select
          value={filter.class_id || ""}
          onChange={(v) => setFilter({ ...filter, class_id: v || undefined })}
          placeholder="All categories"
          options={(facets?.classes || []).map((c) => ({
            value: c.class_id,
            label: `${c.name} (${c.count})`,
          }))}
        />
        <Select
          value={filter.source || ""}
          onChange={(v) => setFilter({ ...filter, source: v || undefined })}
          placeholder="All sources"
          options={(facets?.sources || []).map((s) => ({
            value: s.source,
            label: `${s.source} (${s.count})`,
          }))}
        />
        <span className="ml-auto text-2xs tabular-nums text-subtle">
          {rows ? `${rows.length} shown` : ""}
        </span>
        <input
          ref={fileRef}
          type="file"
          multiple
          // sr-only, not display:none — a hidden input can be skipped by the
          // label click that is supposed to open the picker, which is exactly
          // why uploading appeared to do nothing.
          className="sr-only"
          onChange={(e) => upload(e.target.files)}
        />
        <button
          onClick={() => fileRef.current?.click()}
          className="rounded-lg border border-accent bg-accent px-3 py-1.5 text-xs font-medium text-white transition hover:bg-accentSoft"
        >
          Add documents
        </button>
      </div>

      {/* table */}
      <div className="min-h-0 flex-1 overflow-auto">
        {dragging && (
          <div className="pointer-events-none sticky top-0 z-10 border-b border-accent/50 bg-accent/10 px-4 py-2 text-center text-xs text-accentSoft">
            Drop to add to the knowledge base
          </div>
        )}

        {rows === null ? (
          <div className="px-4 py-8 text-xs text-subtle">Loading the index…</div>
        ) : rows.length === 0 && pending.length === 0 ? (
          <div className="px-4 py-10">
            <Empty
              title={q || filter.class_id || filter.source ? "No matches" : "The index is empty"}
              hint={
                q || filter.class_id || filter.source
                  ? "Nothing here matches those filters."
                  : "Add a document and it will be read, filed and indexed. Nothing is generated for you — the index holds only what you put in it."
              }
            />
          </div>
        ) : (
          <table className="w-full border-collapse text-left">
            <thead className="sticky top-0 z-[1] bg-canvas">
              <tr className="border-b border-edge text-2xs uppercase tracking-wide text-subtle">
                <Th className="pl-4">Document</Th>
                <Th>Source</Th>
                <Th>Categories</Th>
                <Th className="text-right">Ver</Th>
                <Th className="pr-4 text-right">Added</Th>
              </tr>
            </thead>
            <tbody>
              {pending.map((p) => (
                <tr key={p.name} className="border-b border-edge/60">
                  <td className="py-2.5 pl-4">
                    <div className="flex items-center gap-2">
                      {p.state === "reading" ? (
                        <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                      ) : (
                        <span className="text-danger">✕</span>
                      )}
                      <span className="text-xs text-muted">{p.name}</span>
                    </div>
                  </td>
                  <td colSpan={4} className="py-2.5 pr-4 text-2xs text-subtle">
                    {p.state === "reading" ? "reading and indexing…" : p.detail}
                  </td>
                </tr>
              ))}

              {rows.map((item) => (
                <tr
                  key={item.id}
                  onClick={() => onOpen(item.id)}
                  className="cursor-pointer border-b border-edge/60 transition hover:bg-elevated"
                >
                  <td className="max-w-0 py-2.5 pl-4 pr-3">
                    <div className="truncate text-xs font-medium text-ink">{item.title}</div>
                    <div className="truncate font-mono text-2xs text-subtle">
                      {item.source.locator}
                    </div>
                  </td>
                  <td className="py-2.5 pr-3">
                    <span
                      className={cx(
                        "rounded border px-1.5 py-0.5 text-2xs",
                        sourceTone(item.source.source)
                      )}
                    >
                      {item.source.source}
                    </span>
                  </td>
                  <td className="py-2.5 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {(item.classes || []).length === 0 && (
                        <span className="text-2xs text-subtle">—</span>
                      )}
                      {(item.classes || []).map((c) => (
                        <span
                          key={c.class_id}
                          title={c.pinned ? `Set by ${c.actor}` : c.basis || ""}
                          className={cx(
                            "rounded border px-1.5 py-0.5 text-2xs",
                            confidenceTone(c.confidence, c.pinned)
                          )}
                        >
                          {c.pinned && "📌 "}
                          {c.name}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="py-2.5 pr-3 text-right font-mono text-2xs text-subtle">
                    {item.version}
                  </td>
                  <td className="whitespace-nowrap py-2.5 pr-4 text-right text-2xs text-subtle">
                    {when(item.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex-1 bg-canvas px-4 py-3">
      <div className="text-lg font-semibold tabular-nums text-ink">{value}</div>
      <div className="text-2xs uppercase tracking-wide text-subtle">{label}</div>
    </div>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cx("py-2 pr-3 font-medium", className)}>{children}</th>;
}

function Select({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-edge bg-canvas px-2.5 py-1.5 text-xs text-ink outline-none focus:border-accent/60"
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
