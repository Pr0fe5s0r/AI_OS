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

from .agent import (
    Agent,
    AgentAnswer,
    AgentResult,
    Thinking,
    ToolCall,
    ToolResult,
)
from .client import SCOPES, Collection, Markvector, Where
from .errors import (
    AuthError,
    IndexingTimeout,
    InvalidRequest,
    MarkvectorError,
    NotFound,
    RateLimited,
    Unavailable,
)
from .models import (
    Answer,
    ApiKey,
    Category,
    Chunk,
    Citation,
    CollectionInfo,
    Deletion,
    Document,
    IndexSummary,
    Match,
    MintedKey,
    Neighbor,
    Original,
    Region,
    Results,
    Section,
    Source,
    Structure,
    WriteResult,
)
from .patterns import Pattern, PatternReport

__version__ = "0.3.0"

__all__ = [
    "Agent",
    "AgentAnswer",
    "AgentResult",
    "Answer",
    "ApiKey",
    "AuthError",
    "Category",
    "Chunk",
    "Citation",
    "Collection",
    "CollectionInfo",
    "Deletion",
    "Document",
    "IndexSummary",
    "IndexingTimeout",
    "InvalidRequest",
    "Markvector",
    "MarkvectorError",
    "Match",
    "MintedKey",
    "Neighbor",
    "NotFound",
    "Pattern",
    "PatternReport",
    "Original",
    "RateLimited",
    "Region",
    "Results",
    "SCOPES",
    "Section",
    "Source",
    "Structure",
    "Thinking",
    "ToolCall",
    "ToolResult",
    "Unavailable",
    "Where",
    "WriteResult",
    "__version__",
]
