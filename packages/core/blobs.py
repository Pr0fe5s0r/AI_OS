from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# ORIGINAL FILE STORE.
#
# The KB indexes the Markdown a document becomes, but a person still wants to
# see the file they uploaded — a PDF as a PDF, an image as an image. That
# original lives here, in S3-compatible object storage (MinIO locally), keyed
# by the item it belongs to. The index in Postgres/Neo4j is the source of
# truth for *what a document says*; this is only the bytes it arrived as.
#
# Keyed by item_id, not by version: re-uploading a changed document overwrites
# the stored original so it always matches the text and passages on screen.
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """False when no object store is configured — then originals are simply not
    kept, exactly as before this feature existed, and the viewer degrades to
    text + passages."""
    return bool(os.getenv("S3_ENDPOINT"))


def _bucket() -> str:
    return os.getenv("S3_BUCKET", "originals")


def max_original_bytes() -> int:
    """Files larger than this are still indexed, but their original is not kept:
    object storage is cheap, but a store is not a backup target."""
    return int(os.getenv("S3_MAX_ORIGINAL_MB", "200")) * 1024 * 1024


def key_for(workspace_id: str, item_id: str) -> str:
    return f"{workspace_id}/{item_id}"


# How many times a prefix delete re-lists before giving up. The listing lags a
# burst of writes, so one pass is not enough; an unbounded loop would spin on a
# bucket somebody else is still writing to.
MAX_DELETE_SWEEPS = 6


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        # MinIO speaks path-style; virtual-host style needs DNS per bucket.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


def _ensure_bucket_sync() -> None:
    client = _client()
    try:
        client.head_bucket(Bucket=_bucket())
    except ClientError:
        # 404 (missing) or 403 (no head perm) -> try to create; a race with
        # another worker doing the same is not an error.
        try:
            client.create_bucket(Bucket=_bucket())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise


async def ensure_bucket(retries: int = 10, delay: float = 1.5) -> None:
    """Create the bucket if it is missing. Retried, because object storage may
    come up a beat after the app on a cold `compose up`."""
    if not enabled():
        return
    last: Exception | None = None
    for _ in range(retries):
        try:
            await asyncio.to_thread(_ensure_bucket_sync)
            return
        except Exception as exc:  # noqa: BLE001 - startup: log-and-retry, not classify
            last = exc
            await asyncio.sleep(delay)
    if last is not None:
        raise last


async def put(key: str, data: bytes, content_type: str) -> None:
    await asyncio.to_thread(
        lambda: _client().put_object(
            Bucket=_bucket(), Key=key, Body=data, ContentType=content_type
        )
    )


async def get(key: str) -> tuple[bytes, str]:
    """Return (bytes, content_type). Raises if the object is absent."""

    def _read() -> tuple[bytes, str]:
        obj = _client().get_object(Bucket=_bucket(), Key=key)
        return obj["Body"].read(), obj.get("ContentType", "application/octet-stream")

    return await asyncio.to_thread(_read)


async def delete_prefix(prefix: str) -> int:
    """Remove every object under a prefix. Returns how many went.

    Everything one document owns is stored beneath `{workspace}/{item}` — the
    original, each rendered page, each page's figure verdicts — so deleting the
    document is deleting that prefix. Item ids are fixed-length hex, so no other
    document's key can begin with another's and the prefix cannot over-reach.

    Paginated because a 962-page PDF has nearly a thousand rendered pages, and
    the list call caps at a thousand keys: without the continuation token the
    tail would be left behind, which is the quiet half of a deletion that
    reports success.

    And SWEPT REPEATEDLY, which is the part that is not obvious. Measured
    against MinIO: write an original plus 1,200 page pictures, then list the
    prefix, and the listing answers ONE. Delete what it offered, list again,
    and the same prefix now answers 1,201. The objects were all there the whole
    time — the listing index lags a burst of writes — so a single
    list-then-delete pass leaves behind whatever it could not yet see, and
    reports success while doing it.

    So the sweep repeats until a pass finds nothing, bounded so a bucket that
    is being written to concurrently cannot spin here forever. Anything that
    survives all of it is unreachable rather than exposed: every route that
    serves an object looks the document up in Postgres first, and those rows
    are gone.
    """
    if not enabled():
        return 0

    def _purge() -> int:
        client = _client()
        bucket = _bucket()
        removed = 0
        for _sweep in range(MAX_DELETE_SWEEPS):
            found = 0
            token: str | None = None
            while True:
                page = client.list_objects_v2(
                    **{
                        "Bucket": bucket,
                        "Prefix": prefix,
                        **({"ContinuationToken": token} if token else {}),
                    }
                )
                keys = [{"Key": row["Key"]} for row in page.get("Contents", [])]
                if keys:
                    client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                    found += len(keys)
                token = page.get("NextContinuationToken")
                if not page.get("IsTruncated"):
                    break
            removed += found
            if found == 0:
                break
        return removed

    return await asyncio.to_thread(_purge)


async def exists(key: str) -> bool:
    def _head() -> bool:
        try:
            _client().head_object(Bucket=_bucket(), Key=key)
            return True
        except ClientError:
            return False

    return await asyncio.to_thread(_head)


__all__ = [
    "enabled",
    "ensure_bucket",
    "exists",
    "get",
    "key_for",
    "max_original_bytes",
    "put",
]
