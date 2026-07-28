"use client";

import { useRef, useState } from "react";
import { Collection, cx, num } from "../data";
import { Button, Card, Chip, Code, Label, Mono } from "../ui/kit";

type Stage = "idle" | "embedding" | "upserting" | "done";

export function Upload({
  collections,
  toast,
}: {
  collections: Collection[];
  toast: (m: string) => void;
}) {
  const [target, setTarget] = useState(collections[0]?.id || "");
  const [file, setFile] = useState<{ name: string; rows: number } | null>(null);
  const [drag, setDrag] = useState(false);
  const [stage, setStage] = useState<Stage>("idle");
  const [pct, setPct] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const col = collections.find((c) => c.id === target);

  function accept(name: string) {
    const rows = 200 + Math.floor(Math.random() * 9800);
    setFile({ name, rows });
    setStage("idle");
    setPct(0);
  }

  function run() {
    if (!file || !col) return;
    setStage("embedding");
    setPct(0);
    const step = () =>
      setPct((p) => {
        const next = p + 4 + Math.random() * 8;
        if (next >= 55 && stageRef.current === "embedding") setStageSafe("upserting");
        if (next >= 100) {
          setStageSafe("done");
          toast(`Upserted ${num(file.rows)} vectors into ${col.name}`);
          return 100;
        }
        timer.current = setTimeout(step, 120);
        return next;
      });
    timer.current = setTimeout(step, 200);
  }

  // Keep the async stage transitions honest without stale closures.
  const stageRef = useRef<Stage>("idle");
  stageRef.current = stage;
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const setStageSafe = (s: Stage) => {
    stageRef.current = s;
    setStage(s);
  };

  const busy = stage === "embedding" || stage === "upserting";

  const snippet = `from markvector import Client

client = Client(api_key="mvk_live_…")

# each record: an id, a ${col?.dims ?? 1536}-dim vector, and a payload you can filter on
client.upsert(
    collection="${col?.name ?? "support_docs"}",
    points=[
        {
            "id": "doc_8823",
            "vector": embed(text),          # your embedding model
            "payload": {"title": title, "url": url},
        },
        # …${file ? num(file.rows) : "N"} more
    ],
)`;

  return (
    <div className="mx-auto max-w-4xl px-6 py-7">
      <h1 className="text-sm font-semibold text-ink">Upload data</h1>
      <p className="mt-1 max-w-xl text-xs leading-relaxed text-muted">
        Drop a JSONL or CSV of records and MarkVector embeds and upserts them into the
        collection. Already have vectors? Upload them directly — the embedding step is skipped.
      </p>

      <div className="mt-5 grid gap-2">
        <Label>Destination collection</Label>
        <div className="flex flex-wrap gap-2">
          {collections.map((c) => (
            <Chip
              key={c.id}
              active={c.id === target}
              onClick={() => setTarget(c.id)}
              tone={
                c.id === target
                  ? "text-ink border-accent/50 bg-accent/10"
                  : "text-muted border-edgeStrong bg-elevated"
              }
            >
              {c.name} · {c.dims}d
            </Chip>
          ))}
        </div>
      </div>

      {/* Drop zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          const f = e.dataTransfer.files?.[0];
          accept(f?.name || "records.jsonl");
        }}
        onClick={() => inputRef.current?.click()}
        className={cx(
          "mt-5 flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-12 text-center transition field-grid",
          drag ? "border-accent bg-accent/5" : "border-edge hover:border-edgeStrong"
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".jsonl,.json,.csv"
          className="hidden"
          onChange={(e) => accept(e.target.files?.[0]?.name || "records.jsonl")}
        />
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#7c8cff" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 15V4m0 0L8 8m4-4l4 4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2" />
        </svg>
        <p className="mt-3 text-sm text-ink">
          {file ? file.name : "Drop a file or click to browse"}
        </p>
        <p className="mt-1 font-mono text-2xs text-subtle">
          {file ? `${num(file.rows)} records detected` : "JSONL · JSON · CSV — up to 100MB"}
        </p>
      </div>

      {/* Run / progress */}
      {file && (
        <Card className="mt-4 p-4">
          {stage === "done" ? (
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-heat-0/15">
                  <span className="h-2 w-2 rounded-full bg-heat-0" />
                </span>
                <div>
                  <Mono className="text-xs text-ink">
                    {num(file.rows)} vectors upserted into {col?.name}
                  </Mono>
                  <Label className="block">indexing continues in the background</Label>
                </div>
              </div>
              <Button size="sm" onClick={() => setFile(null)}>
                Upload more
              </Button>
            </div>
          ) : busy ? (
            <div>
              <div className="flex items-center justify-between">
                <Mono className="text-xs text-muted">
                  {stage === "embedding" ? "Embedding records…" : "Upserting vectors…"}
                </Mono>
                <Mono className="text-xs tabular-nums text-ink">{Math.min(100, Math.round(pct))}%</Mono>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-elevated">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-heat-3 to-heat-0 transition-all"
                  style={{ width: `${Math.min(100, pct)}%` }}
                />
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-between">
              <Mono className="text-xs text-muted">
                Ready to embed with <span className="text-heat-2">text-embedding-3-small</span> →{" "}
                {col?.dims}d
              </Mono>
              <Button variant="primary" size="sm" onClick={run}>
                Embed & upsert
              </Button>
            </div>
          )}
        </Card>
      )}

      <div className="mt-6">
        <Label className="mb-2 block">…or upsert programmatically</Label>
        <Code code={snippet} filename="upsert.py" />
      </div>
    </div>
  );
}
