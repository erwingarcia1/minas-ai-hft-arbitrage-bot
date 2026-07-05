# Cross-Exchange Crypto Arbitrage System

Latency-aware cross-exchange arbitrage system for **Binance (global), Bybit,
OKX (global), and Bitget** spot markets. Python 3.11+, single-process asyncio,
native WebSocket feeds.

> **Current status: Phase 1 — market data + spread observer.**
> This build maintains live L2 order books, detects theoretical arbitrage
> opportunities **net of taker fees and depth-implied slippage**, and records
> them for research. **It contains no order-placement code whatsoever** — the
> `arb/execution/` package is intentionally empty, no API keys are read, and
> nothing in this build can trade. Run it for days first: the data it collects
> answers whether real net-positive spreads exist *at your fee tier* before any
> capital is at risk.

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 1 | WebSocket feeds, book maintenance, net-spread detection, opportunity research log | **this build** |
| 2 | Paper execution: order manager, kill-switch risk manager, PnL accounting (simulated fills vs live book) | not started |
| 3 | Live, minimum size, one symbol, two venues, kill switch armed and deliberately test-fired first | not started |
| 4 | Hardening: chaos-tested reconnects, error-code matrix, rate-limit budgets, Docker, multi-region notes | not started |

## Architecture (Phase 1)

```
feeds/       native WS clients per venue -> local L2 books (Decimal-exact,
             sequence-gap + CRC32-checksum validated, staleness-stamped)
signals/     executable-spread detector: VWAP depth walk per clip size,
             net of both taker fees; opportunity open/close lifecycle
risk/        staleness monitor (>500ms or unsynced => venue unusable);
             the Phase-2 kill switch attaches at this seam
accounting/  JSONL research log: opportunity records + periodic spread samples
monitoring/  JSON logs, Prometheus /metrics, rich terminal dashboard
config/      one YAML, strictly validated (unknown keys are fatal)
```

Why native WebSockets instead of ccxt.pro: every venue's book-sync protocol
(sequence rules, snapshot semantics, checksums, heartbeats) is implemented
explicitly, because gap detection and resync behaviour *are* the product in an
arb system. ccxt.pro abstracts exactly the failure modes we need to observe;
it remains an acceptable fallback behind the same `WsFeed` interface if a
venue's protocol changes faster than we maintain it.

Per-venue integrity mechanisms:

| Venue | Stream | Integrity |
|---|---|---|
| Binance | `<sym>@depth@100ms` diff + REST snapshot | official `U`/`u`/`lastUpdateId` chaining (applied to live AND buffered events); gap ⇒ snapshot resync |
| Bybit | `orderbook.50.<SYM>` (v5 spot) | `u` continuity; `u==1` restart rule; gap ⇒ resubscribe |
| OKX | `books` (400 levels) | `seqId`/`prevSeqId` chain (OKX deprecated the books checksum on 2026-05-21 — field now fixed to 0; non-zero checksums from older gateways are still verified) |
| Bitget | `books` (v2 spot) | CRC32 checksum every message |

Every in-band resubscribe is guarded by a snapshot deadline (no snapshot ⇒
recycle the connection) and a storm guard (repeated back-to-back resyncs ⇒
recycle instead of spamming sub/unsub against venue rate limits).

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                       # unit tests: book engine, fee math, detector
python -m arb observe        # starts the observer (public data only)
```

- Config: `config/config.yaml`. **Set the fee tiers to your actual tiers** —
  the default is retail VIP0 and the entire point of Phase 1 is measuring edge
  net of *your* fees.
- Prometheus metrics: `http://localhost:9109/metrics`
  (spread histograms, book age, resync counts, feed latency, reconnects).
- Research data: `data/opportunities-YYYYMMDD.jsonl` — opportunity open/close
  records (`type=opportunity`) and unconditional periodic spread samples
  (`type=spread_sample`, negative edges included).
- Dashboard: on by default in a TTY; logs then go to `logs/`. Use
  `--no-dashboard` for headless runs (logs to stderr as JSON lines).

No `.env` is needed in Phase 1. `.env.example` documents the variables later
phases will use and the key-permission requirements below.

## API key permission checklist (for Phase 2+ — nothing reads keys yet)

When you eventually provision keys, they must be:

- [ ] **Trade-only.** Withdrawals DISABLED. Internal/sub-account transfers DISABLED.
- [ ] **IP-whitelisted** to the machine that runs this system.
- [ ] Scoped to spot only, no margin/derivatives permissions.
- [ ] One key pair per venue per environment (paper vs live never share keys).
- [ ] Stored only in `.env` (gitignored) / environment variables — never in code or config.
- [ ] Inventory pre-positioned on each venue; this system will never move funds
      between venues (no transfer code exists by policy).

## Risks of running this

Phase 1 is read-only and risks no capital, but be clear-eyed about the system
it is the foundation of:

- **Cross-exchange spot arbitrage is mostly a fee/latency game.** At retail
  taker fees (10bps × 2 legs), a spread must exceed ~20bps *after* depth-walk
  slippage before it is worth anything. Such spreads are rare, brief, and
  contested by co-located firms with better fee tiers. Expect the Phase-1 data
  to be sobering; that is the cheap lesson, and it is why observation comes first.
- **Latency reality.** From a single non-co-located machine (e.g. North
  America) expect 150–300ms+ round trips to these venues' matching engines
  (mostly AWS ap-northeast-1 Tokyo / ap-southeast-1 Singapore / Hong Kong).
  Co-located in-region you might see 10–35ms. This build does not and cannot
  claim better; opportunities shorter than your round trip are unexecutable
  fiction, which is exactly what the persistence-duration field in the
  research log measures.
- **Execution risk (later phases):** one-leg fills, partial fills, venue
  outages mid-trade, withdrawal freezes stranding inventory, API bans from
  rate-limit violations.
- **Counterparty/venue risk:** exchange insolvency, sudden fee changes,
  delisted pairs. Pre-positioned inventory is exposed to every venue you fund.
- **Regime risk:** spreads that existed for weeks can vanish permanently when
  one market-maker adds a venue.
- **Operational honesty:** clock skew makes the feed-latency metric a trend
  indicator, not truth; JSONL research data is append-only and unencrypted —
  treat the box as sensitive infrastructure from day one.

## Known limitations (Phase 1)

- Fees are modelled in quote terms on both legs (some venues charge spot
  buy-side fees in base asset — equivalent for edge math at these sizes).
- The Bybit v5 spot depth stream has no checksum; integrity rests on `u`
  continuity, resolved toward resync on any ambiguity.
- Feed-latency histograms include local/exchange clock skew.
- Deliberate exception to "no environment reads": the Binance REST snapshot
  client uses `trust_env=True` so the observer works behind HTTP(S) proxies.
  aiohttp will then also consult `~/.netrc` for `api.binance.com` — do not
  keep exchange credentials in `.netrc`. Nothing else reads the environment.
- Development-sandbox note: this repository was built and unit-tested in an
  environment whose egress policy blocks exchange endpoints; run the observer
  from your own machine to validate live behaviour end-to-end.

## Tests

```bash
pytest -q
```

Covers: book snapshot/delta/removal semantics, CRC32 checksum construction
(interleaving, uneven sides, literal-string formatting), VWAP depth walks and
insufficient-depth behaviour, exact fee math, detector lifecycle (open,
decay-close, staleness gate, unsynced gate, insufficient depth, venue-unusable,
shutdown flush), Binance snapshot-sync algorithm (buffered overlap, duplicates,
gap resync), Bybit sequencing (snapshot/delta chain, `u==1` restart, gap
resubscribe), and strict config validation.
