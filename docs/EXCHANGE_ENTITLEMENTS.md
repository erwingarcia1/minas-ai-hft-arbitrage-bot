# EXCHANGE_ENTITLEMENTS.md — Per-Account Entitlement Register

> **Status: SPECIFIED — NOT IMPLEMENTED.**
> Today the repository contains only the Phase-1 public-market-data observer:
> four public WebSocket feed adapters and a public REST instrument fetch. **No
> exchange account is connected, no API key exists or is read anywhere in the
> code, and every entry in the matrix below is UNVERIFIED.** This document
> specifies the entitlement-discovery process (CLAUDE.md §8, constrained by
> §3) that must be completed before any phase that connects an account —
> shadow trading (CLAUDE.md Milestone 3, §50 / Mode 4) at the earliest, and
> unconditionally before limited live trading (Mode 5 / README Phase 3).

Governing spec: CLAUDE.md §8 (discovery and entitlements), §3 (non-negotiable
safety), §2 (venue protocol availability), §33 (rate-limit engine consumes
this register), §44 (live qualification).

---

## 1. Purpose and scope

A **capability** is something a venue offers to some class of customer. An
**entitlement** is something *this specific account* is permitted and able to
use, proven against that account. The two are recorded separately:

| Register | Question answered | Where |
|---|---|---|
| Capability matrix | "Does the venue offer X at all?" | `docs/EXCHANGE_CAPABILITY_MATRIX.md` |
| Entitlement register (this doc) | "May *our account* use X, verified when, per which doc version?" | this doc + the versioned registry of §3 below |

CLAUDE.md §8 requires: consult only current official exchange documentation,
do not hard-code published limits as permanent constants, and **enable only
capabilities available to the actual account**. §2 additionally forbids
claiming institutional-grade access that the account does not have.

## 2. Verification states

Every entitlement entry carries exactly one state:

| State | Meaning | Sufficient for |
|---|---|---|
| `UNVERIFIED` | Nothing checked. The shipped default for every cell. | Nothing |
| `DOC_NOTED` | Described in official public docs; doc page name and retrieval date recorded; **not** checked against any account | Planning only. Public-market-data code may run without entitlements because it authenticates nothing. |
| `ACCOUNT_VERIFIED` | Proven against the real account via an authenticated API call or an official account page, with evidence recorded | Enabling the dependent feature in the mode being qualified |
| `UNAVAILABLE` | Verified as absent for this account (wrong tier, wrong region, feature not offered) | Marking dependent features permanently disabled for this venue |
| `EXPIRED` | Was `ACCOUNT_VERIFIED`; `verified_at` older than the re-verification TTL, or the venue announced a change since | Nothing — treated as `UNVERIFIED` by all gates |

## 3. Entitlement record schema

The register must be stored as versioned data (config or database table in
later phases), not prose. Per CLAUDE.md §8 the registry struct is:

```rust
struct ExchangeCapabilities {
    exchange: ExchangeId,
    websocket_market_data: bool,
    websocket_order_entry: bool,
    fix_order_entry: bool,
    sbe_market_data: bool,
    fast_order_response: bool,
    disconnect_cancel_all: bool,
    self_trade_prevention: bool,
    batch_orders: bool,
    spot_ioc: bool,
    spot_fok: bool,
    spot_post_only: bool,
    demo_environment: bool,
    verified_at: Timestamp,
    documentation_version: String,
}
```

Each individual entry (row × venue cell) must additionally carry:

| Field | Type | Content |
|---|---|---|
| `state` | enum | One of §2 above |
| `value` | string/typed | The discovered value (endpoint URL, limit, tier name, …) |
| `source` | string | Official doc page name, authenticated endpoint called, or support-ticket ID |
| `documentation_version` | string | Doc version string or changelog date **as published by the venue**; `"unverified"` if the venue does not date its docs |
| `verified_at` | timestamp | When the check was performed (UTC) |
| `verified_by` | string | Operator or agent identity |
| `notes` | string | Caveats (regional variance, tier dependence, …) |

Changes to the register must be append-only with before/after values, reason,
identity and timestamp — the same audit discipline §29 requires for risk
limits, because entitlements gate what the risk engine will permit.

## 4. Verification process

1. **Doc pass (`DOC_NOTED`).** Read the *current official* documentation page
   for the field. Record page name, URL, the venue's own version/changelog
   date if published, and the retrieval date. Never copy numbers from blogs,
   SDKs, or model memory.
