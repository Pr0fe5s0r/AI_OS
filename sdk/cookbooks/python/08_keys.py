"""08 · API keys — mint a scoped key, list keys, revoke.

A key minted with `collection_id=` is confined to that collection server-side:
every call it makes is forced into that collection whatever it asks for. The
secret exists exactly once — in the create response. Store it then; only its
hash is kept.

    python python/08_keys.py
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError


def main() -> None:
    with Markvector() as mv:
        # A read-only key locked to one collection — safe to hand to a bot.
        minted = mv.create_key(
            "cookbook-readonly-bot",
            scopes="read",
            collection_id="cookbook",
        )
        print("NEW KEY (store it now, it is never shown again):")
        print(" ", minted.key)
        print(" ", "scopes:", minted.scopes, "collection:", minted.collection_id)

        # List existing keys — prefixes and usage only, never the secrets.
        print("\nkeys in workspace:")
        for k in mv.keys():
            scope = k.collection_id or "workspace-wide"
            state = "revoked" if k.revoked else "active"
            print(f"  {k.prefix}…  {k.name}  [{','.join(k.scopes)}]  {scope}  {state}")

        # Revoke the one we just made — it stops working immediately.
        mv.revoke_key(minted.key_id)
        print(f"\nrevoked {minted.key_id}")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
