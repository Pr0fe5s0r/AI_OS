"""Python client for the Knowledge Base.

    from knowledgebase import KnowledgeBase

    kb = KnowledgeBase(api_key="kb_live_…")
    docs = kb.collection("client-research")
    docs.add("Q2 paid conversions fell 18 percent.", locator="notes/q2", wait=True)

    for match in docs.search("why did paid results fall"):
        print(match.score, match.title, match.matched_on)
"""

from .client import Collection, KnowledgeBase
from .errors import (
    AuthError,
    IndexingTimeout,
    InvalidRequest,
    KnowledgeBaseError,
    NotFound,
    Unavailable,
)
from .models import (
    Category,
    CollectionInfo,
    Document,
    Match,
    Results,
    Source,
    WriteResult,
)

__version__ = "0.1.0"

__all__ = [
    "AuthError",
    "Category",
    "Collection",
    "CollectionInfo",
    "Document",
    "IndexingTimeout",
    "InvalidRequest",
    "KnowledgeBase",
    "KnowledgeBaseError",
    "Match",
    "NotFound",
    "Results",
    "Source",
    "Unavailable",
    "WriteResult",
    "__version__",
]
