"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { Cluster, cx } from "./data";
import { Chip, Label, Logo, Mono, useToast } from "./ui/kit";
import { Gate } from "./views/gate";
import { Keys } from "./views/keys";
import { IndexGraph } from "./views/index-graph";
import { Overview } from "./views/overview";
import { Playground } from "./views/playground";
import { Query } from "./views/query";
import { Sdk } from "./views/sdk";
import { Summaries } from "./views/summaries";
import { Traces } from "./views/traces";
import { Upload } from "./views/upload";

type Section = "overview" | "upload" | "graph" | "query" | "keys" | "sdk" | "playground" | "traces" | "summaries";

const NAV: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "Overview", icon: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 14h7v7H3z", group: "Workspace" },
  { id: "upload", label: "Upload data", icon: "M12 15V4m0 0L8 8m4-4l4 4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2", group: "Data" },
  { id: "graph", label: "Index graph", icon: "M6 5h.01M18 7h.01M8 18h.01M6 5l12 2M6 5l2 13M18 7L8 18", group: "Data" },
  { id: "query", label: "Query & chat", icon: "M21 12a9 9 0 01-9 9 9 9 0 01-4-1l-4 1 1-4a9 9 0 1116-5z", group: "Data" },
  { id: "traces", label: "Traces", icon: "M3 12h4l3 8 4-16 3 8h4", group: "Data" },
  { id: "summaries", label: "Summaries", icon: "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 012-2h2a2 2 0 012 2M9 5h6M9 14h6m-6-4h6", group: "Data" },
  { id: "sdk", label: "SDK & docs", icon: "M8 9l-4 3 4 3m8-6l4 3-4 3M13 5l-2 14", group: "Developer" },
  { id: "keys", label: "API keys", icon: "M15 7a4 4 0 11-3.8 5.3L7 16.5 5 15l1.5-2L4 11l2-2 3.2 3.2A4 4 0 0115 7z", group: "Developer" },
  { id: "playground", label: "Playground", icon: "M8 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-3M7 8l4 4-4 4M13 16h5", group: "Developer" },
];

const GROUPS = ["Workspace", "Data", "Developer"];
const DEFAULT_COLLECTION = "default";

