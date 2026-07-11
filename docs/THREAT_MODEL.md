# Threat Model

> **Status: SPECIFIED — NOT IMPLEMENTED (analysis current for Phase 1).**
> The threat inventory and planned mitigations below are specification per
> CLAUDE.md §38. What exists in code today is only the Phase-1 observer —
> public WebSocket/REST ingestion, local L2 books, spread detector, JSONL
> research log, Prometheus metrics, terminal dashboard — so most listed
> threats have **no current exposure** (the targeted asset or capability does
> not exist yet). The "Phase-1 exposure" column is the honest statement of
> today's attack surface; mitigations are implemented only where marked.

Sources: CLAUDE.md §38 (threat list), §3 (safety defaults), §31
(idempotency), §11 (book integrity), §29–30 (limits, kill switches).
Companion document: `docs/SECURITY_MODEL.md`.

---

## 1. Assets

| ID | Asset | Exists in Phase 1? |
|---|---|---|
| A1 | Exchange API credentials | **No** — none are read or stored |
| A2 | Pre-funded exchange inventory (capital) | **No** — no accounts connected |
| A3 | Market-data integrity (local books → decisions) | Yes |
| A4 | Research data (JSONL opportunity/spread records) | Yes |
| A5 | Operational telemetry (metrics endpoint, logs, dashboard) | Yes |
| A6 | Codebase and dependency chain | Yes |
| A7 | Configuration (`config/config.yaml`) | Yes |
| A8 | Audit journal / accounting records | No — Phase 2 deliverable |
| A9 | Live-trading activation control | No — no live mode exists |

## 2. Phase-1 attack surface (accurate as of today)

1. **Public-data ingestion** — four venue WebSocket clients plus the Binance
   REST snapshot endpoint. Input is untrusted: malformed frames, wrong
   sequence numbers, forged checksums, absurd prices are all possible inputs.
2. **JSONL research logs** — `data/opportunities-YYYYMMDD.jsonl`, plaintext,
   append-only by behavior, unencrypted, no integrity protection.
3. **Metrics port** — `Metrics.start_server()` calls prometheus_client's
   `start_http_server(port)` without an address argument, so it binds
   **all interfaces** (default `0.0.0.0`), port 9109 by config. Unauthenticated.
4. **Dependency supply chain** — `websockets`, `aiohttp`, `orjson`,
   `pydantic`, `PyYAML`, `sortedcontainers`, `rich`, `prometheus-client`,
   `uvloop` (+ `pytest`, `pytest-asyncio` in dev). Lower-bounded ranges, no
   lockfile, no hash pinning.
5. **Environment/proxy path** — the Binance snapshot client uses
   `trust_env=True`; aiohttp will honor proxy variables and consult
   `~/.netrc` for `api.binance.com`. No other environment reads exist.
6. **Future credential introduction** — the largest *forward* risk: Phase 2+
   adds A1/A2 to this surface. `.env.example` pre-commits the provisioning
   rules (trade-only, no withdrawals, IP-whitelisted, per-env key pairs).

There is no order path, no private stream, no database, no web control
surface, and no admin API in Phase 1.

### Trust boundaries (Phase 1)

```text
[Exchanges: Binance / Bybit / OKX / Bitget]          UNTRUSTED input
        │  TLS WebSocket + REST (Binance snapshot via optional proxy)
        ▼
[Observer process: feeds → books → detector]          Trusted computation;
        │                                             validates sequence /
        │                                             checksum / staleness
        ├──► [JSONL research log on local disk]       Trusted-at-rest only if
        │                                             the box is trusted
        ├──► [Prometheus /metrics on 0.0.0.0:9109]    EXPOSED, unauthenticated
        └──► [stderr/file logs, TTY dashboard]        Local only
```

Everything on the left of the first boundary is adversarial by assumption
(CLAUDE.md §11: never trade on a book that fails integrity checks). Inside
the process there is a single trust domain — one OS user, one process, no
IPC. The only outbound listener is the metrics port.