2. **Account pass (`ACCOUNT_VERIFIED`).** Once an account exists (Milestone 3
   at the earliest), verify each field with an authenticated call where one
   exists — e.g. the adapter contract's `fetch_account_fee_rate()`,
   `fetch_instruments()`, `fetch_symbol_rules()` (§9) — or with a recorded
   screenshot/export of the official account page where no API exists (VIP
   level, IP allowlist configuration). Stamp `verified_at`,
   `documentation_version`, `verified_by`.
3. **Key-permission inspection (mandatory, blocking).** Per §3, on every key
   load the system must inspect the key's permissions via the venue's
   key-info endpoint and **reject any credential with withdrawal permission**
   or transfer permission. This inspection is itself an entitlement entry
   ("supported key types" / "account entitlements") and a precondition of §3
   live-activation item 5.
4. **Expiry and re-verification.** Every `ACCOUNT_VERIFIED` entry expires
   after a configurable TTL (proposed default: 30 days — a project policy
   choice, not a venue requirement) and immediately upon: a venue changelog
   announcement touching the field, a key rotation, a fee-tier change, or
   entry into any qualification gate of §44. Expired entries revert to
   `EXPIRED` and block dependent features until re-verified.
5. **Fail safely** (§8): see §7 below.

## 5. Discovery matrix (fillable — ships empty)

Every field from CLAUDE.md §8, one row each. **Current value of every cell:
`UNVERIFIED`.** No account exists to verify against; do not fill a cell
without following §4.

| # | Field | OKX Global | Bitget | Bybit | Binance Global |
|---|---|---|---|---|---|
| 1 | Public API endpoint | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 2 | Private API endpoint | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 3 | Regional endpoint(s) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 4 | WebSocket endpoint (market data) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 5 | WebSocket order-entry endpoint | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 6 | FIX endpoint | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 7 | SBE endpoint / SBE market data | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 8 | Authentication method | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 9 | Supported key types | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 10 | Clock requirements (recv window / timestamp rules) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 11 | Connection limits | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 12 | Subscription limits | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 13 | Order limits (rate) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 14 | Cancellation limits | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 15 | Batch-order capabilities | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 16 | Client-order-ID rules (charset, length) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 17 | Order types | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 18 | Time-in-force options (IOC/FOK/post-only/GTC) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 19 | Spot symbol filters (tick/step/min-notional source) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 20 | Fee-rate endpoints | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 21 | Account-level fee tier | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 22 | Private order stream | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 23 | Private execution (fill) stream | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 24 | Balance stream | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 25 | Disconnect-cancel-all feature | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 26 | Self-trade-prevention options | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 27 | Test / demo environment | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 28 | Institutional access requirements | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 29 | VIP requirements (for faster feeds / higher limits) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 30 | Account entitlements (spot trade scope, sub-accounts) | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |

### 5.1 Public-docs capability notes — *from public docs, unverified against an account*

The only cells with any grounding today are the public endpoints the Phase-1
code actually connects to (readable in `arb/feeds/*.py` and
`arb/instruments/fetch.py`). These are facts about this repository's code;
they still count only as `DOC_NOTED` for register purposes because no account
has confirmed anything, and the documentation dates below are **unverified**.

| Venue | Public endpoints used by Phase-1 code (rows 1, 4) | Official doc page to consult (rows 11–14) |
|---|---|---|
| Binance Global | WS `wss://stream.binance.com:9443/stream` (combined diff-depth streams); REST `https://api.binance.com/api/v3/depth`, `/api/v3/exchangeInfo` | "Binance Spot API — WebSocket Streams" and "General API Information / LIMITS"; `exchangeInfo` also returns a machine-readable `rateLimits` array — prefer that over transcribed constants. Doc date: unverified. |
| Bybit | WS `wss://stream.bybit.com/v5/public/spot` (`orderbook.50.<SYM>`); REST `https://api.bybit.com/v5/market/instruments-info?category=spot` | "Bybit V5 API — Rate Limit" and "WebSocket — Connect". Doc date: unverified. |
| OKX Global | WS `wss://ws.okx.com:8443/ws/v5/public` (`books`, 400 levels); REST `https://www.okx.com/api/v5/public/instruments?instType=SPOT` | "OKX V5 API — Rate Limits" and "WebSocket — Overview". Note: OKX deprecated the `books` checksum (field fixed to 0) per its changelog as recorded in this repo's README/code; date recorded there as 2026-05-21, unverified here. Doc date: unverified. |
| Bitget | WS `wss://ws.bitget.com/v2/ws/public` (`books`, v2 spot, CRC32-checksummed); REST `https://api.bitget.com/api/v2/spot/public/symbols` | "Bitget API v2 — Rate Limits / Restrictions" and "Spot WebSocket — Public Channel". Doc date: unverified. |

