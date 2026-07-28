"use client";

import { useState } from "react";
import { Facets, TaxonomyClass, api, cx } from "../lib";
import { Button, Chip, Empty } from "../ui/kit";

/** Managing the categories. Adding one is a row in a table, not a release —
 *  which is why this screen exists at all. */
export function Taxonomy({
  classes,
  facets,
  onChanged,
  onFilter,
}: {
  classes: TaxonomyClass[];
  facets: Facets | null;
  onChanged: () => void;
  onFilter: (classId: string) => void;
}) {
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const counts = new Map((facets?.classes || []).map((c) => [c.class_id, c.count]));
  const top = classes.filter((c) => !c.parent_id);
  const kids = (id: string) => classes.filter((c) => c.parent_id === id);

  async function add() {
    const trimmed = name.trim();
    if (!trimmed) return;
    const slug = trimmed
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
    if (!slug) {
      setError("Give the category a name using letters or numbers.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api("/api/classes", {
        method: "POST",
        body: JSON.stringify({
          class_id: slug,
          name: trimmed,
          parent_id: parent || null,
        }),
      });
      setName("");
      setParent("");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(c: TaxonomyClass) {
    if (
      !confirm(
        `Delete “${c.name}”?\n\nDocuments filed under it stay in the knowledge base, but they lose this label.`
      )
    )
      return;
    try {
      await api(`/api/classes/${c.class_id}`, { method: "DELETE" });
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-3xl">
        <header className="mb-5">
          <h2 className="text-sm font-semibold text-ink">Categories</h2>
          <p className="mt-1 text-xs leading-relaxed text-subtle">
            Documents are filed automatically as they arrive. You decide what the
            categories are — add one here and it takes effect immediately, with no
            release needed.
          </p>
        </header>

        <div className="mb-6 rounded-xl border border-edge bg-panel p-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-[12rem] flex-1">
              <span className="mb-1 block text-2xs uppercase tracking-wide text-subtle">
                New category
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && add()}
                placeholder="Retainer, Paid social, Brand guidelines…"
                className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
              />
            </label>
            <label>
              <span className="mb-1 block text-2xs uppercase tracking-wide text-subtle">
                Inside
              </span>
              <select
                value={parent}
                onChange={(e) => setParent(e.target.value)}
                className="rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent/60"
              >
                <option value="">Top level</option>
                {top.map((c) => (
                  <option key={c.class_id} value={c.class_id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <Button variant="primary" onClick={add} disabled={busy || !name.trim()}>
              {busy ? "Adding…" : "Add"}
            </Button>
          </div>
          {error && <p className="mt-2 text-2xs text-danger">{error}</p>}
        </div>

        {classes.length === 0 ? (
          <Empty title="No categories yet" />
        ) : (
          <ul className="space-y-1.5">
            {top.map((c) => (
              <li key={c.class_id}>
                <Row
                  c={c}
                  count={counts.get(c.class_id) || 0}
                  onRemove={remove}
                  onFilter={onFilter}
                />
                {kids(c.class_id).length > 0 && (
                  <ul className="ml-6 mt-1.5 space-y-1.5 border-l border-edge pl-3">
                    {kids(c.class_id).map((k) => (
                      <li key={k.class_id}>
                        <Row
                          c={k}
                          count={counts.get(k.class_id) || 0}
                          onRemove={remove}
                          onFilter={onFilter}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Row({
  c,
  count,
  onRemove,
  onFilter,
}: {
  c: TaxonomyClass;
  count: number;
  onRemove: (c: TaxonomyClass) => void;
  onFilter: (id: string) => void;
}) {
  return (
    <div className="group flex items-center gap-3 rounded-lg border border-edge bg-panel px-3 py-2.5">
      <button
        onClick={() => onFilter(c.class_id)}
        className="min-w-0 flex-1 text-left"
        title="Show what is filed here"
      >
        <span className="text-sm text-ink">{c.name}</span>
        {c.description && (
          <span className="ml-2 text-2xs text-subtle">{c.description}</span>
        )}
      </button>
      <span className="shrink-0 tabular-nums text-2xs text-subtle">{count}</span>
      {c.scope === "platform" ? (
        <Chip tone="text-subtle border-edgeStrong bg-elevated" title="Ships with the product">
          built in
        </Chip>
      ) : (
        <button
          onClick={() => onRemove(c)}
          className="rounded px-1.5 py-0.5 text-2xs text-subtle opacity-0 transition hover:text-danger group-hover:opacity-100"
        >
          Delete
        </button>
      )}
    </div>
  );
}
