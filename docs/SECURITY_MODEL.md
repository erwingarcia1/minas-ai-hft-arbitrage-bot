# Security Model

> **Status: PARTIALLY IMPLEMENTED — Phase-1 structural safety only.**
> What exists in code today: the Phase-1 observer reads **no API credentials at
> all** (there is no code path that loads a key), `arb/execution/` is empty by
> design, and the only environment access is the documented `trust_env=True`
> proxy exception in the Binance snapshot client. Everything else in this
> document — the mode-flag machinery, credential vault, permission inspection,
> live-activation gate, audit chain, scanning pipeline — is **specification for
> Phase 2+ (paper/shadow) and the Phase-3 live gate**, per CLAUDE.md §3, §31
> and §38. Nothing here should be read as a description of running code unless
> explicitly marked "exists today".

Sources: CLAUDE.md §3 (non-negotiable trading safety), §38 (security
architecture), §31 (client order IDs and idempotency), §29 (risk-limit audit),
§46 (human authorization). Companion document: `docs/THREAT_MODEL.md`.

---

## 1. Safety defaults (§3)

The governing spec fixes the following default configuration. These flags are
the **required defaults of any execution-capable build**; each must default to
the safe value and be changeable only through the activation gate in §3 of
this document.

```text
MARKET_DATA_ONLY=true
PAPER_TRADING=true
SHADOW_TRADING=false
LIVE_TRADING=false

SPOT_TRADING_ONLY=true
MARGIN_TRADING=false
FUTURES_TRADING=false

WITHDRAWAL_PERMISSIONS_ALLOWED=false
AUTOMATIC_EXTERNAL_TRANSFERS=false
```

| Flag | Meaning when at default | Consequence of the default |
|---|---|---|
| `MARKET_DATA_ONLY=true` | Only public market-data connections are made. | No private streams, no order entry, no credentials needed. |
| `PAPER_TRADING=true` | When execution logic exists, fills are simulated (Mode 3). | Real exchanges never receive orders. |
| `SHADOW_TRADING=false` | Shadow mode (Mode 4: real balances read-only, hypothetical orders) is off until separately enabled. | No private read credentials are connected by default. |
| `LIVE_TRADING=false` | Live order submission is disabled. | Enabling requires the full 12-condition gate below. |
| `SPOT_TRADING_ONLY=true` | Only spot instruments may ever be traded. | Margin/derivatives adapters must refuse to initialize. |
| `MARGIN_TRADING=false` / `FUTURES_TRADING=false` | Leveraged products disabled (§25). | Cannot be enabled without separate spec, review and human authorization. |
| `WITHDRAWAL_PERMISSIONS_ALLOWED=false` | Credentials that carry withdrawal permission are **rejected**, not merely unused. | See §4 (credential handling). |
| `AUTOMATIC_EXTERNAL_TRANSFERS=false` | The system never moves funds between venues or off-exchange. | Rebalancing produces recommendations only (§23 of CLAUDE.md). |

