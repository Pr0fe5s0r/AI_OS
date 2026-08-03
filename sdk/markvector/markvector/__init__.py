"""markvector — the Python client for the MarkVector knowledge store.

    from markvector import Markvector

    mv = Markvector(api_key="kb_live_…")            # or set MARKVECTOR_API_KEY
    docs = mv.collection("client-research")
    docs.add("Q2 paid conversions fell 18 percent.", locator="notes/q2", wait=True)

    for hit in docs.search("why did paid results fall"):
        print(hit.score, hit.title, hit.matched_on)

    print(docs.answer("summarise Q2 performance"))   # grounded, cited answer

Store documents (text, PDF, Word, Markdown), search them by meaning and exact
wording together, get grounded answers with citations, read the passages a
document was split into, and download the original file as it was uploaded.
"""

from .client import Collection, Markvector
from .errors import (
    AuthError,
    IndexingTimeout,
    InvalidRequest,
    MarkvectorError,
    NotFound,
    Unavailable,
)
from .models import (
    Answer,
    ApiKey,
    Category,
    Chunk,
    Citation,
    CollectionInfo,
    Document,
    Match,
    MintedKey,
    Original,
    Results,
    Source,
    WriteResult,
)

__version__ = "0.1.0"

__all__ = [
    "Answer",
    "ApiKey",
    "AuthError",
    "Category",
    "Chunk",
    "Citation",
    "Collection",
    "CollectionInfo",
    "Document",
    "IndexingTimeout",
    "InvalidRequest",
    "Markvector",
    "MarkvectorError",
    "Match",
    "MintedKey",
    "NotFound",
    "Original",
    "Results",
    "Source",
    "Unavailable",
    "WriteResult",
    "__version__",
]