## 3. Threat table (§38 inventory)

Phases: **P1** = observer (today), **P2** = paper execution, **P3** = shadow
→ limited live, **P4** = hardening. "None (asset absent)" means the targeted
asset/capability does not exist in this build.

| ID | Threat (§38) | Asset | Vector | Phase-1 exposure | Planned mitigation | Phase |
|---|---|---|---|---|---|---|
| T-01 | API-key theft | A1, A2 | Exfiltration from disk, env, memory, backups | **None (asset absent)** — no keys are read; residual: operator stores keys early in `~/.netrc`, which `trust_env=True` would read (documented prohibition) | Encrypted credential storage, managed keys, rotation, least privilege, IP allowlisting at venue (SECURITY_MODEL §4.4) | P2+ |
| T-02 | Withdrawal-enabled credentials | A2 | Over-permissioned key allows fund exfiltration if stolen | **None (asset absent)** | Boot-time permission probe **rejects** withdrawal/transfer-enabled keys (§40 inv. 19); provisioning checklist in `.env.example` | P2+ |
| T-03 | Insider misuse | A1, A2, A9 | Operator/developer abuses access to keys, limits, or activation | None (nothing consequential to misuse) | MFA, admin step-up auth, least privilege, tamper-evident audit of limit/mode changes (§29, §38) | P3 |
| T-04 | Administrator takeover | A9 | Compromised admin account enables live mode or weakens limits | None (no admin surface exists) | Server-side activation gate (12 conditions), no frontend-only enablement, step-up auth, signed config | P3 |
| T-05 | Secret exposure in logs | A1 | Credentials leak into logs/metrics/traces/crash reports/fixtures | **None today** (no secrets in process); logs/metrics/JSONL must be re-audited when secrets first enter | Logger-layer redaction; CI secret scanning; §38 "never appear in" list enforced by review checklist | P2+ |
| T-06 | Database compromise | A8 | Tampering with orders/fills/accounting state | None (no database) | Encryption at rest, least-privilege DB roles, append-only journal with hash chaining, backups | P2+ |
| T-07 | Dependency compromise | A6 | Malicious release of a direct/transitive dependency executes in-process | **Real** — floating version ranges, no lockfile; any `pip install` resolves fresh | Pinned + hash-verified lockfile; dependency vulnerability scanning in CI; minimal dependency set | P1→P2 (lockfile is a near-term action) |
| T-08 | Supply-chain attacks (build/CI) | A6 | Compromised build tooling or CI injects code | Low — no CI pipeline exists yet; builds are local | Signed configuration releases, CI provenance, static analysis + secret scan gates (§38, §42 step 5) | P2+ |
| T-09 | Unauthorized live activation | A9, A2 | Flag flip, config edit, or UI action enables real orders | **None (structurally impossible)** — no order code exists to activate | 12-condition server-side gate; `--live` **AND** `ARB_CONFIRM_LIVE_TRADING` interlock; immutable activation record; §40 inv. 20 test evidence | P3 |
| T-10 | Manipulated market data | A3, A4 | MITM on feeds, or venue-side spoofed depth/painted books skewing detected "edge" | **Real but bounded** — worst case today is polluted research data and wrong viability conclusions; no money moves | TLS on all feeds (in place); venue integrity protocols (sequence chaining, CRC32/seqId checks — **implemented**); staleness gate (>500 ms ⇒ venue unusable — **implemented**); cross-venue sanity checks and confidence scoring (§17) before any execution trusts a book | P1 (partial) / P2 |
| T-11 | Replay attacks | A3 | Stale or repeated feed messages re-applied to books | Low — sequence-continuity rules reject stale/duplicate deltas (**implemented** per venue: Binance `U`/`u` chaining, Bybit `u` continuity, OKX `seqId`/`prevSeqId`, Bitget CRC32); books invalidate on gaps | Same rules under contract test; for private streams (P3+): signed/nonce-checked requests per venue auth schemes | P1 (largely covered) |
| T-12 | Duplicate orders | A2 | Retransmission after timeout, reconnect races, duplicate events | **None (asset absent)** — no orders exist in any form | Deterministic idempotent client order IDs; proof-of-non-execution before resubmit; timeout ≠ rejection (§31, §40 inv. 6/7/14/15) | P2 (design) / P3 (live) |
| T-13 | Privilege escalation | A9, A1 | Component or user gains rights beyond its role | None (single-process, single-role, no privileged operations) | Least privilege per component; market-data components receive no credentials; MFA + step-up on control surface | P2+ |
| T-14 | Configuration tampering | A7 | Edited YAML changes fees/thresholds to fabricate viability, or (later) weakens risk limits | **Low but real** — an attacker with box access could distort research conclusions (e.g. zero fee tiers). Strict schema validation rejects unknown keys (**implemented**); values are not signed | Signed configuration releases; risk-limit changes require audited authorization (§29); config versions recorded in journal | P2+ |
| T-15 | Audit-log tampering | A8, A4 | Editing history to conceal actions or losses | JSONL research log is plaintext and editable by anyone with box access — acceptable for research data, **not** for audit data; no audit journal exists yet | Append-only, hash-chained (tamper-evident) journal; backups; immutable activation records (§38, §34) | P2 |

