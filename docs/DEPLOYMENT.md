# Deployment

> **Status: SPECIFIED — NOT IMPLEMENTED** (Phase-1 observer deploys trivially;
> everything else here is forward specification).
> What exists in code today is a single-process Python asyncio observer
> (`python -m arb observe`) that connects to four public WebSocket feeds and
> can run on any Linux/macOS host with outbound WSS — there is no Dockerfile,
> no orchestration, no multi-region code, and no execution engine to deploy.
> This document records the topology options and the measure-first rule from
> CLAUDE.md §14 (with the technology baseline from §6), the deployment
> decisions that must be made **before Phase 3 (limited live trading)**, and
> the region-splitting seam already marked in the Phase-1 code for Phase 4.

## 1. Governing rules

These rules from CLAUDE.md §14 and §3 bind every deployment decision:

1. **Measure first.** "Do not assume the best server region. Measure actual
   network latency to every permitted exchange endpoint." No topology or
   region may be selected on intuition or vendor marketing; selection requires
   recorded benchmark evidence per `docs/BENCHMARK_STRATEGY.md`.
2. **Benchmark all permitted options** (Architectures A, B, C below) before
   selecting a production topology.
3. **Jurisdiction and compliance.** "Do not deploy to prohibited jurisdictions
   or use infrastructure to circumvent exchange restrictions." This restates
   §3: no VPNs to bypass exchange rules, no circumvention of geographic
   restrictions or identity verification. A candidate region is only a
   candidate if the operator's accounts are legitimately entitled to trade
   from it.
4. **Human authorization** is required before deploying to production and
   before connecting real-money credentials (§46). Deployment automation must
   not be able to flip `LIVE_TRADING` — live activation requires the full §3
   checklist independently of where the system runs.

## 2. Current reality (Phase 1)

| Aspect | State today |
|---|---|
| Process model | One Python 3.11 asyncio process, `python -m arb observe` |
| Placement | Anywhere with outbound WSS to the four venues; no region requirement — Phase 1 measures opportunity *existence and lifetime*, not capture |
| Container | None. No Dockerfile exists; §6 recommends Docker + Linux and README Phase 4 scopes "Docker, multi-region notes" |
| State on disk | `data/` (JSONL research log, optional replay journal), `logs/` (headless mode) |
| Network listeners | Prometheus `/metrics` on localhost port from `config/config.yaml` (default 9109) |
| Credentials | None read, by design. No `.env` needed in Phase 1 |
| Known constraint | The development sandbox blocks egress to exchange endpoints; live feed behaviour must be validated from an operator machine (README "Known limitations") |

Latency context from the README (order-of-magnitude expectations, not
measurements of this system): a single non-colocated machine (e.g. North
America) should expect **150–300 ms+ round trips** to these venues' matching
engines, which cluster in AWS ap-northeast-1 (Tokyo), ap-southeast-1
(Singapore), and Hong Kong. In-region placement might see **10–35 ms**. This
build does not and cannot claim better; opportunities shorter than the round
trip are unexecutable, which is exactly what the research log's
persistence-duration field measures.

Consequence for Phase 1: run the observer wherever is convenient, but record
where it ran. Opportunity-lifetime data collected from a 250 ms vantage point
must be interpreted with that vantage point attached.

## 3. Topology options (CLAUDE.md §14)

All three must be benchmarked before a production topology is selected.

### Architecture A — single-region engine

One execution engine receives all feeds and submits all orders from one region.

| Pros | Cons |
|---|---|
| Simplest coordination and state management | One or more venues will have higher feed and order latency |
| Single risk engine, single journal, single clock domain | Region choice trades one venue's latency against another's |
| Restart/recovery is one process's problem | A regional outage takes down everything at once (which is at least fail-closed) |

### Architecture B — per-exchange edge agents

Each exchange has a nearby authorized execution agent; agents communicate over
a low-latency private network.

| Pros | Cons |
|---|---|
| Lowest venue-specific order latency | Distributed state; no true atomic dual-leg commit becomes even harder |
| Venue isolation: one venue's problems stay local | Coordination latency between agents enters the cycle |
| | Significantly more complex recovery and reconciliation |

### Architecture C — hybrid

Per-exchange market-data and order agents, one regional decision coordinator,
**local emergency hedging authority** at each edge, durable central audit
system.

| Pros | Cons |
|---|---|
| Edge order latency where it matters, central risk/accounting where it matters | Most moving parts of the three |
| Emergency hedging survives loss of the coordinator | Requires careful specification of what an edge may do autonomously |

Selection criterion: measured full-cycle latency and failure-containment
evidence, not architecture preference. Do not introduce networked components
into the hot path unless benchmarks justify the added latency and operational
complexity (§6).

## 4. The region-splitting seam (exists in code today)

