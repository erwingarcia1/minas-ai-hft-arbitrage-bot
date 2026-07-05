"""Native WebSocket market-data feeds and local L2 order books.

Why native (not ccxt.pro): each venue's book-sync protocol (sequence numbers,
snapshot semantics, CRC32 checksums, heartbeats) is implemented directly so
that gap detection and resync behaviour are explicit and auditable. ccxt.pro
abstracts exactly the failure modes an arbitrage system must observe. It may
be reintroduced later as a fallback behind the same ``WsFeed`` interface.

Failure modes (module-wide):
- Any disconnect marks every book on that venue unsynced; the spread detector
  refuses unsynced or stale books, so a dead feed can never produce a signal.
- Sequence gaps / checksum mismatches trigger an explicit resync (snapshot
  refetch or resubscribe) and increment ``arb_book_resyncs_total``.
- Parsing errors on a single message are logged with the raw payload excerpt
  and counted; they do not kill the connection loop.
"""

from arb.feeds.base import FeedState, WsFeed
from arb.feeds.book import BookRegistry, OrderBook

__all__ = ["BookRegistry", "FeedState", "OrderBook", "WsFeed"]