**Exists today:** none of these flags exist as configuration keys, because
there is nothing to gate — Phase 1 is `MARKET_DATA_ONLY` **structurally**: no
order-construction, signing, or submission code exists anywhere in the
repository, so every flag's safe state holds by absence of capability, which
is stronger than a flag. When Phase 2 introduces execution scaffolding, these
flags must be introduced in the same change, defaulted as above, and covered
by tests proving the defaults (CLAUDE.md §40, invariant 20: "Live mode is
disabled by default").

### Live-trading interlock already documented for Phase 3

`.env.example` (exists today, no code reads it) documents the planned
dead-man interlock:

> Live trading will require BOTH the `--live` CLI flag AND this variable set
> to the exact string shown. Either one alone must refuse to trade live.
>
> `ARB_CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISKS`

This interlock is **in addition to**, not instead of, the activation gate
below.

---

## 2. Prohibited conduct (§3)

The system must never, in any mode or phase:

- circumvent geographic restrictions or identity verification, or use a VPN
  to bypass exchange rules;
- submit spoof orders, engage in wash trading, self-trading, quote stuffing,
  or price manipulation;
- exploit unauthorized endpoints;
- place orders for which inventory is unavailable;
- present theoretical profit as realized profit, or guarantee returns.

These are engineering constraints, not policy aspirations: §40 invariant 25
requires automated evidence that no strategy can engage in spoofing, wash
trading or self-trading.

---

## 3. Live-activation gate (§3)

Live trading activation requires **all twelve** conditions. No frontend
control may independently enable live trading; the decision is enforced
server-side.

| # | Condition | Evidence artifact (planned) |
|---|---|---|
| 1 | Server-side authorization | Signed activation record checked by the engine at boot and on mode change |
| 2 | Explicit operator approval | Operator identity + timestamp in the activation record |
| 3 | Valid jurisdiction eligibility | Documented eligibility review |
| 4 | Approved exchange accounts | Account-entitlement matrix (docs/EXCHANGE_ENTITLEMENTS.md) |
| 5 | Successful API-permission inspection | Boot-time permission probe result (see §4) |
| 6 | Successful market-data qualification | Mode-1 metrics vs thresholds (CLAUDE.md §44) |
| 7 | Successful paper-trading qualification | Mode-3 results vs thresholds |
| 8 | Successful shadow-trading qualification | Mode-4 results vs thresholds |
| 9 | Successful risk review | Risk Engineer sign-off record |
| 10 | Successful security review | Security Engineer sign-off record |
| 11 | Successful failure-recovery testing | Chaos/restart test results (CLAUDE.md §39) |
| 12 | Immutable activation audit record | Append-only, tamper-evident journal entry (§6 below) |

Additionally, §46 requires a human pause before: activating live trading,
connecting real-money credentials, deploying to production, increasing
capital limits, adding symbols or exchanges, enabling maker-taker or
derivatives, enabling automatic external transfers, or weakening risk
controls.

**Exists today:** nothing — there is no live mode to gate. This table is the
acceptance contract for the Phase-3 gate implementation.

---

## 4. Credential handling (§38, §3)

### 4.1 Current state (exists today)

Phase 1 reads **no credentials**. Specifically:

- No code loads `.env`; `.env.example` exists purely as forward documentation
  and every credential line in it is commented out.
- The one deliberate environment read: the Binance REST snapshot client uses
  `aiohttp` with `trust_env=True` so the observer works behind HTTP(S)
  proxies. aiohttp will then also consult `~/.netrc` for `api.binance.com` —
  operators must **not** keep exchange credentials in `.netrc` (documented in
  README and `arb/feeds/binance.py`). Nothing else reads the environment.

### 4.2 Key-provisioning rules (Phase 2+; documented now)

`.env.example` and the README fix the provisioning checklist that applies the
moment keys are first created:

- **Trade-only** permission. Withdrawals DISABLED. Internal/sub-account
  transfers DISABLED.
- **IP-whitelisted** to the machine that runs this system.
- Scoped to **spot only** — no margin/derivatives permissions.
- **One key pair per venue per environment** (paper vs live never share keys).
- Stored only in `.env` (gitignored) / environment variables — never in code
  or config.
- Inventory pre-positioned per venue; the system never moves funds between
  venues (no transfer code exists by policy).

### 4.3 Rejection of withdrawal-enabled keys (§3, §40 invariant 19)

The system must not merely avoid using withdrawal permission — it must
**reject the credential**. On first use of any key (boot, reconnect, key
rotation), the engine must call the venue's key/permission introspection
endpoint and refuse to proceed if the key grants withdrawal or transfer
rights. This check must be covered by contract tests per adapter, and its
result is condition 5 of the activation gate.

### 4.4 Storage and rotation (§38)

Planned controls, in dependency order:

1. Application-level credential encryption at rest (managed encryption keys).
2. Key rotation procedure with per-venue rotation without downtime where the
   venue supports parallel keys.
3. IP allowlisting at the venue for every key, where supported.
4. Least privilege: each component receives only the credentials and scopes
   it requires; market-data components receive none.

### 4.5 Where credentials must never appear (§38)

No private API credential may appear in:

```text
logs            metrics          traces
browser responses               crash reports
support exports                 test fixtures
source control
```

Enforcement (planned): structured-logging field redaction at the logger
layer, secret scanning in CI over source and fixtures (§7 below), and a
review-checklist item on every diff (CLAUDE.md §42 step 9 already lists
"secret exposure"). **Exists today:** trivially satisfied — there are no
credentials in the process — but the JSONL research log and Prometheus
endpoint must be re-audited against this list when private data first enters
the process in Phase 2/3.

---

## 5. Access control (§38)

All planned; none exists today because there is no privileged surface yet.

| Control | Requirement |
|---|---|
| Least privilege | Every principal (component, operator, admin) gets minimum necessary rights; the trading key must not hold withdrawal rights (§4.3). |
| MFA | Required for operator/administrator access to any control surface. |
| Administrator step-up authentication | Consequential actions (mode changes, limit changes, restarts after kill switch) require re-authentication. |
| IP allowlisting | Applied at the exchanges for API keys, and to any operational control plane, where supported. |
| Signed configuration releases | Production configuration changes are signed; the engine verifies signatures before applying. |

---

## 6. Audit records and tamper evidence (§38, §29)

- The journal (CLAUDE.md §34) is **append-only**; security-relevant entries
  (activation records, risk-limit changes, kill-switch events, administrative
  changes) must be **tamper-evident** — e.g. hash-chained so that
  modification or deletion of a past record is detectable.
- Risk-limit changes require: authorization, before-and-after values, reason,
  identity, timestamp, audit record (§29). §40 invariant 24: risk limits
  cannot change without an audit record.
- Live activation writes an **immutable activation audit record** (gate
  condition 12).
- Backup and recovery of audit data is a required control (§38).

**Exists today:** the JSONL research log (`data/opportunities-YYYYMMDD.jsonl`)
is append-only in behavior but **not** tamper-evident, and it contains
research data only (no credentials, no orders). It is not the audit journal;
the audit journal is a Phase-2 deliverable.

---

## 7. Scanning and supply chain (§38)

Planned CI controls (none wired up today):

- **Secret scanning** over source, history, and test fixtures.
- **Dependency scanning** (known-vulnerability audit) for the Python
  dependency set; today `pyproject.toml` uses lower-bounded version ranges
  with no lockfile — introducing a pinned/hashed lockfile is a prerequisite
  for meaningful supply-chain guarantees (see THREAT_MODEL.md T-07/T-08).
- **Static analysis** on every iteration (CLAUDE.md §42 step 5 lists security
  scans among broad regression checks).

---

## 8. Idempotency as a security control (§31)

Duplicate-order prevention is a financial-integrity control (§40 invariants
6, 14, 15). When execution exists:

- Client order IDs are deterministic and encode: system ID, exchange, cycle
  ID, leg, attempt, strategy version — within each venue's charset/length
  limits.
- After an uncertain response, resubmission is forbidden until non-execution
  is **proven** via: client-order-ID search, private order events, execution
  events, REST order status, and balance reconciliation. A timeout is never
  proof of rejection.

**Exists today:** not applicable — no orders exist in any form.

---

## 9. Phase mapping

| Control area | Phase 1 (today) | Phase 2 (paper) | Phase 3+ (shadow → limited live) |
|---|---|---|---|
| Mode flags (§1) | Structural (no capability) | Implemented with safe defaults + tests | Enforced by server-side gate |
| Credentials | None read | Still none for paper; vault + rejection logic built and tested | Read-only keys (shadow), then trade-only keys (live) after permission probe |
| Activation gate | N/A | Gate machinery built, cannot pass (by design) | All 12 conditions evidenced |
| Audit journal | Research JSONL only | Append-only journal + hash chaining | Immutable activation records |
| Scanning | Not wired | CI: secret, dependency, static analysis | Same, plus release signing |
| MFA / step-up / allowlisting | N/A (no control surface) | Specified with dashboard work | Required before live |
