"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { Cluster, cx } from "./data";
import { Chip, Label, Logo, Mono, useToast } from "./ui/kit";
import { Collections } from "./views/collections";
import { Gate } from "./views/gate";
import { Keys } from "./views/keys";
import { Overview } from "./views/overview";
import { Query } from "./views/query";
import { Sdk } from "./views/sdk";
import { Traces } from "./views/traces";
import { Upload } from "./views/upload";

type Section = "overview" | "collections" | "upload" | "query" | "keys" | "sdk" | "traces";

const NAV: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "Overview", icon: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 14h7v7H3z", group: "Cluster" },
  { id: "collections", label: "Collections", icon: "M4 5c0-1.1 3.6-2 8-2s8 .9 8 2-3.6 2-8 2-8-.9-8-2zM4 5v14c0 1.1 3.6 2 8 2s8-.9 8-2V5", group: "Cluster" },
  { id: "upload", label: "Upload data", icon: "M12 15V4m0 0L8 8m4-4l4 4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2", group: "Data" },
  { id: "query", label: "Query & chat", icon: "M21 12a9 9 0 01-9 9 9 9 0 01-4-1l-4 1 1-4a9 9 0 1116-5z", group: "Data" },
  { id: "traces", label: "Traces", icon: "M3 12h4l3 8 4-16 3 8h4", group: "Data" },
  { id: "keys", label: "API keys", icon: "M15 7a4 4 0 11-3.8 5.3L7 16.5 5 15l1.5-2L4 11l2-2 3.2 3.2A4 4 0 0115 7z", group: "Develop" },
  { id: "sdk", label: "SDK & docs", icon: "M8 9l-4 3 4 3m8-6l4 3-4 3M13 5l-2 14", group: "Develop" },
];

const GROUPS = ["Cluster", "Data", "Develop"];

export default function Page() {
  const [me, setMe] = useState<api.Me | null | undefined>(undefined);
  const [clusters, setClusters] = useState<Cluster[] | null>(null);
  const [section, setSection] = useState<Section>("overview");
  const [clusterId, setClusterId] = useState<string | null>(null);
  const [collectionId, setCollectionId] = useState<string | null>(null);
  const [switcher, setSwitcher] = useState(false);
  const { toast, node } = useToast();

  const load = useCallback(async () => {
    const tree = await api.clusters();
    setClusters(tree);
    setClusterId((current) => current ?? tree[0]?.id ?? null);
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

  if (me === undefined) {
    return (
      <main className="flex h-screen items-center justify-center bg-canvas">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
      </main>
    );
  }

  if (me === null) return <Gate onIn={check} />;

  const cluster = clusters?.find((c) => c.id === clusterId) || null;
  const collections = cluster?.collections || [];

  /** A workspace with no cluster yet is the correct resting state, not an
   *  error — so the console offers to make one rather than showing chrome
   *  around nothing. */
  async function firstCollection() {
    await api.createCollection("Default collection");
    await load();
    toast("Collection created");
  }

  return (
    <main className="flex h-screen overflow-hidden bg-canvas text-ink">
      <aside className="flex w-56 shrink-0 flex-col border-r border-edge bg-panel">
        <div className="flex h-14 items-center border-b border-edge px-4">
          <Logo />
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
                await api.signOut().catch(() => {});
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
          <button
            onClick={() => setSwitcher((s) => !s)}
            disabled={!cluster}
            className="flex items-center gap-2.5 rounded-lg border border-edge bg-elevated px-3 py-1.5 transition hover:border-edgeStrong disabled:opacity-50"
          >
            <span className="h-2 w-2 rounded-full bg-success" />
            <Mono className="text-xs font-medium text-ink">{cluster?.name || "no cluster"}</Mono>
            {cluster && <Label>{cluster.region}</Label>}
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-subtle">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>

          {switcher && clusters && (
            <>
              <div className="fixed inset-0 z-20" onClick={() => setSwitcher(false)} />
              <div className="card-in absolute left-5 top-14 z-30 w-72 rounded-xl border border-edgeStrong bg-raised p-1.5 shadow-2xl shadow-black/50">
                <Label className="px-2 py-1">Switch cluster</Label>
                {clusters.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => {
                      setClusterId(c.id);
                      setCollectionId(null);
                      setSwitcher(false);
                    }}
                    className={cx(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition",
                      c.id === clusterId ? "bg-accent/10" : "hover:bg-elevated"
                    )}
                  >
                    <span className="h-2 w-2 shrink-0 rounded-full bg-success" />
                    <div className="min-w-0 flex-1">
                      <Mono className="block truncate text-xs text-ink">{c.name}</Mono>
                      <span className="text-2xs text-subtle">{c.region}</span>
                    </div>
                    <Chip>{c.collections.length} coll</Chip>
                  </button>
                ))}
              </div>
            </>
          )}

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
          ) : !cluster ? (
            <div className="px-6 py-16 text-center">
              <p className="text-sm text-ink">Nothing here yet</p>
              <p className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-subtle">
                A workspace starts empty. Create a collection and the console fills in as
                you put data into it — nothing is generated for you.
              </p>
              <button
                onClick={firstCollection}
                className="mt-4 rounded-lg border border-accent bg-accent px-3.5 py-2 text-xs font-semibold text-canvas transition hover:bg-accentSoft"
              >
                Create the first collection
              </button>
            </div>
          ) : (
            <>
              {section === "overview" && (
                <Overview
                  cluster={cluster}
                  collections={collections}
                  onOpen={(id) => {
                    setCollectionId(id);
                    setSection("collections");
                  }}
                  go={setSection}
                />
              )}
              {section === "collections" && (
                <Collections
                  collections={collections}
                  selected={collectionId}
                  onSelect={setCollectionId}
                  onQuery={() => setSection("query")}
                  onChanged={load}
                  toast={toast}
                />
              )}
              {section === "upload" && (
                <Upload collections={collections} toast={toast} onIngested={load} />
              )}
              {section === "query" && <Query collections={collections} />}
              {section === "keys" && <Keys toast={toast} />}
              {section === "sdk" && <Sdk collections={collections} />}
              {section === "traces" && <Traces collections={collections} />}
            </>
          )}
        </div>
      </div>
      {node}
    </main>
  );
}