**No rate-limit, connection-limit, or subscription-limit numbers are stated
in this document deliberately.** Per §8 they must not be hard-coded as
permanent constants; the rate-limit engine (§33) must load them from this
register after a §4 verification pass, with `documentation_version` attached.

## 6. Account-level facts required before an account connects

Before Milestone 3 (shadow trading — first use of real keys, read-only) and
again, with stricter evidence, before README Phase 3 / Mode 5 (limited live),
every row below must reach `ACCOUNT_VERIFIED` per venue:

| Fact | Why it gates progress | How captured | Blocking rule |
|---|---|---|---|
| Account fee tier / VIP level (maker + taker bps per symbol class) | Opportunity math is net-of-fees; the Phase-1 detector already takes fee bps from config (`arb/signals/fees.py`) and defaults to retail VIP0 | `fetch_account_fee_rate()` (§9) + account page export | Uncertain fees ⇒ conservative fee + uncertainty reserve + no live trading if material (§26) |
| API key permission set | §3: withdrawal- or transfer-enabled keys must be **rejected** at load | Venue key-info endpoint, inspected on every startup | Any withdrawal/transfer permission ⇒ hard reject, security event |
| Key scope | Spot-only; no margin, no futures (§3 defaults) | Key-info endpoint | Extra scopes ⇒ reject |
| IP allowlist | Keys must be IP-restricted to the operating host (§38; README checklist) | Account page export + a probe call from a non-allowed host must fail | Unrestricted key ⇒ not live-eligible |
| Private stream entitlements (orders, executions, balances) | Reconciliation (§9, §19) depends on them | Authenticated WS subscribe test in shadow mode | Missing stream ⇒ venue fails live qualification |
| Disconnect-cancel-all availability *for this account/key* | §30/§32 recovery posture depends on whether it exists | Authenticated activation test in demo env where offered | Absent ⇒ compensating cancel-on-reconnect logic must be verified instead |
| Demo/test environment access | §39 integration tests need it | Login + authenticated call against the demo env | Absent ⇒ document and shift tests to shadow mode |
| Jurisdiction / account eligibility | §3 live activation item 3; no geo-circumvention ever | Operator attestation + venue account status page | Ineligible ⇒ venue excluded entirely |
| Sub-account structure and transfer entitlements | Transfers must be impossible from trading keys (§3, §23) | Key-info + account export | Transfer-capable ⇒ reject key |
| Clock-sync requirements met | Signed requests fail or, worse, straddle recv windows | `fetch_server_time()` / `synchronize_clock()` (§9) evidence | Skew beyond venue rule ⇒ venue unusable |

One key pair per venue per environment; paper and live must never share
credentials (README checklist). Keys live only in environment variables /
`.env` (gitignored) — never in code, config, logs, or this register: the
register records *permissions and tiers*, never secrets (§38).

## 7. Fail-safe rule for missing entitlements

Per §8 — "fail safely when an expected entitlement is unavailable" — later
phases must implement lookup semantics, not assumptions:

1. Every feature that depends on a venue capability must query this register
   at startup and on register change; there is no default-true.
2. A dependent feature whose entitlement is not `ACCOUNT_VERIFIED` (or
   `DOC_NOTED` for public-data-only features, which authenticate nothing)
   must start **disabled**, with a structured log line and a metric naming
   the missing entitlement.
3. If the missing entitlement is safety-relevant (private streams,
   cancel-all, key-permission inspection itself), the venue is excluded from
   the mode being started — the process may still run in a lower mode
   (observation) if that mode's requirements are met.
4. An entitlement discovered to be `UNAVAILABLE` or `EXPIRED` **while
   running** must degrade the venue the same way a §30 exchange kill switch
   does: block new cycles on that venue, preserve recovery capability,
   record the cause, require authorized restart.
5. Never substitute a published-docs value for a failed account verification,
   and never retry verification in a tight loop against venue rate limits.

## 8. Current truth (as of 2026-07-11)

- **No exchange accounts are connected. No API keys exist, and no code path
  reads one** — `arb/execution/` is an empty package by design; the only
  environment access is the documented `trust_env=True` proxy exception in
  the Binance REST snapshot client (see README "Known limitations").
- The code touches exactly four public WS endpoints and four public REST
  endpoints (§5.1 above); all are unauthenticated.
- **Every cell of the §5 matrix is `UNVERIFIED`.** The §5.1 notes are
  code/doc observations only, explicitly *unverified against an account*.
- No entitlement-register storage, no key-permission inspector, and no
  rate-limit loader exist in code. They are specified here for the phase
  that first connects an account (Milestone 3 / README Phase 2+ key
  provisioning), and are prerequisites of §3 live-activation items 4–6.
