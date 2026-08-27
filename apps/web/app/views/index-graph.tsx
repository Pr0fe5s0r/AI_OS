"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import { cx, num } from "../data";
import { CollectionGraph } from "../ui/graph";
import { Button, Card, Chip, Empty, Label, Stat } from "../ui/kit";

type Mode = "graph" | "map";

/** The contents of one collection as they actually exist in the index.
 *  A point is an indexed passage; edges are similarity or document-order
 *  relationships. Nothing illustrative is generated for this view. */
export function IndexGraph({
  active,
  onUpload,
}: {
  active: string | null;
  onUpload: () => void;
}) {
  const [shape, setShape] = useState<api.CollectionShape | null>(null);
  const [mode, setMode] = useState<Mode>("graph");
  const [k, setK] = useState(5);
  const [error, setError] = useState<string | null>(null);
  const [opened, setOpened] = useState<api.ChunkDetail | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const openRequest = useRef(0);

  useEffect(() => {
    let live = true;
    setShape(null);
    setError(null);
    setOpened(null);
    setOpening(null);
    setSelectedId(null);
    if (!active) return () => {
      live = false;
    };

    api.collectionGraph(active, k).then(
      (next) => live && setShape(next),
      (cause) => {
        if (!live) return;
        setError(cause instanceof Error ? cause.message : "The index graph could not be loaded.");
        setShape({
          nodes: [],
          edges: [],
          truncated: false,
          k,
          floor: 0,
          documents: 0,
          projection: { method: "none", explained_variance: 0 },
        });
      }
    );

    return () => {
      live = false;
    };
  }, [active, k]);

  async function openPassage(chunkId: string) {
    const request = ++openRequest.current;
    setOpening(chunkId);
    setOpened(null);
    try {
      const detail = await api.chunk(chunkId);
      if (request === openRequest.current) setOpened(detail);
    } catch (cause) {
      if (request === openRequest.current) {
        setError(cause instanceof Error ? cause.message : "That passage could not be opened.");
      }
    } finally {
      if (request === openRequest.current) setOpening(null);
    }
  }

  const visible = useMemo(() => {
    if (!shape) return { nodes: [], edges: [] };
    const needle = search.trim().toLowerCase();
    const nodes = needle
      ? shape.nodes.filter((node) =>
          [node.title, node.document, node.categoryName, node.source]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(needle)
        )
      : shape.nodes;
    const ids = new Set(nodes.map((node) => node.id));
    return { nodes, edges: shape.edges.filter((edge) => ids.has(edge.src) && ids.has(edge.dst)) };
  }, [shape, search]);

  const selected = shape?.nodes.find((node) => node.id === selectedId) ?? null;

  function selectNode(id: string | null) {
    setSelectedId(id);
    if (id === null) {
      openRequest.current += 1;
      setOpening(null);
      setOpened(null);
    } else {
      openPassage(id);
    }
  }

  return (
    <div className="mx-auto max-w-[1700px] px-5 py-6 sm:px-6">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Index graph</h1>
          <p className="mt-1 max-w-2xl text-xs leading-5 text-muted">
            See what was indexed. Each point is a passage; connections show nearby meaning or
            consecutive passages from the same document.
          </p>
        </div>
        {active && <Chip tone="border-accent/40 bg-accent/10 text-accentSoft">{active}</Chip>}
      </header>

      {!active ? (
        <Empty
          title="Choose one collection"
          hint="The graph needs a single collection. Select one from the collection menu in the sidebar."
        />
      ) : shape === null ? (
        <div className="flex h-80 items-center justify-center rounded-xl border border-edge font-mono text-2xs text-subtle">
          <span className="mr-2 h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
          Reading indexed passages…
        </div>
      ) : error && shape.nodes.length === 0 ? (
        <div role="alert" className="rounded-xl border border-danger/30 bg-danger/10 px-5 py-8 text-center">
          <p className="text-sm text-danger">The index graph could not be loaded.</p>
          <p className="mt-1 text-xs text-muted">{error}</p>
        </div>
      ) : shape.nodes.length === 0 ? (
        <Empty
          title="Nothing is indexed yet"
          hint="Upload a document first. Its indexed passages will appear here automatically."
          action={<Button variant="primary" onClick={onUpload}>Upload a document</Button>}
        />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
            <div className="flex flex-wrap gap-x-8 gap-y-3">
              <Stat value={num(shape.documents)} label="documents" />
              <Stat value={num(shape.nodes.length)} label="indexed passages" />
              <Stat value={num(shape.edges.length)} label="connections" />
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <input
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  selectNode(null);
                }}
                placeholder="Find a passage or document"
                className="w-56 rounded-lg border border-edge bg-canvas px-3 py-2 text-xs text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
              />
              <div className="flex items-center gap-1.5">
                <Label>Neighbours</Label>
                {[2, 3, 5].map((value) => (
                  <Chip key={value} active={k === value} onClick={() => setK(value)}>
                    {value}
                  </Chip>
                ))}
              </div>
              <div className="flex rounded-lg border border-edge bg-elevated p-0.5">
                {(["graph", "map"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setMode(value)}
                    className={cx(
                      "rounded-md px-3 py-1.5 text-xs transition focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                      mode === value ? "bg-raised text-ink" : "text-subtle hover:text-muted"
                    )}
                  >
                    {value === "graph" ? "Connections" : "Meaning map"}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_18rem]">
            <CollectionGraph
              nodes={visible.nodes}
              edges={visible.edges}
              mode={mode}
              selectedId={selectedId}
              onSelect={selectNode}
            />
            <Card className="h-fit p-4 xl:sticky xl:top-4">
              {selected ? (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <Label>passage details</Label>
                    <button
                      type="button"
                      onClick={() => selectNode(null)}
                      className="rounded-md border border-edge px-2 py-1 font-mono text-2xs text-subtle transition hover:text-ink"
                    >
                      Clear
                    </button>
                  </div>
                  <h2 className="mt-2 text-sm font-medium leading-5 text-ink">
                    {selected.title || `Passage ${(selected.ordinal ?? 0) + 1}`}
                  </h2>
                  {selected.document && (
                    <p className="mt-1 break-words font-mono text-2xs text-muted">{selected.document}</p>
                  )}
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <Chip>{selected.degree} neighbours</Chip>
                    {selected.categoryName && <Chip>{selected.categoryName}</Chip>}
                    {selected.nodeType === "summary" && (
                      <Chip tone="border-warn/30 bg-warn/10 text-warn">generated summary</Chip>
                    )}
                  </div>
                  <div className="mt-4 border-t border-edge pt-4">
                    {opening === selected.id ? (
                      <div className="flex items-center gap-2 font-mono text-2xs text-subtle">
                        <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                        Loading passage…
                      </div>
                    ) : opened?.chunk_id === selected.id ? (
                      <p className="max-h-72 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-muted">
                        {opened.text}
                      </p>
                    ) : (
                      <p className="text-xs text-danger">The passage could not be loaded.</p>
                    )}
                  </div>
                </>
              ) : (
                <>
                  <Label>passage details</Label>
                  <p className="mt-2 text-xs leading-5 text-subtle">
                    Select a node to inspect it. Click the same node, the canvas background, or press Escape to clear it.
                  </p>
                  <div className="mt-4 border-t border-edge pt-3 font-mono text-2xs text-subtle">
                    {visible.nodes.length} visible nodes · {visible.edges.length} connections
                  </div>
                </>
              )}
            </Card>
          </div>

          <p className="mt-2 text-2xs leading-5 text-subtle">
            {mode === "graph" ? (
              <>
                Layout spreads connected passages apart for readability; position itself has no
                meaning. Switch to <span className="text-muted">Meaning map</span> to compare
                vector distance.
              </>
            ) : shape.projection.method === "none" ? (
              "There are too few passages for a meaningful two-dimensional projection."
            ) : (
              <>
                Nearby points have nearby embeddings. This projection preserves{" "}
                <span className="text-muted">
                  {(shape.projection.explained_variance * 100).toFixed(1)}%
                </span>{" "}
                of the original variance.
              </>
            )}
            {shape.truncated && " Showing the first 400 passages."}
          </p>

        </>
      )}
    </div>
  );
}