Phase 1 deliberately marks the boundary at which Architecture B/C would split
the process. Grep for `REGION-SPLITTING SEAM`:

| Location | What the seam is |
|---|---|
| `arb/__init__.py` | Module docstring: everything upstream of the signal callback (feeds → books) is venue-local and would move to per-region processes (AWS Tokyo/Singapore/Hong Kong) |
| `arb/feeds/base.py` (`UpdateCallback`) | The `on_update(venue, symbol)` callback type is the boundary that would become an inter-region bus publish (feeds venue-local; signals central) |
| `arb/__main__.py` (feed wiring) | Feeds publish via a plain callback; in the multi-region design each feed process runs next to its venue and this callback becomes a bus publish consumed where the execution engine lives |

Rule for all Phase-2/3 work: keep this callback the *only* coupling between
feed-side and decision-side code, so that splitting along it in Phase 4 is a
transport change, not a rewrite.

## 5. Runtime baseline (recommended by §6)

For any deployment beyond a developer machine:

- **Linux** host; **Docker** container per process (to be added — none exists).
- Time synchronization configured and monitored (chrony/NTP); clock health is
  a risk-engine input in later phases (§28) and already caveats the Phase-1
  feed-latency metric (skew is included).
- Prometheus-compatible metrics scraped from every process (§6, §37);
  structured JSON logs shipped off-box.
- Append-only journals (research log, replay journal) on durable storage;
  treat the box as sensitive infrastructure from day one (README) — the JSONL
  data is unencrypted.
- No inbound exposure: `/metrics` and the dashboard are operator-local;
  nothing in any phase requires an internet-facing listener.
- For Phase 2+: secrets only via environment/`.env` (gitignored), trade-only
  IP-whitelisted keys per the README checklist and `docs/SECURITY_MODEL.md`.

## 6. Latency measurement procedure (prerequisite to topology selection)

Before Phase 3, from **each candidate region** (at minimum: Tokyo, Singapore,
and wherever the operator would otherwise default to):

1. Measure TCP+TLS+WebSocket establishment and steady-state feed latency to
   each venue's **actual permitted endpoints** (the URLs the adapters use, and
   the regional/institutional endpoints recorded in
   `docs/EXCHANGE_CAPABILITY_MATRIX.md` §8 discovery), not to ping targets.
2. Measure REST round-trip to the endpoints used for snapshots and (later)
   order entry.
3. Record p50/p95/p99/p99.9 per venue per region per
   `docs/BENCHMARK_STRATEGY.md`, with timestamps, region, provider, and
   instance type attached.
4. Re-run the Phase-1 observer from the leading candidate region long enough
   to compare opportunity-lifetime distributions against the non-colocated
   baseline — the decision input is "what fraction of observed opportunities
   outlive our measured round trip from here", not raw RTT.

Endpoint choice within a venue also matters (§8: regional endpoints,
VIP/institutional access) — enable only capabilities available to the actual
account, and never select an endpoint whose terms the account does not meet
(rule 3 of §1 above).

## 7. Decisions required before Phase 3 (limited live trading)

Each requires recorded evidence and, where marked, human authorization (§46):

| # | Decision | Evidence required | Human auth |
|---|---|---|---|
| 1 | Topology (A/B/C) | Comparative latency + failure-containment benchmarks from §6 above | — |
| 2 | Region(s) and provider | Per-region measurement records; jurisdiction eligibility confirmed for the operator's accounts | Yes (production deploy) |
| 3 | Venue endpoints per adapter | Capability/entitlement registry entries (§8), verified against the actual account | — |
| 4 | Host baseline | Time-sync configuration verified; monitoring/alerting reachable; journal storage durable | — |
| 5 | Key handling and IP allowlists | Trade-only, withdrawal-disabled keys; allowlist matches deployed hosts (README checklist, `docs/SECURITY_MODEL.md`) | Yes (real-money credentials) |
| 6 | Kill-switch and operator-alert path | Demonstrated from the deployed environment, deliberately test-fired first (README Phase 3) | — |
| 7 | Rollback/restart procedure | Restart-recovery evidence (§40.16) from the deployed environment | — |

Until all rows have evidence, the only thing this repository deploys is the
read-only observer, and the safety defaults of §3 (`MARKET_DATA_ONLY=true`,
`LIVE_TRADING=false`) remain in force.

## 8. Out of scope here

- Hot-path design constraints: `docs/HOT_PATH_ARCHITECTURE.md` (§13).
- Credential and secret architecture: `docs/SECURITY_MODEL.md`, `docs/THREAT_MODEL.md` (§38).
- Operational procedures and incident handling: `docs/OPERATIONS_RUNBOOK.md`, `docs/INCIDENT_RESPONSE.md`.
- What is measured and how baselines are kept: `docs/BENCHMARK_STRATEGY.md`.