export default function Page() {
  const [me, setMe] = useState<api.Me | null | undefined>(undefined);
  const [clusters, setClusters] = useState<Cluster[] | null>(null);
  const [section, setSection] = useState<Section>("overview");
  // undefined = not chosen yet (falls back to the default collection); null =
  // explicitly "All collections"; a string = one collection.
  const [selected, setSelected] = useState<string | null | undefined>(undefined);
  const [collOpen, setCollOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const { toast, node } = useToast();

  // Every workspace starts with one collection so there is always somewhere to
  // put data — but more can be created. If none exists yet, the default one is
  // made on load.
  const load = useCallback(async () => {
    let tree = await api.clusters();
    if (!tree.some((c) => c.collections.length > 0)) {
      await api.createCollection("Default").catch(() => { });
      tree = await api.clusters();
    }
    setClusters(tree);
  }, []);

  const check = useCallback(async () => {
    try {
      const who = await api.whoami();
      setMe(who);
      await load();
    } catch {
      setMe(null);
    }
  }, [load]);

  useEffect(() => {
    check();
  }, [check]);

  // A session can end while the page is open. Without this the console kept
  // rendering a workspace it could no longer read, and every action failed
  // with "Not signed in" beside a sidebar still showing who you were.
  useEffect(() => {
    api.onUnauthorized(() => {
      setMe(null);
      setClusters(null);
      setSelected(undefined);
    });
    return () => api.onUnauthorized(null);
  }, []);

  if (me === undefined) {
    return (
      <main className="flex h-screen items-center justify-center bg-canvas">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
      </main>
    );
  }

  if (me === null) return <Gate onIn={check} />;

  const collections = clusters?.flatMap((c) => c.collections) || [];
  const fallback =
    collections.find((c) => c.id === DEFAULT_COLLECTION)?.id ?? collections[0]?.id ?? null;
  // The active collection every view runs inside. Defaults to the pre-built
  // one; the sidebar switches it, and "All collections" (null) searches across.
  const active = selected === undefined ? fallback : selected;
  const activeLabel = active ?? "All collections";

  async function createCollection() {
    const name = newName.trim();
    if (!name) return;
    try {
      const made = await api.createCollection(name);
      setNewName("");
      setCreating(false);
      setCollOpen(false);
      await load();
      setSelected(made.collection_id);
      toast("Collection created");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <main className="flex h-screen overflow-hidden bg-canvas text-ink">
      <aside className="flex w-56 shrink-0 flex-col border-r border-edge bg-panel">
        <div className="flex h-14 items-center border-b border-edge px-4">
          <Logo />
        </div>

        {/* The collection every data & developer view runs in. One exists by
            default; the list here switches between them and makes more. */}
        <div className="relative border-b border-edge px-3 py-3">
          <Label className="px-1">Collection</Label>
          <button
            onClick={() => setCollOpen((o) => !o)}
            className="mt-1.5 flex w-full items-center gap-2 rounded-lg border border-edge bg-elevated px-2.5 py-2 transition hover:border-edgeStrong"
          >
            <span className={cx("h-2 w-2 shrink-0 rounded-full", active ? "bg-accent" : "bg-subtle")} />
            <Mono className="min-w-0 flex-1 truncate text-left text-xs text-ink">{activeLabel}</Mono>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-subtle">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>

          {collOpen && (
            <>
              <div
                className="fixed inset-0 z-20"
                onClick={() => {
                  setCollOpen(false);
                  setCreating(false);
                }}
              />
              <div className="card-in absolute inset-x-3 top-full z-30 mt-1 max-h-80 overflow-y-auto rounded-xl border border-edgeStrong bg-raised p-1.5 shadow-2xl shadow-black/50">
                {[null, ...collections.map((c) => c.id)].map((id) => (
                  <button
                    key={id ?? "__all__"}
                    onClick={() => {
                      setSelected(id);
                      setCollOpen(false);
                    }}
                    className={cx(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition",
                      id === active ? "bg-accent/10" : "hover:bg-elevated"
                    )}
                  >
                    <span className={cx("h-2 w-2 shrink-0 rounded-full", id ? "bg-accent" : "bg-subtle")} />
                    <Mono className="min-w-0 flex-1 truncate text-xs text-ink">
                      {id || "All collections"}
                    </Mono>
                    {id === active && <span className="h-1.5 w-1.5 rounded-full bg-accent" />}
                  </button>
                ))}

                <div className="my-1 border-t border-edge" />
                {creating ? (
                  <div className="flex items-center gap-1.5 px-1.5 py-1">
                    <input
                      autoFocus
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") createCollection();
                        if (e.key === "Escape") setCreating(false);
                      }}
                      placeholder="Collection name"
                      className="min-w-0 flex-1 rounded-md border border-edge bg-canvas px-2 py-1 font-mono text-2xs text-ink outline-none focus:border-accent/60"
                    />
                    <button
                      onClick={createCollection}
                      disabled={!newName.trim()}
                      className="rounded-md border border-accent bg-accent px-2 py-1 font-mono text-2xs font-semibold text-canvas transition hover:bg-accentSoft disabled:opacity-40"
                    >
                      add
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setCreating(true)}
                    className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-accentSoft transition hover:bg-elevated"
                  >
                    <span className="font-mono text-sm leading-none">＋</span>
                    <span className="font-mono text-xs">New collection</span>
                  </button>
                )}
              </div>
            </>
          )}
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
          {GROUPS.map((g) => (
            <div key={g}>
              <Label className="px-2">{g}</Label>
              <div className="mt-1.5 space-y-0.5">
                {NAV.filter((n) => n.group === g).map((n) => (
                  <button
                    key={n.id}
                    onClick={() => setSection(n.id)}
                    className={cx(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs font-medium transition",
                      section === n.id
                        ? "bg-accent/10 text-ink"
                        : "text-muted hover:bg-elevated hover:text-ink"
                    )}
                  >
                    <svg
                      width="15"
                      height="15"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke={section === n.id ? "#7c8cff" : "currentColor"}
                      strokeWidth="1.7"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d={n.icon} />
                    </svg>
                    {n.label}
                    {section === n.id && (
                      <span className="ml-auto h-1.5 w-1.5 rounded-full bg-accent" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-edge p-3">
          <div className="flex items-center gap-2 rounded-lg bg-elevated px-2.5 py-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent/20 font-mono text-2xs uppercase text-accentSoft">
              {(me.identified_as || "?").slice(0, 2)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-2xs text-ink">{me.identified_as}</div>
              <div className="truncate font-mono text-2xs text-subtle">{me.workspace_id}</div>
            </div>
            <button
              title="Sign out"
              onClick={async () => {
                await api.signOut().catch(() => { });
                setMe(null);
                setClusters(null);
              }}
              className="text-subtle transition hover:text-ink"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
              </svg>
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="relative flex h-14 shrink-0 items-center gap-3 border-b border-edge px-5">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-success" />
            <Mono className="text-xs font-medium text-ink">{me.workspace_id}</Mono>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <Chip tone="text-muted border-edgeStrong bg-elevated">
              <span className="mr-1 h-1.5 w-1.5 rounded-full bg-heat-2" />
              {api.API.replace(/^https?:\/\//, "")}
            </Chip>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {clusters === null ? (
            <div className="px-6 py-10 font-mono text-xs text-subtle">Loading workspace…</div>
          ) : !fallback ? (
            <div className="flex h-full items-center justify-center gap-2 font-mono text-xs text-subtle">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
              Preparing your workspace…
            </div>
          ) : (
            <>
              {section === "overview" && (
                <Overview collections={collections} active={active} go={setSection} />
              )}
              {section === "upload" && (
                <Upload
                  collections={collections}
                  active={active}
                  toast={toast}
                  onIngested={load}
                />
              )}
              {section === "graph" && (
                <IndexGraph active={active} onUpload={() => setSection("upload")} />
              )}
              {section === "query" && <Query active={active} />}
              {section === "keys" && (
                <Keys collections={collections} active={active} toast={toast} />
              )}
              {section === "sdk" && <Sdk collections={collections} active={active} />}
              {section === "playground" && <Playground active={active} />}
              {section === "traces" && <Traces active={active} />}
              {section === "summaries" && <Summaries active={active} toast={toast} />}
            </>
          )}
        </div>
      </div>
      {node}
    </main>
  );
}