## 4. Phase-1-specific threats (beyond the §38 list)

| ID | Threat | Vector | Current handling |
|---|---|---|---|
| P1-01 | Parser abuse / feed DoS | Malformed or oversized frames from a venue or MITM crash or wedge the observer | Frame-handling errors are counted (`arb_message_errors_total`) and books resync or invalidate; a wedged/stale book makes the venue unusable within 100 ms ticks (**implemented**). Availability loss = lost research data only. |
| P1-02 | Metrics-port information disclosure | Port 9109 binds all interfaces, unauthenticated; exposes symbols watched, spreads observed, venue health | Operator action today: firewall the port or bind to loopback. A near-term hardening item is an explicit bind-address config default of `127.0.0.1`. No secrets are present in metrics. |
| P1-03 | Research-data disclosure/tampering | JSONL files reveal strategy focus (symbols, clip sizes, observed edges) and can be edited to mislead viability decisions | README states the policy: treat the box as sensitive infrastructure from day one. Files are gitignored; encryption/integrity is deliberately deferred (research-grade data). |
| P1-04 | Proxy/`.netrc` trust | `trust_env=True` means a hostile proxy env var or `.netrc` entry can redirect or authenticate the Binance snapshot request | Documented exception (README, `arb/feeds/binance.py`): never keep exchange credentials in `.netrc`; TLS still validates the venue certificate. |
| P1-05 | Premature credential introduction | Operator provisions API keys before Phase 2 controls exist and leaves them on the box | `.env.example` rules (trade-only, withdrawals disabled, IP-whitelisted); no code reads them, but theft would still burn the keys — do not provision keys until an execution phase needs them. |

## 5. Explicit non-threats in Phase 1

For clarity, the following are **out of scope today because the capability
does not exist** (not because they are mitigated): loss of funds via this
system's orders (no orders), fund exfiltration via this system's keys (no
keys), accounting fraud in the trade journal (no journal), unauthorized live
trading (no live path). Each becomes in-scope at the phase noted in the
tables above, and this document must be revised **before** the phase that
introduces the asset — revisiting this file is a gate item for Milestone 2
review.

## 6. Review cadence

- Re-run this threat model at every phase boundary and whenever a new asset
  class (credentials, database, control surface, derivatives) is introduced
  (CLAUDE.md §46 pause points).
- The Adversarial Reviewer role (§41) must specifically attempt: overstated
  spreads, stale-data trading, duplicate orders, improper live enablement —
  the last two against the mitigations promised in T-09 and T-12.
- Near-term open items from this analysis: (1) pinned/hashed dependency
  lockfile (T-07); (2) metrics bind-address default of loopback (P1-02);
  (3) CI secret/dependency scanning before any credential-bearing phase
  (T-05, T-07).
