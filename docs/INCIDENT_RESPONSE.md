# Incident Response

> **Status: PARTIALLY IMPLEMENTED — Phase-1 incident classes only.**
> Today the system is a read-only market-data observer: the only incidents
> that can actually occur are feed-integrity, venue-connectivity and
> observability failures (IC-1, IC-2, IC-7), and the engineering-incident
> procedure (§6) applies to development work now. Residual exposure,
> accounting, kill-switch and credential-security incidents (IC-3–IC-6)
> are **specified for Phase 2+** from CLAUDE.md §21, §30, §32, §38, §43 —
> no code for them exists and no capital is at risk in this build.

---

## 1. Scope and phase applicability

| ID | Incident class | Source spec | Can occur today? |
|---|---|---|---|
| IC-1 | Feed / order-book integrity | §11, §32 | **Yes** |
| IC-2 | Venue outage / connectivity | §32 | **Yes** (public feeds only) |
| IC-3 | Residual exposure / one-leg fill | §20, §21 | No — no orders exist (Phase 2+ spec) |
| IC-4 | Accounting / reconciliation mismatch | §34 | No — no accounting engine (Phase 2+ spec) |
| IC-5 | Security event | §38 | Partially — host/data hygiene only; no credentials exist yet |
| IC-6 | Kill-switch activation | §30 | No — no kill switches exist (Phase 2 spec) |
| IC-7 | Observability failure (metrics/log/research-log loss) | §37 | **Yes** |

## 2. Severity ladder

Aligned with the CLAUDE.md §42 priority order. Severity is assigned at
detection and may only be **raised** without approval; lowering requires the
incident owner's sign-off in the record.

| Severity | Definition | Phase-1 examples | Phase-2+ examples (spec) |
|---|---|---|---|
| SEV-1 | Capital, credentials or correctness of money-handling at risk (§42 P0) | none possible (no capital/keys) | credential exposure, duplicate-order possibility, uncontrolled residual exposure, accounting defect, stale-book trading, risk-engine bypass, unsafe live activation |
| SEV-2 | Data integrity or recovery capability compromised (§42 P1) | silent research-data loss (write errors climbing unnoticed), book accepted despite failed integrity check | adapter defect, reconciliation defect, failed invariant |
| SEV-3 | Degraded operation, collection continues | one venue unusable > 15 min, resync storm, feed task death | single-venue trading halt with clean state |
| SEV-4 | Cosmetic / no data impact | dashboard crash (by contract non-fatal), transient reconnects | operator-UI defects |

## 3. Universal response doctrine (CLAUDE.md §32)

During any uncertainty, in every phase, the order is fixed:

```text
Stop new cycles          (Phase 1: distrust affected signals/data)
Preserve hedge capability (Phase 1: n/a — no positions)
Reconcile                (Phase 1: verify books/logs/metrics agree)
Do not guess
```

Never guess an order state, a balance, or a book's validity. Phase-1
translation: never *interpret* research data collected during an unresolved
integrity incident as valid — mark the affected time window in the incident
record so analysis excludes it.

## 4. Per-class procedures

### IC-1 — Feed / order-book integrity (LIVE today)

The system self-handles the known cases (§11): sequence gap, checksum
mismatch, crossed book, buffer anomalies ⇒ book invalidated, resynced, and
excluded from signals until rebuilt; resubscribe guarded by a 10 s snapshot
deadline and a storm guard; 30 s idle watchdog force-closes half-open
sockets. An *incident* exists when self-handling is not converging:

1. **Detect:** `arb_book_resyncs_total` climbing steadily for one
   venue/symbol; repeated `resync storm` / `resync deadline expired` log
   warnings; `arb_venue_usable` flapping.
2. **Classify:** one symbol (likely instrument-specific), one venue (venue
   protocol or venue-side issue), all venues (local host/network — see IC-2).
3. **Contain:** the staleness gate already excludes bad books from signals.
   If a venue's data is suspect but *passing* checks (e.g. plausible but
   wrong prices), disable the venue in config and restart — a wrong-but-
   confident book is the dangerous case (§32: manipulated market data).
