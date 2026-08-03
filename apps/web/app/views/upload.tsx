"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Collection, Document, ago, bytes, cx } from "../data";
import { Button, Card, Chip, Empty, Label, Mono } from "../ui/kit";

type Row = {
  name: string;
  state: "sending" | "indexing" | "done" | "failed";
  detail?: string;
};

/** Putting data in.
 *
 *  Writing and indexing are separate jobs, so a file is not searchable the
 *  instant the upload returns. The row stays visible and keeps polling until
 *  the document genuinely exists — guessing at a delay is what made uploading
 *  look broken before. */
export function Upload({
  collections,
  active,
  toast,
  onIngested,
}: {
  collections: Collection[];
  active: string | null;
  toast: (m: string) => void;
  onIngested: () => Promise<void> | void;
}) {
  // Upload writes into exactly one collection, so "All collections" is not a
  // valid target — the sidebar has to name one first.
  const collectionId = active ?? undefined;
  const [rows, setRows] = useState<Row[]>([]);
  const [dragging, setDragging] = useState(false);
  const [formats, setFormats] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  // What is already in this collection, and the document a person opened to
  // read — its text and the passages it was split into.
  const [library, setLibrary] = useState<Document[] | null>(null);
  const [openDoc, setOpenDoc] = useState<Document | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .formats()
      .then((f) => setFormats(f.supported))
      .catch(() => {});
  }, []);

  // The library is per collection, so it reloads whenever the active collection
  // changes and again after each ingest settles.
  const loadLibrary = useCallback(async () => {
    if (!collectionId) {
      setLibrary(null);
      return;
    }
    setLibrary(await api.documents(collectionId, 200).catch(() => []));
  }, [collectionId]);

  useEffect(() => {
    setOpenDoc(null);
    loadLibrary();
  }, [loadLibrary]);

  function mark(name: string, state: Row["state"], detail?: string) {
    setRows((r) => r.map((x) => (x.name === name ? { ...x, state, detail } : x)));
  }

  /** Wait for a locator to actually appear in the collection. */
  async function settle(locator: string, name: string) {
    for (let attempt = 0; attempt < 16; attempt++) {
      await new Promise((r) => setTimeout(r, 1200));
      const docs = await api.documents(collectionId, 200).catch(() => []);
      const found = docs.find((d) => d.locator === locator);

      if (!found) {
        // Not there yet — but it may never be. Parsing happens in a worker, so
        // the reason a file was unreadable arrives after the upload returned;
        // without this the row just span until it timed out saying nothing.
        const failed = (await api.failures(collectionId).catch(() => [])).find(
          (f) => f.locator === locator
        );
        if (failed) {
          mark(name, "failed", failed.reason);
          return false;
        }
      }

      if (found) {
        // Filing runs after the write, so wait a beat longer for categories
        // rather than showing every new document as uncategorised forever.
        if (found.categories.length || attempt > 6) {
          mark(name, "done", `v${found.version} · ${found.categories.length} categories`);
          return true;
        }
      }
    }
    mark(name, "failed", "still indexing after 20s — it may yet appear in the collection");
    return false;
  }

  async function send(files: FileList | null) {
    if (!files?.length) return;
    if (!collectionId) {
      toast("Pick a collection in the sidebar first");
      return;
    }
    const chosen = Array.from(files);
    setRows((r) => [...chosen.map((f) => ({ name: f.name, state: "sending" as const })), ...r]);

    for (const file of chosen) {
      try {
        await api.addFile(collectionId, file);
        mark(file.name, "indexing");
        await settle(file.name, file.name);
      } catch (e) {
        mark(file.name, "failed", (e as Error).message);
      }
    }
    await onIngested();
    await loadLibrary();
    if (fileRef.current) fileRef.current.value = "";
  }

  async function sendText() {
    if (!text.trim() || !collectionId) return;
    const locator = `note/${Date.now()}`;
    const name = title.trim() || locator;
    setRows((r) => [{ name, state: "sending" }, ...r]);
    try {
      await api.addText(collectionId, {
        source: "console",
        locator,
        body: text,
        title: title.trim() || undefined,
      });
      setText("");
      setTitle("");
      mark(name, "indexing");
      await settle(locator, name);
      await onIngested();
      await loadLibrary();
    } catch (e) {
      mark(name, "failed", (e as Error).message);
    }
  }

  return (
    <div
      className="px-6 py-6"
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
        send(e.dataTransfer.files);
      }}
    >
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Upload data</h1>
          <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
            Files are read, converted to Markdown and indexed. The original is never
            stored — the store keeps text and a link back, not a copy of your file.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Label>into</Label>
          <Chip tone={collectionId ? "text-accentSoft border-accent/40 bg-accent/10" : undefined}>
            {collectionId || "no collection selected"}
          </Chip>
        </div>
      </div>

      {!collectionId && (
        <Card className="mb-5 border-heat-1/30 bg-heat-1/5 p-4">
          <p className="text-xs text-ink">Pick a collection to upload into</p>
          <p className="mt-1 text-2xs leading-relaxed text-subtle">
            {collections.length === 0
              ? "This cluster has no collections yet — create one first, then choose it in the sidebar."
              : "Use the collection selector in the sidebar. Uploading needs one target collection, so “All collections” can’t receive files."}
          </p>
        </Card>
      )}

      <input
        ref={fileRef}
        type="file"
        multiple
        // The picker greys out what the store cannot read, so an unreadable
        // file is refused before it is chosen rather than after it is sent.
        accept={formats.join(",")}
        className="sr-only"
        onChange={(e) => send(e.target.files)}
      />
      <button
        onClick={() => fileRef.current?.click()}
        className={cx(
          "mb-5 flex w-full flex-col items-center justify-center rounded-xl border border-dashed py-12 transition",
          dragging
            ? "border-accent/60 bg-accent/5"
            : "border-edge hover:border-edgeStrong hover:bg-elevated/40"
        )}
      >
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#7c8cff" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 15V4m0 0L8 8m4-4l4 4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2" />
        </svg>
        <span className="mt-2.5 text-xs text-ink">Drop files here, or click to choose</span>
        <span className="mt-1 font-mono text-2xs text-subtle">
          {formats.length ? formats.join("  ") : "loading formats…"}
        </span>
      </button>

      <Card className="mb-5 p-4">
        <Label className="mb-2 block">or paste text</Label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Title (optional)"
          className="mb-2 w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-xs text-ink outline-none focus:border-accent/60"
        />
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
          placeholder="Paste a note, a report, a transcript…"
          className="w-full resize-y rounded-lg border border-edge bg-canvas px-3 py-2 text-xs leading-relaxed text-ink outline-none focus:border-accent/60"
        />
        <div className="mt-2 flex justify-end">
          <Button variant="primary" onClick={sendText} disabled={!text.trim() || !collectionId}>
            Add to {collectionId || "…"}
          </Button>
        </div>
      </Card>

      {rows.length > 0 && (
        <>
          <Label className="mb-2 block">this session</Label>
          <Card className="divide-y divide-edge/60">
            {rows.map((r) => (
              <div key={r.name} className="flex items-center gap-3 px-4 py-2.5">
                {r.state === "done" ? (
                  <span className="text-heat-0">✓</span>
                ) : r.state === "failed" ? (
                  <span className="text-danger">✕</span>
                ) : (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                )}
                <Mono className="min-w-0 flex-1 truncate text-xs text-ink">{r.name}</Mono>
                <span className="font-mono text-2xs text-subtle">
                  {r.detail ||
                    { sending: "sending…", indexing: "indexing…", done: "", failed: "" }[r.state]}
                </span>
                <Chip
                  tone={
                    r.state === "done"
                      ? "text-heat-0 border-heat-0/30 bg-heat-0/10"
                      : r.state === "failed"
                        ? "text-danger border-danger/30 bg-danger/10"
                        : "text-muted border-edgeStrong bg-elevated"
                  }
                >
                  {r.state}
                </Chip>
              </div>
            ))}
          </Card>
        </>
      )}

      {collectionId && (
        <div className="mt-6">
          <div className="mb-2 flex items-center justify-between">
            <Label>
              in this collection
              {library && library.length > 0 && (
                <span className="ml-2 normal-case tracking-normal text-subtle">
                  {library.length} document{library.length === 1 ? "" : "s"}
                </span>
              )}
            </Label>
            <button
              onClick={loadLibrary}
              className="font-mono text-2xs text-subtle transition hover:text-ink"
            >
              refresh
            </button>
          </div>

          {library === null ? (
            <div className="font-mono text-xs text-subtle">Loading…</div>
          ) : library.length === 0 ? (
            <Empty
              title="Nothing uploaded yet"
              hint="Drop a file or paste text above. Once it is indexed it shows here, and you can open it to read the text and the passages it was split into."
            />
          ) : (
            <Card className="divide-y divide-edge/60">
              {library.map((d) => (
                <button
                  key={d.id}
                  onClick={() => setOpenDoc(d)}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition hover:bg-elevated"
                >
                  <div className="min-w-0 flex-1">
                    {/* The file name first — it is what a person recognises. The
                        extracted title is secondary. */}
                    <div className="truncate font-mono text-xs text-ink">
                      {d.original?.filename || d.locator}
                    </div>
                    <div className="truncate text-2xs text-subtle">{d.title}</div>
                  </div>
                  <Chip>{d.source}</Chip>
                  {d.original && (
                    <Chip
                      title={`original kept: ${d.original.filename}`}
                      tone="text-heat-2 border-heat-2/30 bg-heat-2/10"
                    >
                      {(d.original.filename.split(".").pop() || "file").toLowerCase()}
                    </Chip>
                  )}
                  {d.categories.slice(0, 2).map((c) => (
                    <Chip key={c.class_id}>{c.name}</Chip>
                  ))}
                  <Mono className="hidden text-2xs text-subtle sm:block">v{d.version}</Mono>
                  <Mono className="hidden whitespace-nowrap text-2xs text-subtle md:block">
                    {ago(d.createdAt)}
                  </Mono>
                </button>
              ))}
            </Card>
          )}
        </div>
      )}

      {openDoc && (
        <DocPanel doc={openDoc} collectionId={collectionId} onClose={() => setOpenDoc(null)} />
      )}
    </div>
  );
}

