"use client";

import { useEffect, useState } from "react";
import * as api from "../api";
import { Collection, CollectionDetail, Document, ago, bytes, cx, num } from "../data";
import { CollectionGraph } from "../ui/graph";
import { Button, Card, Chip, Empty, Label, Mono, Stat } from "../ui/kit";

/** Collections, and what is inside one.
 *
 *  The numbers here come from the store rather than from a summary table, so
 *  "items" means active items — superseded versions are counted separately
 *  because a collection that looks twice its size is a collection nobody
 *  trusts. */
export function Collections({
  collections,
  selected,
  onSelect,
  onQuery,
  onChanged,
  toast,
}: {
  collections: Collection[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onQuery: () => void;
  onChanged: () => Promise<void> | void;
  toast: (m: string) => void;
}) {
  const [detail, setDetail] = useState<CollectionDetail | null>(null);
  const [docs, setDocs] = useState<Document[] | null>(null);
  const [failed, setFailed] = useState<{ locator: string; reason: string }[]>([]);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [view, setView] = useState<"graph" | "map" | "table">("graph");
  const [k, setK] = useState(3);
  const [shape, setShape] = useState<api.CollectionShape | null>(null);
  const [opened, setOpened] = useState<api.ChunkDetail | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    if (!selected) {
      setDetail(null);
      setDocs(null);
      return;
    }
    setDetail(null);
    setDocs(null);
    setFailed([]);
    (async () => {
      const [d, items, bad] = await Promise.all([
        api.collection(selected),
        api.documents(selected, 100),
        api.failures(selected).catch(() => []),
      ]);
      if (!live) return;
      setDetail(d);
      setDocs(items);
      setFailed(bad);
    })();
    return () => {
      live = false;
    };
  }, [selected]);

  // The graph is loaded separately from the detail: it is the slower of the
  // two (it reads every vector in the collection), and the stats should not
  // wait on it.
  useEffect(() => {
    let live = true;
    setShape(null);
    setOpened(null);
    setOpening(null);
    if (!selected || view === "table") return;
    api
      .collectionGraph(selected, k)
      .then((s) => live && setShape(s))
      .catch(
        () =>
          live &&
          setShape({
            nodes: [],
            edges: [],
            truncated: false,
            k,
            floor: 0,
            documents: 0,
            projection: { method: "none", explained_variance: 0 },
          })
      );
    return () => {
      live = false;
    };
  }, [selected, view, k]);

  /** Fetch the passage a point stands for. Requested on click rather than
   *  shipped with the graph: four hundred passages of text would be most of a
   *  megabyte to draw a few hundred circles. */
  async function openPassage(chunkId: string) {
    setOpening(chunkId);
    setOpened(null);
    try {
      const detail = await api.chunk(chunkId);
      setOpened(detail);
    } catch (e) {
      toast((e as Error).message);
      setOpening(null);
    }
  }

  async function create() {
    if (!name.trim()) return;
    try {
      const made = await api.createCollection(name.trim());
      setName("");
      setCreating(false);
      await onChanged();
      onSelect(made.collection_id);
      toast("Collection created");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function drop(c: Collection) {
    if (
      !confirm(
        `Delete "${c.name}"?\n\nIts ${num(c.items)} document(s) go with it. This cannot be undone.`
      )
    )
      return;
    const { items_removed } = await api.deleteCollection(c.id);
    onSelect(null);
    await onChanged();
    toast(`Deleted — ${items_removed} document(s) removed`);
  }

  // ------------------------------ one collection ------------------------------

  if (selected) {
    return (
      <div className="px-6 py-6">
        <button
          onClick={() => onSelect(null)}
          className="mb-4 font-mono text-2xs text-subtle transition hover:text-ink"
        >
          ← all collections
        </button>

        {!detail ? (
          <div className="font-mono text-xs text-subtle">Loading collection…</div>
        ) : (
          <>
            <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
              <div>
                <h1 className="font-mono text-base font-semibold text-ink">{detail.name}</h1>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  <Chip>{detail.id}</Chip>
                  <Chip tone="text-heat-2 border-heat-2/30 bg-heat-2/10">cosine</Chip>
                  <Chip>{detail.dimensions} dims</Chip>
                  <span className="font-mono text-2xs text-subtle">
                    {detail.embeddingModel || "no model set"}
                  </span>
                </div>
              </div>
              <div className="flex gap-2">
                <Button onClick={onQuery}>Query it</Button>
                <Button variant="danger" onClick={() => drop(detail)}>
                  Delete
                </Button>
              </div>
            </div>

            <Card className="mb-5 flex flex-wrap gap-x-10 gap-y-4 p-4">
              <Stat value={num(detail.stats.items)} label="documents" />
              <Stat value={num(detail.stats.sources)} label="sources" />
              <Stat value={bytes(detail.stats.characters)} label="stored text" />
              <Stat
                value={num(detail.stats.superseded)}
                label="superseded"
                tone={detail.stats.superseded ? "text-muted" : undefined}
              />
              <Stat
                value={num(detail.stats.failed)}
                label="failed"
                tone={detail.stats.failed ? "text-danger" : undefined}
              />
            </Card>

            {/* A count with no reason is the same defect as no count at all:
                the person who uploaded the file still cannot tell what to fix. */}
            {failed.length > 0 && (
              <Card className="mb-5 border-danger/30 bg-danger/5 p-4">
                <Label className="mb-2 block text-danger">
                  {failed.length} file{failed.length > 1 ? "s" : ""} could not be read
                </Label>
                <div className="space-y-1.5">
                  {failed.map((f) => (
                    <div key={f.locator} className="flex flex-wrap items-baseline gap-x-2">
                      <Mono className="text-xs text-ink">{f.locator}</Mono>
                      <span className="text-2xs text-muted">{f.reason}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-2.5 text-2xs text-subtle">
                  Nothing was indexed from these, so they cannot be retrieved. Fix the file
                  and upload it again — it will land on the same record rather than adding
                  a second one.
                </p>
              </Card>
            )}

            <div className="mb-2 flex items-center justify-between">
              <Label>
                {view === "graph"
                  ? "neighbour graph"
                  : view === "map"
                    ? "embedding map"
                    : "documents"}
                {view !== "table" && shape && shape.nodes.length > 0 && (
                  <span className="ml-2 normal-case tracking-normal text-subtle">
                    {shape.nodes.length} passages · {shape.documents} document
                    {shape.documents === 1 ? "" : "s"}
                  </span>
                )}
              </Label>
              <div className="flex items-center gap-3">
                {view !== "table" && (
                  <div className="flex items-center gap-1.5">
                    <Label>k</Label>
                    {[2, 3, 5].map((n) => (
                      <Chip key={n} active={k === n} onClick={() => setK(n)}>
                        {n}
                      </Chip>
                    ))}
                  </div>
                )}
                <div className="flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
                  {(["graph", "map", "table"] as const).map((v) => (
                    <button
                      key={v}
                      onClick={() => setView(v)}
                      className={cx(
                        "rounded-md px-2.5 py-1 font-mono text-2xs transition",
                        view === v ? "bg-accent/15 text-ink" : "text-subtle hover:text-muted"
                      )}
                    >
                      {v}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {view !== "table" ? (
              shape === null ? (
                <div className="flex h-64 items-center justify-center rounded-xl border border-edge font-mono text-2xs text-subtle">
                  building the neighbour graph…
                </div>
              ) : (
                <>
                  <CollectionGraph
                    nodes={shape.nodes}
                    edges={shape.edges}
                    mode={view === "map" ? "map" : "graph"}
                    onOpen={openPassage}
                  />

                  {/* A point that cannot say what it represents is decoration.
                      Clicking one opens the passage it stands for. */}
                  {(opening || opened) && (
                    <Card className="card-in mt-2 p-4">
                      {opening && !opened ? (
                        <div className="font-mono text-2xs text-subtle">reading the passage…</div>
                      ) : opened ? (
                        <>
                          <div className="mb-2 flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="truncate text-xs font-medium text-ink">
                                {opened.heading || `passage ${opened.ordinal + 1}`}
                              </div>
                              <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                                {opened.document && (
                                  <Mono className="text-2xs text-subtle">{opened.document}</Mono>
                                )}
                                <Chip>passage {opened.ordinal + 1}</Chip>
                                <Chip>stage {opened.stage}</Chip>
                                {opened.node_type === "summary" && (
                                  <Chip tone="text-heat-2 border-heat-2/40 bg-heat-2/10">
                                    written by the store · from {opened.merged_from.length} passages
                                  </Chip>
                                )}
                                {opened.archived && (
                                  <Chip tone="text-hot border-hot/40 bg-hot/10">archived</Chip>
                                )}
                              </div>
                            </div>
                            <button
                              onClick={() => {
                                setOpened(null);
                                setOpening(null);
                              }}
                              className="shrink-0 rounded-md border border-edge px-2 py-0.5 font-mono text-2xs text-subtle transition hover:text-ink"
                            >
                              close
                            </button>
                          </div>
                          <p className="max-h-52 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted">
                            {opened.text}
                          </p>
                          {opened.node_type === "summary" && (
                            <p className="mt-2 border-t border-edge pt-2 text-2xs text-subtle">
                              This text was written by the store from other passages, not taken
                              from a document.
                            </p>
                          )}
                        </>
                      ) : null}
                    </Card>
                  )}
                  <p className="mt-2 text-2xs leading-relaxed text-subtle">
                    {view === "map" ? (
                      shape.projection.method === "none" ? (
                        <>
                          Too few passages to place meaningfully — two points always sit on a
                          line, whatever the vectors say, so no projection was run and these
                          positions carry no meaning. Add more content and the map becomes
                          readable.
                        </>
                      ) : (
                        <>
                          Every passage placed by its actual position in embedding space, so
                          distance on screen is distance in the vectors — two points near each
                          other really are alike. Flattening 1536 dimensions to two loses
                          something, and these axes keep{" "}
                          <span className="text-muted">
                            {(shape.projection.explained_variance * 100).toFixed(1)}%
                          </span>{" "}
                          of the variance.
                        </>
                      )
                    ) : (
                      <>
                        Each point is a <span className="text-muted">passage</span>, not a
                        document — passages are what was embedded, so they are what can honestly
                        be drawn. Coloured edges join a passage to its {shape.k} nearest
                        neighbours by cosine similarity; faint dotted edges join consecutive
                        passages of the same file. Positions come from a force layout and carry
                        no meaning; switch to the map to read distance. Edges below{" "}
                        <span className="text-muted">{shape.floor}</span> similarity are dropped.
                      </>
                    )}
                    {shape.truncated && " Showing the first 400 passages."}
                  </p>
                </>
              )
            ) : docs === null ? (
              <div className="font-mono text-xs text-subtle">Loading…</div>
            ) : docs.length === 0 ? (
              <Empty
                title="This collection is empty"
                hint="Upload a file or write to it with the SDK. Nothing is generated for you."
              />
            ) : (
              <Card className="overflow-hidden">
                <table className="w-full text-left">
                  <thead>
                    <tr className="border-b border-edge">
                      <Th className="pl-4">Document</Th>
                      <Th>Source</Th>
                      <Th>Categories</Th>
                      <Th className="text-right">Ver</Th>
                      <Th className="pr-4 text-right">Added</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {docs.map((d) => (
                      <tr key={d.id} className="border-b border-edge/60 last:border-0">
                        <td className="max-w-0 py-2.5 pl-4 pr-3">
                          <div className="truncate text-xs text-ink">{d.title}</div>
                          <div className="truncate font-mono text-2xs text-subtle">
                            {d.locator}
                          </div>
                        </td>
                        <td className="py-2.5 pr-3">
                          <Chip>{d.source}</Chip>
                        </td>
                        <td className="py-2.5 pr-3">
                          <div className="flex flex-wrap gap-1">
                            {d.categories.length === 0 && (
                              <span className="font-mono text-2xs text-subtle">—</span>
                            )}
                            {d.categories.map((c) => (
                              <Chip
                                key={c.class_id}
                                title={c.pinned ? "set by a person" : c.basis || ""}
                                tone={
                                  c.pinned
                                    ? "text-accentSoft border-accent/40 bg-accent/10"
                                    : "text-muted border-edgeStrong bg-elevated"
                                }
                              >
                                {c.pinned && "📌 "}
                                {c.name}
                              </Chip>
                            ))}
                          </div>
                        </td>
                        <td className="py-2.5 pr-3 text-right font-mono text-2xs text-subtle">
                          {d.version}
                        </td>
                        <td className="whitespace-nowrap py-2.5 pr-4 text-right font-mono text-2xs text-subtle">
                          {ago(d.createdAt)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </>
        )}
      </div>
    );
  }

  // ------------------------------- the listing -------------------------------

  return (
    <div className="px-6 py-6">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Collections</h1>
          <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
            A collection holds documents and their vectors. Its embedding model and
            dimensions are fixed when it is made — changing either would invalidate
            everything inside it.
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreating(true)}>
          + New collection
        </Button>
      </div>

      {creating && (
        <Card className="card-in mb-5 flex flex-wrap items-end gap-3 p-4">
          <label className="min-w-[16rem] flex-1">
            <Label className="mb-1.5 block">Name</Label>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create()}
              placeholder="Client research"
              className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent/60"
            />
          </label>
          <Button variant="primary" onClick={create} disabled={!name.trim()}>
            Create
          </Button>
          <Button onClick={() => setCreating(false)}>Cancel</Button>
        </Card>
      )}

      {collections.length === 0 ? (
        <Empty
          title="No collections in this cluster"
          hint="Create one, then upload a document or write to it with the SDK."
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              Create a collection
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {collections.map((c) => (
            <Card key={c.id} hover onClick={() => onSelect(c.id)} className="p-4">
              <div className="flex items-start justify-between gap-2">
                <Mono className="truncate text-sm font-medium text-ink">{c.name}</Mono>
                <Chip>{c.dimensions}d</Chip>
              </div>
              <div className="mt-1 truncate font-mono text-2xs text-subtle">{c.id}</div>
              <div className="mt-4 flex items-baseline gap-1.5">
                <span className="font-mono text-lg font-semibold tabular-nums text-ink">
                  {num(c.items)}
                </span>
                <Label>documents</Label>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return (
    <th
      className={cx(
        "py-2 pr-3 font-mono text-2xs font-medium uppercase tracking-[0.14em] text-subtle",
        className
      )}
    >
      {children}
    </th>
  );
}
