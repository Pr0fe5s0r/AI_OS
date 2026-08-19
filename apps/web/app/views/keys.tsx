"use client";

import { ReactNode, useEffect, useState } from "react";
import * as api from "../api";
import { ApiKey, Collection, MintedKey, ago, cx } from "../data";
import { Button, Card, Chip, Copy, Empty, Label, Mono } from "../ui/kit";

/** API keys.
 *
 *  The earlier version of this screen offered a "reveal" on every row. The
 *  store cannot do that and should not pretend to: only a hash is kept, so a
 *  key exists in readable form exactly once — in the response that created
 *  it. That single fact shapes the whole screen. */
export function Keys({
  collections,
  active,
  toast,
}: {
  collections: Collection[];
  active: string | null;
  toast: (m: string) => void;
}) {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [minted, setMinted] = useState<MintedKey | null>(null);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState("read,write");
  // "" is a MULTI-COLLECTION key (every collection in the workspace);
  // otherwise it is a SINGLE-COLLECTION key locked to the one named.
  // Defaults to whichever collection the sidebar is in, so the obvious act —
  // "create a key while looking at this collection" — produces a key for this
  // collection rather than one that quietly reaches all of them.
  const [collectionId, setCollectionId] = useState(active ?? "");
  useEffect(() => setCollectionId(active ?? ""), [active]);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);

  async function load() {
    setKeys(await api.keys());
  }

  // Which keys belong on screen while one collection is selected.
  //
  // A key bound to ANOTHER collection does not: it cannot reach this one, and
  // listing it here was the bug — standing in "test-hard" and reading a key
  // labelled "book". A key with no binding is a different matter and stays
  // visible everywhere, because it genuinely does reach this collection. That
  // is the honest cut: hiding it would leave real access to this collection
  // invisible on the one screen meant to show it.
  const shown = (keys || []).filter(
    (k) => !active || !k.collectionId || k.collectionId === active
  );
  const elsewhere = (keys || []).length - shown.length;

  useEffect(() => {
    load();
  }, []);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      setMinted(await api.createKey(name.trim(), scopes, collectionId || null));
      setName("");
      setCollectionId(active ?? "");
      setCreating(false);
      await load();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(k: ApiKey) {
    if (!confirm(`Revoke "${k.name}"?\n\nAnything using it stops working immediately.`)) return;
    await api.revokeKey(k.id);
    await load();
    toast("Key revoked");
  }

  return (
    <div className="px-4 py-5 sm:px-6 sm:py-6">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">API keys</h1>
          <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
            Authenticate every request with{" "}
            <Mono className="text-accentSoft">Authorization: Bearer</Mono>. A key carries the
            workspace, so nothing you write with it has to name one. It is{" "}
            <span className="text-muted">single-collection</span> when bound to one, and{" "}
            <span className="text-muted">multi-collection</span> when it is not.
          </p>
          {active && (
            <p className="mt-1.5 max-w-xl text-2xs leading-relaxed text-subtle">
              Showing keys that can reach{" "}
              <Mono className="text-accentSoft">{active}</Mono> — single-collection keys
              bound to it, and multi-collection keys, which reach every collection.
              {elsewhere > 0 && (
                <>
                  {" "}
                  {elsewhere} single-collection {elsewhere === 1 ? "key is" : "keys are"} bound
                  elsewhere and {elsewhere === 1 ? "is" : "are"} hidden here.
                </>
              )}
            </p>
          )}
        </div>
        <Button variant="primary" onClick={() => setCreating(true)}>
          + Create key
        </Button>
      </div>

      {/* The one moment the secret exists. */}
      {minted && (
        <Card className="card-in mb-5 border-hot/40 bg-hot/[0.06] p-4">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <Label className="text-hot">copy this now</Label>
              <p className="mt-1 text-xs text-muted">
                This is the only time <Mono className="text-ink">{minted.name}</Mono> can be
                read. Only a hash is stored — if it is lost, make another.
              </p>
              <div className="mt-2.5 flex items-center gap-2">
                <Mono className="min-w-0 flex-1 truncate rounded-lg border border-hot/30 bg-canvas px-3 py-2 text-xs text-hot">
                  {minted.key}
                </Mono>
                <Copy text={minted.key} label="copy key" />
              </div>
            </div>
            <button
              onClick={() => setMinted(null)}
              className="font-mono text-2xs text-subtle transition hover:text-ink"
            >
              done
            </button>
          </div>
        </Card>
      )}

      {creating && (
        <Card className="card-in mb-5 p-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-[14rem] flex-1">
              <Label className="mb-1.5 block">Label</Label>
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && create()}
                placeholder="ci-pipeline"
                className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
              />
            </label>
            <label>
              <Label className="mb-1.5 block">Scope</Label>
              <select
                value={scopes}
                onChange={(e) => setScopes(e.target.value)}
                className="rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
              >
                <option value="read,write">read + write</option>
                <option value="read">read only</option>
              </select>
            </label>
            <label>
              <Label className="mb-1.5 block">Collection</Label>
              <select
                value={collectionId}
                onChange={(e) => setCollectionId(e.target.value)}
                className="rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
              >
                <option value="">All collections (multi-collection key)</option>
                {collections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id}
                  </option>
                ))}
              </select>
            </label>
            <Button variant="primary" onClick={create} disabled={busy || !name.trim()}>
              {busy ? "…" : "Create"}
            </Button>
            <Button onClick={() => setCreating(false)}>Cancel</Button>
          </div>
        </Card>
      )}

      {keys === null ? (
        <div className="font-mono text-xs text-subtle">Loading keys…</div>
      ) : shown.length === 0 ? (
        <Empty
          title={active ? `No keys reach ${active}` : "No keys yet"}
          hint={
            elsewhere
              ? `A key is how the SDK, an MCP client or a CI job reaches this collection. ${elsewhere} single-collection ${elsewhere === 1 ? "key is bound" : "keys are bound"} elsewhere and cannot reach this one.`
              : "A key is how the SDK, an MCP client or a CI job reaches this workspace. The console itself uses your session instead."
          }
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              {active ? `Create a key for ${active}` : "Create your first key"}
            </Button>
          }
        />
      ) : (
        <Card className="overflow-hidden">
          {/* A table has a floor width. Let it scroll inside the card instead
              of widening the page, which is what pushed the whole console
              sideways on a phone. */}
          <div className="overflow-x-auto">
          <table className="w-full min-w-[34rem] text-left">
            <thead>
              <tr className="border-b border-edge">
                <Th className="pl-4">Label</Th>
                <Th>Key</Th>
                <Th>Scope</Th>
                <Th>Last used</Th>
                <Th className="pr-4" />
              </tr>
            </thead>
            <tbody>
              {shown.map((k) => (
                <tr
                  key={k.id}
                  className={cx(
                    "border-b border-edge/60 last:border-0",
                    k.revoked && "opacity-45"
                  )}
                >
                  <td className="py-3 pl-4">
                    <div className="flex items-center gap-1.5">
                      <Mono className="text-xs text-ink">{k.name}</Mono>
                      {k.collectionId ? (
                        <Chip
                          title="Single-collection key: it can reach this collection and no other."
                          tone="text-heat-2 border-heat-2/30 bg-heat-2/10"
                        >
                          {k.collectionId}
                        </Chip>
                      ) : (
                        // Named for its reach, not its container. The tooltip
                        // always said "every collection in the workspace"
                        // while the label read "workspace", which described
                        // where the key lives rather than what it opens.
                        <Chip title="Multi-collection key: it reaches every collection in this workspace.">
                          all collections
                        </Chip>
                      )}
                    </div>
                    <div className="mt-0.5 text-2xs text-subtle">
                      created {ago(k.createdAt)}
                      {k.createdBy ? ` by ${k.createdBy}` : ""}
                    </div>
                  </td>
                  <td className="py-3">
                    <Mono className="text-2xs text-muted">{k.prefix}</Mono>
                    <span className="ml-1 font-mono text-2xs text-subtle">••••••••••••</span>
                  </td>
                  <td className="py-3">
                    <Chip
                      tone={
                        k.scopes.includes("write")
                          ? "text-accentSoft border-accent/40 bg-accent/10"
                          : "text-heat-1 border-heat-1/30 bg-heat-1/10"
                      }
                    >
                      {k.scopes.join(" + ")}
                    </Chip>
                  </td>
                  <td className="py-3 font-mono text-2xs text-subtle">{ago(k.lastUsed)}</td>
                  <td className="py-3 pr-4 text-right">
                    {k.revoked ? (
                      <Chip tone="text-danger border-danger/30 bg-danger/10">revoked</Chip>
                    ) : (
                      <button
                        onClick={() => revoke(k)}
                        className="font-mono text-2xs text-subtle transition hover:text-danger"
                      >
                        revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Card>
      )}
    </div>
  );
}

function Th({ children, className }: { children?: ReactNode; className?: string }) {
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
