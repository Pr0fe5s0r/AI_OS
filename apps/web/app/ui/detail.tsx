"use client";

import { useEffect, useState } from "react";
import {
  Item,
  TaxonomyClass,
  api,
  confidenceTone,
  cx,
  period,
  sourceTone,
  when,
} from "../lib";
import { Button, Chip, Markdown, Spinner } from "./kit";

/** One item, in full: what it says, where it came from, how it is filed,
 *  and every version it has had. The filing is editable here — that edit is
 *  the human override, and it is sticky. */
export function Detail({
  itemId,
  taxonomy,
  onClose,
  onChanged,
}: {
  itemId: string;
  taxonomy: TaxonomyClass[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [item, setItem] = useState<Item | null>(null);
  const [versions, setVersions] = useState<Item[]>([]);
  const [showing, setShowing] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<"content" | "versions">("content");

  useEffect(() => {
    let live = true;
    setItem(null);
    setTab("content");
    setShowing(null);
    (async () => {
      const [detail, history] = await Promise.all([
        api<Item>(`/api/items/${itemId}`),
        api<{ versions: Item[] }>(`/api/items/${itemId}/versions`).catch(() => ({ versions: [] })),
      ]);
      if (!live) return;
      setItem(detail);
      setVersions(history.versions);
      setPicked((detail.classes || []).map((c) => c.class_id));
    })();
    return () => {
      live = false;
    };
  }, [itemId]);

  async function save() {
    setSaving(true);
    try {
      await api(`/api/items/${itemId}/classes`, {
        method: "PUT",
        body: JSON.stringify({ class_ids: picked }),
      });
      const fresh = await api<Item>(`/api/items/${itemId}`);
      setItem(fresh);
      setEditing(false);
      onChanged();
    } finally {
      setSaving(false);
    }
  }

  const shown = showing != null ? versions.find((v) => v.version === showing) || item : item;

  return (
    // An overlay, not a column. Splitting the layout squeezed the list it was
    // opened from into an unreadable strip on anything short of a wide screen.
    <aside className="fixed inset-y-0 right-0 z-30 flex w-[min(44rem,92vw)] animate-slide flex-col border-l border-edge bg-panel shadow-2xl shadow-black/60">
      <header className="flex items-start gap-3 border-b border-edge px-6 py-4">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-base font-semibold text-ink">{item?.title || "…"}</h2>
          {item && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <Chip tone={sourceTone(item.source.source)}>{item.source.source}</Chip>
              <span className="font-mono text-2xs text-subtle">{item.source.locator}</span>
              {item.status !== "active" && (
                <Chip tone="text-warn border-warn/30 bg-warn/10">{item.status}</Chip>
              )}
            </div>
          )}
        </div>
        <button
          onClick={onClose}
          className="rounded-lg border border-edge px-2 py-1 text-xs text-muted transition hover:bg-elevated hover:text-ink"
        >
          Close
        </button>
      </header>

      {!item ? (
        <div className="px-6">
          <Spinner />
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* provenance — what a citation would be rendered from */}
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-b border-edge px-6 py-4 text-xs sm:grid-cols-4">
            <Meta label="Version" value={`v${item.version}`} />
            <Meta label="Added" value={when(item.created_at)} />
            <Meta label="Period" value={period(item) || "—"} />
            <Meta label="Fingerprint" value={item.hash.slice(0, 12)} mono />
          </dl>

          {/* filing — the override lives here */}
          <section className="border-b border-edge px-6 py-4">
            <div className="mb-2.5 flex items-center justify-between">
              <h3 className="text-2xs uppercase tracking-wide text-subtle">Filed under</h3>
              {editing ? (
                <div className="flex gap-2">
                  <Button
                    onClick={() => {
                      setPicked((item.classes || []).map((c) => c.class_id));
                      setEditing(false);
                    }}
                  >
                    Cancel
                  </Button>
                  <Button variant="primary" onClick={save} disabled={saving}>
                    {saving ? "Saving…" : "Save filing"}
                  </Button>
                </div>
              ) : (
                <Button onClick={() => setEditing(true)}>Change</Button>
              )}
            </div>

            {editing ? (
              <>
                <div className="flex flex-wrap gap-1.5">
                  {taxonomy.map((c) => {
                    const on = picked.includes(c.class_id);
                    return (
                      <Chip
                        key={c.class_id}
                        active={on}
                        tone={
                          on
                            ? "text-accentSoft border-accent/50 bg-accent/15"
                            : "text-muted border-edgeStrong bg-elevated"
                        }
                        onClick={() =>
                          setPicked((p) =>
                            on ? p.filter((x) => x !== c.class_id) : [...p, c.class_id]
                          )
                        }
                      >
                        {c.name}
                      </Chip>
                    );
                  })}
                </div>
                <p className="mt-2.5 text-2xs leading-relaxed text-subtle">
                  Your choice is kept. Re-syncing or re-ingesting this document will not
                  change it back.
                </p>
              </>
            ) : (
              <div className="space-y-2">
                {(item.classes || []).length === 0 && (
                  <p className="text-xs text-subtle">Not filed yet.</p>
                )}
                {(item.classes || []).map((c) => (
                  <div key={c.class_id} className="flex items-start gap-2.5">
                    <Chip tone={confidenceTone(c.confidence, c.pinned)}>
                      {c.pinned && <span aria-hidden>📌</span>}
                      {c.name}
                    </Chip>
                    <p className="flex-1 pt-0.5 text-2xs leading-relaxed text-subtle">
                      {c.pinned
                        ? `Set by ${c.actor || "a person"}`
                        : c.basis || "Filed automatically"}
                      {!c.pinned && ` · ${Math.round(c.confidence * 100)}% confident`}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </section>

          <nav className="flex gap-1 border-b border-edge px-6">
            {(["content", "versions"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={cx(
                  "border-b-2 px-3 py-2.5 text-xs font-medium capitalize transition",
                  tab === t
                    ? "border-accent text-ink"
                    : "border-transparent text-subtle hover:text-muted"
                )}
              >
                {t}
                {t === "versions" && versions.length > 1 && (
                  <span className="ml-1.5 text-subtle">{versions.length}</span>
                )}
              </button>
            ))}
          </nav>

          {tab === "content" ? (
            <div className="px-6 py-5">
              {showing != null && (
                <div className="mb-4 flex items-center justify-between rounded-lg border border-warn/30 bg-warn/10 px-3 py-2">
                  <span className="text-2xs text-warn">
                    Showing v{showing} — an older version, kept for the record.
                  </span>
                  <button
                    onClick={() => setShowing(null)}
                    className="text-2xs text-warn underline"
                  >
                    Back to current
                  </button>
                </div>
              )}
              <Markdown body={shown?.body || ""} />
            </div>
          ) : (
            <div className="px-6 py-5">
              <ol className="space-y-2">
                {versions.map((v) => (
                  <li key={v.version}>
                    <button
                      onClick={() => {
                        setShowing(v.status === "active" ? null : v.version);
                        setTab("content");
                      }}
                      className="flex w-full items-center gap-3 rounded-lg border border-edge bg-elevated px-3 py-2.5 text-left transition hover:border-edgeStrong"
                    >
                      <span className="font-mono text-xs text-muted">v{v.version}</span>
                      <span
                        className={cx(
                          "rounded-full border px-2 py-0.5 text-2xs",
                          v.status === "active"
                            ? "border-success/30 bg-success/10 text-success"
                            : "border-edgeStrong bg-canvas text-subtle"
                        )}
                      >
                        {v.status === "active" ? "current" : v.status}
                      </span>
                      <span className="flex-1 truncate text-xs text-subtle">
                        {v.hash.slice(0, 10)}
                      </span>
                      <span className="text-2xs text-subtle">{when(v.created_at)}</span>
                    </button>
                  </li>
                ))}
              </ol>
              <p className="mt-3 text-2xs leading-relaxed text-subtle">
                Only the current version is used to answer questions. Earlier versions are
                kept so you can see what changed and when.
              </p>
            </div>
          )}
        </div>
      )}

      {item?.source.url && (
        <footer className="border-t border-edge px-6 py-3">
          <a
            href={item.source.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-accentSoft hover:underline"
          >
            Open the original ↗
          </a>
          <p className="mt-1 text-2xs text-subtle">
            The knowledge base keeps the text, not a copy of the file.
          </p>
        </footer>
      )}
    </aside>
  );
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wide text-subtle">{label}</dt>
      <dd className={cx("mt-0.5 text-ink", mono ? "font-mono text-2xs" : "text-xs")}>{value}</dd>
    </div>
  );
}