/** One document, in full — its text and the passages it became.
 *
 *  A document is what you uploaded; a chunk is what the store retrieves. Showing
 *  both here is the answer to "what actually got indexed": the file on top, and
 *  underneath it the N passages it was split into, in document order. Text is
 *  rendered as text, never as markup, so a document cannot make the console
 *  render whatever it happens to contain. */
function DocPanel({
  doc,
  collectionId,
  onClose,
}: {
  doc: Document;
  collectionId: string | undefined;
  onClose: () => void;
}) {
  const [chunks, setChunks] = useState<api.DocChunk[] | null>(null);
  // The original file opens first when there is one; otherwise the panel lands
  // on the converted text.
  const [tab, setTab] = useState<"original" | "text" | "chunks">(
    doc.original ? "original" : "text"
  );
  const [orig, setOrig] = useState<{ url: string; contentType: string } | null>(null);
  const [origError, setOrigError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setChunks(null);
    api
      .documentChunks(doc.id, collectionId)
      .then((c) => live && setChunks(c))
      .catch(() => live && setChunks([]));
    return () => {
      live = false;
    };
  }, [doc.id, collectionId]);

  // Load the original lazily and once, and always hand the object URL back to
  // the browser when the panel closes so it is not leaked.
  useEffect(() => {
    if (!doc.original) return;
    let url: string | null = null;
    setOrig(null);
    setOrigError(null);
    api
      .originalBlobUrl(doc.id, collectionId)
      .then((o) => {
        url = o.url;
        setOrig(o);
      })
      .catch((e) => setOrigError((e as Error).message));
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [doc.id, collectionId, doc.original]);

  return (
    <>
      <div onClick={onClose} className="fixed inset-0 z-20 bg-black/50 backdrop-blur-[1px]" />
      <aside className="fixed inset-y-0 right-0 z-30 flex w-[min(46rem,94vw)] flex-col border-l border-edge bg-panel shadow-2xl shadow-black/60 animate-slide">
        <header className="border-b border-edge px-5 py-4">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <Mono className="block truncate text-sm font-medium text-ink">
                {doc.original?.filename || doc.locator}
              </Mono>
              <div className="mt-1 truncate text-2xs text-subtle">{doc.title}</div>
            </div>
            <button
              onClick={onClose}
              className="shrink-0 rounded-lg border border-edge px-2 py-1 font-mono text-2xs text-muted transition hover:text-ink"
            >
              close
            </button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Chip>{doc.source}</Chip>
            <Chip>v{doc.version}</Chip>
            {doc.status !== "active" && (
              <Chip tone="text-hot border-hot/40 bg-hot/10">{doc.status}</Chip>
            )}
            {doc.categories.map((c) => (
              <Chip
                key={c.class_id}
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
            <Mono className="text-2xs text-subtle">
              {bytes(doc.body.length)} · added {ago(doc.createdAt)}
            </Mono>
          </div>

          <div className="mt-3 flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
            {((doc.original
              ? ["original", "text", "chunks"]
              : ["text", "chunks"]) as ("original" | "text" | "chunks")[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={cx(
                  "rounded-md px-3 py-1 font-mono text-2xs transition",
                  tab === t ? "bg-accent/15 text-ink" : "text-subtle hover:text-muted"
                )}
              >
                {t === "original"
                  ? "original file"
                  : t === "text"
                    ? "document text"
                    : `chunks${chunks ? ` (${chunks.length})` : ""}`}
              </button>
            ))}
          </div>
        </header>

        <div className={cx("min-h-0 flex-1", tab === "original" ? "flex" : "overflow-y-auto px-5 py-5")}>
          {tab === "original" ? (
            <OriginalView
              original={doc.original!}
              blob={orig}
              error={origError}
            />
          ) : tab === "text" ? (
            doc.body.trim() ? (
              <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-muted">
                {doc.body}
              </pre>
            ) : (
              <p className="font-mono text-2xs text-subtle">
                This document indexed no text — nothing was extracted from it.
              </p>
            )
          ) : chunks === null ? (
            <div className="font-mono text-xs text-subtle">Loading passages…</div>
          ) : chunks.length === 0 ? (
            <p className="font-mono text-2xs text-subtle">
              No passages — this document produced nothing retrievable.
            </p>
          ) : (
            <div className="space-y-2">
              {chunks.map((c) => (
                <Card key={c.chunk_id} className="p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <Chip>#{c.ordinal + 1}</Chip>
                    {c.heading && (
                      <Mono className="min-w-0 flex-1 truncate text-2xs text-accentSoft">
                        {c.heading}
                      </Mono>
                    )}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                    {c.text}
                  </p>
                </Card>
              ))}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

/** The uploaded file, shown as it arrived.
 *
 *  PDFs, images and text render in place; anything a browser cannot display —
 *  a Word or Excel file — offers the file to download instead. The bytes are
 *  loaded through a credentialed fetch into an object URL, so this never turns
 *  into a second, unauthenticated request to the API. */
function OriginalView({
  original,
  blob,
  error,
}: {
  original: NonNullable<Document["original"]>;
  blob: { url: string; contentType: string } | null;
  error: string | null;
}) {
  if (error) {
    return (
      <div className="m-auto px-6 text-center">
        <p className="font-mono text-2xs text-danger">{error}</p>
      </div>
    );
  }
  if (!blob) {
    return (
      <div className="m-auto font-mono text-xs text-subtle">Loading {original.filename}…</div>
    );
  }

  const type = blob.contentType || original.contentType;
  if (type === "application/pdf") {
    return <iframe title={original.filename} src={blob.url} className="h-full w-full border-0" />;
  }
  if (type.startsWith("image/")) {
    return (
      <div className="flex h-full w-full items-center justify-center overflow-auto bg-canvas p-4">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={blob.url} alt={original.filename} className="max-h-full max-w-full" />
      </div>
    );
  }
  if (type.startsWith("text/")) {
    return <iframe title={original.filename} src={blob.url} className="h-full w-full border-0 bg-white" />;
  }

  // Office documents and anything else a browser will not render in place.
  return (
    <div className="m-auto max-w-sm px-6 text-center">
      <p className="text-xs text-ink">This file can’t be previewed in the browser</p>
      <p className="mt-1 font-mono text-2xs text-subtle">
        {original.filename} · {bytes(original.size)}
      </p>
      <a
        href={blob.url}
        download={original.filename}
        className="mt-4 inline-block rounded-lg border border-accent bg-accent px-3.5 py-2 text-xs font-semibold text-canvas transition hover:bg-accentSoft"
      >
        Download original
      </a>
      <p className="mt-3 text-2xs leading-relaxed text-subtle">
        The “document text” tab shows the searchable Markdown the store extracted from it.
      </p>
    </div>
  );
}