4. **Diagnose** using the §6 procedure (preserve payload excerpts from
   `message handling failed` logs; venue protocol changes are the most
   common root cause — check the venue's changelog before suspecting code).
5. **Record** the affected time window; exclude it from research analysis.

Phase 2+ (spec): the same detection must additionally trip the market-data
kill switch — an invalid book must never authorize a trade (invariant §40.10).

### IC-2 — Venue outage / connectivity (LIVE today, public side only)

Today: a venue outage degrades, never crashes — books go unusable, other
venues keep collecting; the feed reconnects with backoff forever.

1. Confirm scope: venue status page vs local egress (can you reach other
   venues? DNS? proxy?).
2. If venue-side: no action required; verify recovery
   (`venue <name> became usable`) and record the outage window.
3. If a feed **task died** (error log `feed task ... died`): that is a bug,
   not an outage — SEV-3, capture the traceback, restart the process, then
   run the §6 engineering procedure on the exception.
4. If local-side: fix egress; restart if config changes are needed.

Phase 2+ (spec, §32): the full matrix must be handled — private-feed
disconnect, order-entry disconnect, REST timeout, rate limits, auth
failures, key revocation, symbol suspension, price-band rejection,
duplicate order responses, unknown order state. During uncertainty: stop new
cycles, preserve hedge capability, reconcile, do not guess. Where available
and tested, exchange disconnect-cancel-all must be armed. A timeout must
never be treated as proof of rejection (§31): resubmission is allowed only
after client-order-ID search, private-stream inspection, REST order status
and balance reconciliation prove non-execution.

### IC-3 — Residual exposure / one-leg fill (SPEC — Phase 2+, §20–§21)

Cannot occur today. When execution exists, unmatched fills must trigger,
in order:

1. Freeze new cycles involving the affected asset.
2. Cancel safe-to-cancel open quantities; confirm cancellation.
3. Reconcile actual fills; compute exact residual
   (`net_base_delta`, `net_quote_delta`, `residual_exposure_value`).
4. Obtain fresh books from all eligible venues; pick the safest hedge route.
5. Validate the hedge with the risk engine; submit price-protected;
   reconcile; generate an incident record.

Hedge objective: **minimize risk-adjusted loss** — not rescue the originally
expected profit. Governing limits (config, Phase 2):
`MAX_RESIDUAL_EXPOSURE`, `MAX_RESIDUAL_DURATION_MS`,
`MAX_HEDGE_SLIPPAGE_BPS`, `MAX_HEDGE_LOSS`. If hedging cannot complete
inside those limits: trip the residual-exposure kill switch, preserve state,
escalate to a human (SEV-1). Residual incidents are always at least SEV-2
even when hedged cleanly.

### IC-4 — Accounting / reconciliation mismatch (SPEC — Phase 2+, §34)

Cannot occur today (the only accounting artifact is the append-only research
log). When paper/live accounting exists: any cycle that fails to reconcile
to exchange fills and balance changes is SEV-1 (§42 "P0 accounting defect").
Response: halt new cycles (reconciliation kill switch), never overwrite
journal entries — corrections are new compensating entries, identify whether
the defect could mislabel theoretical profit as realized (§34 categories
must never mix), and block further trading until the books balance.

### IC-5 — Security event (§38; today: hygiene only)

Today no credentials exist, so the credential-theft class is impossible. The
live surface is: the host, the unencrypted append-only JSONL research data,
logs, and the metrics port. Treat the box as sensitive infrastructure from
day one. If the host is suspected compromised: isolate it, preserve logs and
`data/` for forensics, rebuild before rerunning.

Phase 2+ (spec): threat classes include API-key theft, withdrawal-enabled
credentials, unauthorized live activation, configuration tampering,
audit-log tampering, dependency compromise. Any suspected credential
exposure is SEV-1: revoke the key at the venue **first**, trip the
security-event kill switch, then investigate. No private credential may ever
appear in logs, metrics, traces, crash reports or test fixtures — a
credential found in any of those is itself a SEV-1 incident even if unexploited.

### IC-6 — Kill-switch activation (SPEC — Phase 2, §30)

No kill switches exist yet (the staleness monitor's `on_unusable` hook is
the designed seam). When they exist, every activation is an incident (≥
SEV-3, or the severity of its cause). The switch itself must: block new
cycles, preserve emergency hedging and recovery capability, cancel
appropriate open orders, reconcile, record the cause, notify operators.
Operator duties after a trip:

1. Do **not** restart first. Read the recorded cause.
2. Verify reconciliation completed (no unknown order states, no residual).
3. Fix or explicitly accept the root cause.
4. Restart only through the authorized-restart path (identity + reason
   recorded). Recovery/hedging must never be disabled merely because normal
   trading is stopped.

### IC-7 — Observability failure (LIVE today)

Losing eyes is an incident in its own right: the process is *designed* to
die at boot if the metrics port can't bind, and to keep running but count
research-log write failures (`arb_record_write_errors_total`).

- Any increase in `arb_record_write_errors_total` or
  `arb_recording_errors_total`: SEV-2 (silent data loss). Check disk space
  and permissions on `data_dir`; the window with failed writes is lost —
  record it.
- Prometheus scrape failing while the process runs: SEV-3; you are blind to
  trends. Fix the scraper/network; the process itself is unaffected.
- Dashboard crash: SEV-4 by contract (logged, collection continues).

## 5. Escalation and manual intervention

- **Today** there is a single operator; "escalation" means: stop trusting the
  affected data, snapshot logs/metrics, open an incident record.
- **Phase 2+ rules (spec):** any transition into `MANUAL_INTERVENTION`
  (execution state machine, §19), any hedge failure inside limits breach
  (§21), any kill-switch trip, and any SEV-1 requires immediate operator
  notification. Automatic risk-limit increases are forbidden (§29); a human
  with recorded identity must authorize every limit change and every
  restart after a trip. Live activation may never result from incident
  handling — it always requires the full §3/§46 authorization chain.

## 6. Engineering-incident procedure (CLAUDE.md §43 — failed-attempt policy)

This applies **now** to every defect and failed change, and later to every
production incident's diagnosis phase:

1. Preserve the exact error (traceback, payload excerpt, config, commit).
2. Classify the failure.
3. Form a root-cause hypothesis.
4. Design the **smallest** diagnostic test that discriminates the hypothesis.
5. Run the diagnostic.
6. Apply **one** targeted correction.
7. Rerun focused tests.
8. Rerun regressions (`pytest -q` at minimum).
9. Document the outcome (PROGRESS.md iteration log + incident record).

**Three-strikes rule:** after three materially similar failed attempts, stop
repeating the method, record all attempts, mark the item blocked, identify
the missing information, and continue with another safe task.

**Never, in any incident or fix (§43):** delete valid tests to get a passing
build; increase slippage tolerances to disguise weak execution; remove fees
from calculations; ignore partial fills; disable risk controls; treat
timeouts as rejections; remove a correctness check without an equivalent
protection.

## 7. Post-incident record requirements

Every incident ≥ SEV-3 gets a record (a file under `docs/incidents/` or the
team's tracker; today, a dated entry appended via PROGRESS.md is acceptable).
Required fields:

| Field | Content |
|---|---|
| ID / date / severity | Stable ID, UTC timestamps, initial and final severity |
| Class | IC-1 … IC-7 |
| Detection | How it was noticed (metric, log line, human) and detection latency |
| Timeline | Ordered UTC events: onset, detection, containment, resolution |
| Impact | Phase 1: affected data window to exclude from research; Phase 2+: capital, exposure, fees, hedge loss |
| Root cause | Evidence-backed; "unknown" is acceptable, a guess is not |
| Actions | What was done, by whom; every §43 attempt logged |
| Follow-ups | Tests/monitoring/limits added so recurrence is detected or prevented |
| Sign-off | Who closed it; for future kill-switch trips: who authorized restart |

Records are append-only. Phase 2+ (spec, §38): incident records affecting
money handling become part of the tamper-evident audit trail, and recurring
classes must feed the chaos-test suite (§39) so the failure is reproduced
mechanically before the fix is trusted.
