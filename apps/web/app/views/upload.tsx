"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Collection, cx } from "../data";
import { Button, Card, Chip, Label, Mono } from "../ui/kit";

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
  toast,
  onIngested,
}: {
  collections: Collection[];
  toast: (m: string) => void;
  onIngested: () => Promise<void> | void;
}) {
  const [collectionId, setCollectionId] = useState<string | undefined>(collections[0]?.id);
  const [rows, setRows] = useState<Row[]>([]);
  const [dragging, setDragging] = useState(false);
  const [formats, setFormats] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .formats()
      .then((f) => setFormats(f.supported))
      .catch(() => {});
  }, []);

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
      toast("Choose a collection first");
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
          <select
            value={collectionId || ""}
            onChange={(e) => setCollectionId(e.target.value || undefined)}
            className="rounded-lg border border-edge bg-canvas px-2.5 py-1.5 font-mono text-2xs text-ink outline-none focus:border-accent/60"
          >
            {collections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.id}
              </option>
            ))}
          </select>
        </div>
      </div>

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
    </div>
  );
}
