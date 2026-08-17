"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import { Collection, Document, ago, bytes, cx } from "../data";
import { Button, Card, Chip, Empty, Label, Mono } from "../ui/kit";

type Row = {
  name: string;
  // "working" is NOT a failure. Indexing runs in a worker, and reading a
  // scanned document with vision is a model call per page — a minute or two
  // is ordinary. The console used to give up after twenty seconds and stamp
  // the row "failed", with a message underneath admitting the document might
  // yet appear. It usually had. A real failure arrives with a reason, from
  // the worker, and only that is a failure.
  state: "sending" | "indexing" | "done" | "failed" | "working";
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
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [sort, setSort] = useState<"newest" | "oldest" | "title">("newest");
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

  /** Wait for a locator to actually appear in the collection.
   *
   *  Watched for three minutes, quickly at first and then every few seconds:
   *  a Markdown file lands in about a second, and a twenty-page scan read with
   *  vision takes a model call per page. Polling hard for the whole window
   *  would be a request a second for three minutes to learn nothing.
   */
  async function settle(locator: string, name: string) {
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise((r) => setTimeout(r, attempt < 15 ? 1200 : 4000));
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
    // Stopped waiting, not failed. The document is still being indexed and the
    // worker will finish it, so the watch continues in the background rather
    // than leaving a row that will never change and a list that will never
    // refresh — which is what made this look broken in the first place.
    mark(
      name,
      "working",
      "still indexing — reading a document with vision takes a model call per page. This row will update when it lands."
    );
    keepWatching(locator, name);
    return false;
  }

  /** Carry on watching after the foreground wait gives up.
   *
   *  Detached on purpose: the upload has returned, the person is free to go
   *  and do something else, and the row updates itself when the worker
   *  finishes. Ten more minutes at five-second intervals, which comfortably
   *  covers a long scan being read page by page.
   */
  function keepWatching(locator: string, name: string) {
    let attempts = 0;
    const timer = setInterval(async () => {
      attempts += 1;
      const docs = await api.documents(collectionId, 200).catch(() => []);
      const found = docs.find((d) => d.locator === locator);
      if (found) {
        clearInterval(timer);
        mark(name, "done", `v${found.version} · ${found.categories.length} categories`);
        setLibrary(docs);
        return;
      }
      const failed = (await api.failures(collectionId).catch(() => [])).find(
        (f) => f.locator === locator
      );
      if (failed) {
        clearInterval(timer);
        mark(name, "failed", failed.reason);
        return;
      }
      if (attempts >= 120) clearInterval(timer);
    }, 5000);
  }

  async function send(files: FileList | null) {
    if (!files?.length) return;
    if (!collectionId) {
      toast("Pick a collection in the sidebar first");
      return;
    }
    const chosen = Array.from(files);
    // One row per file name, so re-uploading REPLACES the previous attempt
    // instead of stacking beside it. Retrying is the remedy a failure message
    // tells you to try ("try uploading it again"), so it is a path people take
    // deliberately — and it used to leave two identical rows sharing one React
    // key, which React warns about and which made `mark` update both of them.
    setRows((r) => {
      const retried = new Set(chosen.map((f) => f.name));
      return [
        ...chosen.map((f) => ({ name: f.name, state: "sending" as const })),
        ...r.filter((row) => !retried.has(row.name)),
      ];
    });

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

  const visibleDocuments = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const next = (library ?? []).filter((doc) => {
      const matchesSource = sourceFilter === "all" || doc.source === sourceFilter;
      const haystack = [doc.title, doc.locator, doc.source, ...doc.categories.map((c) => c.name)]
        .join(" ")
        .toLowerCase();
      return matchesSource && (!needle || haystack.includes(needle));
    });
    return [...next].sort((a, b) => {
      if (sort === "title") return a.title.localeCompare(b.title);
      const left = a.createdAt ? Date.parse(a.createdAt) : 0;
      const right = b.createdAt ? Date.parse(b.createdAt) : 0;
      return sort === "oldest" ? left - right : right - left;
    });
  }, [library, search, sourceFilter, sort]);

  const sources = useMemo(
    () => [...new Set((library ?? []).map((doc) => doc.source).filter(Boolean))].sort(),
    [library]
  );

  // In-flight (and just-failed) uploads, surfaced in the Documents list so a
  // file appears among your uploaded files the moment it is sent — marked
  // "indexing" — rather than only after the worker has finished. A "done"
  // upload drops out of here because by then it is a real document below.
  const uploading = useMemo(() => rows.filter((r) => r.state !== "done"), [rows]);
  const indexingCount = useMemo(
    () => uploading.filter((r) => r.state !== "failed").length,
    [uploading]
  );

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
        try {
          if (e.dataTransfer?.files?.length) {
            send(e.dataTransfer.files);
          }
        } catch (err) {
          console.warn("File drop error:", err);
        }
      }}
    >
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Upload data</h1>
          <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
            {/* This used to say the original is never stored. It was true when
                it was written and stopped being true the day citations began
                showing the page they were read off — that picture is rendered
                from the file itself. A store whose whole argument is that you
                can check its claims cannot be wrong about where your file
                went. */}
            Files are read, converted to Markdown and indexed. The original file is
            kept too, so a citation can show you the page it came from — and PDF
            pages you ask about can be read as images when their text alone will
            not do.
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

      {collectionId && (
        <div className="mt-6">
          <div className="mb-3 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-ink">Documents</h2>
              <p className="mt-0.5 text-xs text-muted">
                {library?.length ?? 0} document{library?.length === 1 ? "" : "s"} in this collection
                {indexingCount > 0 && (
                  <span className="text-accentSoft"> · {indexingCount} indexing</span>
                )}
              </p>
            </div>
            <button
              onClick={loadLibrary}
              className="font-mono text-2xs text-subtle transition hover:text-ink"
            >
              Refresh
            </button>
          </div>

          <div className="mb-3 grid gap-2 md:grid-cols-[minmax(16rem,1fr)_11rem_11rem]">
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search by title, file, source, or category"
              className="rounded-lg border border-edge bg-canvas px-3 py-2.5 text-xs text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
            />
            <select
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.target.value)}
              className="rounded-lg border border-edge bg-canvas px-3 py-2.5 text-xs text-muted outline-none focus:border-accent/60"
            >
              <option value="all">All sources</option>
              {sources.map((source) => <option key={source} value={source}>{source}</option>)}
            </select>
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as typeof sort)}
              className="rounded-lg border border-edge bg-canvas px-3 py-2.5 text-xs text-muted outline-none focus:border-accent/60"
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="title">Title A–Z</option>
            </select>
          </div>

          {library === null ? (
            <div className="font-mono text-xs text-subtle">Loading…</div>
          ) : library.length === 0 && uploading.length === 0 ? (
            <Empty
              title="Nothing uploaded yet"
              hint="Drop a file or paste text above. It appears here right away while it indexes, and once indexed you can open it to read the text and the passages it was split into."
            />
          ) : (
            <Card className="divide-y divide-edge/60">
              {/* Uploads in flight, shown among the files the instant they are
                  sent — "indexing" until the worker lands them, then they are
                  replaced by the real document below. A failed one stays visible
                  with its reason rather than vanishing. */}
              {uploading.map((r) => (
                <div key={`up-${r.name}`} className="flex items-center gap-3 px-4 py-2.5">
                  {r.state === "failed" ? (
                    <span className="text-danger">✕</span>
                  ) : r.state === "working" ? (
                    <span className="text-warn">◷</span>
                  ) : (
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-xs text-ink">{r.name}</div>
                    <div className="truncate text-2xs text-subtle">
                      {r.detail ||
                        (r.state === "failed"
                          ? "failed"
                          : r.state === "working"
                            ? "still indexing…"
                            : r.state === "sending"
                              ? "sending…"
                              : "indexing…")}
                    </div>
                  </div>
                  <Chip
                    tone={
                      r.state === "failed"
                        ? "text-danger border-danger/30 bg-danger/10"
                        : r.state === "working"
                          ? "text-warn border-warn/30 bg-warn/10"
                          : "text-accentSoft border-accent/30 bg-accent/10"
                    }
                  >
                    {r.state === "failed" ? "failed" : r.state === "working" ? "working" : "indexing"}
                  </Chip>
                </div>
              ))}
              {visibleDocuments.length === 0 && library.length > 0 && (
                <div className="px-4 py-6 text-center text-2xs text-subtle">
                  No documents match that search.
                </div>
              )}
              {visibleDocuments.map((d) => (
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
        <DocPanel
          doc={openDoc}
          collectionId={collectionId}
          onClose={() => setOpenDoc(null)}
          onSaved={(updated) => {
            setOpenDoc(updated);
            setLibrary((current) => current?.map((doc) => doc.id === updated.id ? updated : doc) ?? null);
          }}
          onDeleted={(id) => {
            setOpenDoc(null);
            setLibrary((current) => current?.filter((doc) => doc.id !== id) ?? null);
          }}
        />
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
  onSaved,
  onDeleted,
}: {
  doc: Document;
  collectionId: string | undefined;
  onClose: () => void;
  onSaved: (doc: Document) => void;
  onDeleted: (id: string) => void;
}) {
  const [chunks, setChunks] = useState<api.DocChunk[] | null>(null);
  // The original file opens first when there is one; otherwise the panel lands
  // on the converted text.
  const [tab, setTab] = useState<"original" | "text" | "chunks" | "edit">(
    doc.original ? "original" : "text"
  );
  const [orig, setOrig] = useState<{ url: string; contentType: string } | null>(null);
  const [origError, setOrigError] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState(doc.title);
  const [draftBody, setDraftBody] = useState(doc.body);
  const [draftMetadata, setDraftMetadata] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // What the SERVER says this deletion would destroy. Null while nothing is
  // being asked about; set, a dialog is on screen and nothing has happened yet.
  const [doomed, setDoomed] = useState<api.DeletionPreview | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Two steps, because there is no undo and no trash to restore from. The first
  // call asks what would go and deletes nothing; only the second, made after a
  // person has read the answer, carries it out.
  async function askToDelete() {
    setDeleteError(null);
    try {
      setDoomed(await api.previewDelete(doc.id));
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "Could not check what would be deleted.");
    }
  }

  async function reallyDelete() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteDocument(doc.id);
      setDoomed(null);
      onDeleted(doc.id);
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "The document could not be deleted.");
    } finally {
      setDeleting(false);
    }
  }

  useEffect(() => {
    const editableMetadata = Object.fromEntries(
      Object.entries(doc.metadata).filter(([key]) => key !== "original")
    );
    setDraftTitle(doc.title);
    setDraftBody(doc.body);
    setDraftMetadata(JSON.stringify(editableMetadata, null, 2));
    setSaveError(null);
  }, [doc]);

  async function save() {
    if (!draftTitle.trim()) {
      setSaveError("Title cannot be empty.");
      return;
    }
    let metadata: Record<string, unknown>;
    try {
      const parsed: unknown = draftMetadata.trim() ? JSON.parse(draftMetadata) : {};
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("Metadata must be a JSON object.");
      }
      metadata = parsed as Record<string, unknown>;
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "Metadata is not valid JSON.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await api.updateDocument(doc.id, collectionId, {
        title: draftTitle.trim(),
        body: draftBody,
        metadata,
      });
      onSaved(updated);
      setTab("text");
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "The document could not be saved.");
    } finally {
      setSaving(false);
    }
  }

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
      {/* The alert. It states the NUMBERS the server reported rather than a
          generic "are you sure": a person confirming a destruction they cannot
          undo should be told what they are destroying, and a count they did not
          expect is the one thing that will stop them. */}
      {doomed && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-6 backdrop-blur">
          <Card className="card-in w-full max-w-md p-5">
            <h2 className="text-sm font-semibold text-ink">Delete this document?</h2>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              <span className="font-medium text-ink">{doomed.title}</span> and
              everything indexed from it: {doomed.versions}{" "}
              {doomed.versions === 1 ? "version" : "versions"}, {doomed.passages}{" "}
              {doomed.passages === 1 ? "passage" : "passages"}, its vectors, the
              stored original and every page picture rendered from it.
            </p>
            <p className="mt-2 font-mono text-2xs text-hot">
              This cannot be undone. There is no trash to restore it from.
            </p>
            {deleteError && (
              <p className="mt-2 font-mono text-2xs text-hot">{deleteError}</p>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <Button onClick={() => setDoomed(null)} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={reallyDelete} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete permanently"}
              </Button>
            </div>
          </Card>
        </div>
      )}

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
              onClick={askToDelete}
              title="Delete this document and everything indexed from it"
              className="shrink-0 rounded-lg border border-edge px-2 py-1 font-mono text-2xs text-muted transition hover:border-hot/50 hover:text-hot"
            >
              delete
            </button>
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
              ? ["original", "text", "chunks", "edit"]
              : ["text", "chunks", "edit"]) as ("original" | "text" | "chunks" | "edit")[]).map((t) => (
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
                    : t === "chunks"
                      ? `chunks${chunks ? ` (${chunks.length})` : ""}`
                      : "edit info"}
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
          ) : tab === "edit" ? (
            <div className="mx-auto max-w-2xl space-y-4">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-ink">Title</label>
                <input
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.target.value)}
                  className="w-full rounded-lg border border-edge bg-canvas px-3 py-2.5 text-sm text-ink outline-none focus:border-accent/60"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-ink">Document text</label>
                <textarea
                  value={draftBody}
                  onChange={(event) => setDraftBody(event.target.value)}
                  rows={14}
                  className="w-full resize-y rounded-lg border border-edge bg-canvas px-3 py-2.5 font-mono text-xs leading-relaxed text-ink outline-none focus:border-accent/60"
                />
                <p className="mt-1 text-2xs leading-relaxed text-subtle">
                  Changing searchable text creates a new document version and rebuilds its passages.
                </p>
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-ink">Custom metadata</label>
                <textarea
                  value={draftMetadata}
                  onChange={(event) => setDraftMetadata(event.target.value)}
                  rows={7}
                  spellCheck={false}
                  className="w-full resize-y rounded-lg border border-edge bg-canvas px-3 py-2.5 font-mono text-xs leading-relaxed text-ink outline-none focus:border-accent/60"
                />
                <p className="mt-1 text-2xs text-subtle">JSON object · stored with the document</p>
              </div>
              {saveError && (
                <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                  {saveError}
                </p>
              )}
              <div className="flex justify-end gap-2 border-t border-edge pt-4">
                <Button onClick={() => setTab(doc.original ? "original" : "text")}>Cancel</Button>
                <Button variant="primary" onClick={save} disabled={saving || !draftTitle.trim()}>
                  {saving ? "Saving…" : "Save changes"}
                </Button>
              </div>
            </div>
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
