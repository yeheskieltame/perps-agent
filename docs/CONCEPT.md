# Perps Agent — Concept & Judging Strategy

> **Perps Agent** — X: [@perpsagent](https://x.com/perpsagent). A self-improving,
> verifiable grid-trading agent. It **executes on Bybit**, and **thinks, learns, and
> proves on Mantle**.
>
> Target: Mantle **Turing Test Hackathon 2026 — Phase 2 "AI Awakening"**
> ($100K pool, Demo Day 2–3 Jul 2026). Primary track: **AI · Trading & Strategy
> (sponsor: BGA)** — the only track that names the Bybit API.
> Scope locked: **L1 + L2** (on-chain verifiable strategy memory + non-custodial
> vault/fee settlement) **plus a thin on-chain grid on a Mantle DEX (spot)** so
> execution is literally end-to-end on Mantle. On-chain *perp* execution is roadmap.

---

## 1. One-paragraph thesis

CEX trading bots are black boxes: retail users cannot verify a bot's real track
record, and the bot itself throws away the one asset that compounds — its own
decision/outcome history. Perps Agent turns that history into a **tamper-proof,
public on-chain memory on Mantle**. Every grid episode commits its strategy
*before* trading and attests its *verified outcome* after. That same on-chain
record is read back as the agent's **experience buffer**: the agent recalls which
grid parameters worked in similar market regimes — across its own runs *and the
whole public population of agents* — and improves its winrate over time. The
on-chain layer is therefore not a passive audit log; it is the agent's **analysis
substrate and strategy brain**. Verifiability is a *byproduct* of a system whose
primary job is to learn in the open. It ships as a **production-grade,
multi-tenant platform** (the deltaperps pattern) monetized **natively on-chain**.

This is exactly the BGA ethos — *reward better systems, not the highest PnL;
reduce information asymmetry between retail and institutions* — and it sits
directly on the Bybit ⇄ Mantle **CeDeFi** narrative (Bybit incubated both Mantle
and Byreal; Mantle Super Portal already bridges CEX and on-chain liquidity).

---

## 2. The problem (why this deserves to exist)

1. **Unverifiable track records.** Screenshots and dashboards are trivially
   faked. There is no neutral way for a retail user to trust a bot's claimed PnL.
2. **Information asymmetry.** Institutions have research, execution edge, and
   shared knowledge. Retail traders run static bots in isolation, blind to what
   actually works.
3. **Static grids are fragile.** A fixed grid farms a ranging market and gets run
   over in a trend (one-sided fills + adverse position). Most retail grid bots
   never adapt range/leverage to the regime.
4. **Bots don't learn.** Every bot discards its experience. There is no
   compounding institutional memory, and certainly no *shared* one.

Perps Agent attacks all four with one mechanism: a verifiable, learnable, on-chain
strategy memory feeding an autonomous agent that executes on deep CEX liquidity.

---

## 3. The product: the Verifiable Learning Loop

The core of Perps Agent is a six-step closed loop. Mantle is the hinge (steps 2, 3,
5) — used for **analysis**, **strategy**, and **verification** at once.

```
        ┌──────────────────────── MANTLE (on-chain brain) ───────────────────────┐
        │  StrategyMemory  ·  StrategyLedger (commitments + attestations)  ·  Vault │
        └─────────▲───────────────────────▲────────────────────────▲──────────────┘
                  │ (2) RECALL             │ (3) COMMIT             │ (5) ATTEST + LEARN
                  │                        │                        │
   (1) SENSE ─────┴──> (regime classify) ─┴──> (3) DECIDE params ──┴──> (4) EXECUTE on Bybit
   Surf microstructure                        contextual policy           maker-only grid
   + Elfa real-time signals                   + risk caps                 (deltaperps engine)
   + Nansen smart-money                                                          │
   + on-chain memory ◄──────────────────── (6) LEARN: policy updates ◄───────────┘
```

1. **SENSE (analysis).** Fuse four data sources into a *regime fingerprint*:
   **Surf.AI** market & exchange microstructure (realized vol, funding, RSI, liquidations); **Elfa.ai** real-time
   social/news/price triggers (early regime-shift warning); **Nansen**
   smart-money net flows (macro risk-on/off, incl. Mantle-native flows); and the
   **on-chain memory** itself.
2. **RECALL (on-chain analysis).** Query `StrategyMemory` on Mantle for the
   *k* nearest historical regimes and the parameter sets that produced the best
   risk-adjusted, *verified* outcomes — from this agent and from the public
   population.
3. **DECIDE + COMMIT (strategy).** A contextual policy chooses the grid config
   (range, level count, spacing, leverage, rebalance/exit rules) and risk caps,
   then **commits the config hash + rationale on-chain *before* trading**. This
   pre-commitment is what makes the track record trustless: params cannot be
   retrofitted to results.
4. **EXECUTE.** Run a maker-only geometric grid on Bybit using the engine
   adapted from `deltaperps` (post-only, per-instance externalId recovery,
   WS-driven fills, circuit breakers).
5. **ATTEST + LEARN (verification = training data).** On each epoch/close, write
   one structured record on-chain: `regime fingerprint → params → verified
   outcome` (equity, realized PnL, **winrate**, max adverse excursion, fill
   efficiency, Merkle root of fills). This single write *is* both the audit proof
   and the next training example.
6. **LEARN.** The policy updates from the growing on-chain experience buffer;
   subsequent decisions improve. Because the buffer is public, the agent gains a
   population-level prior, not just self-history.

**The killer demo (Human vs AI + "learning from history"):** an ablation —
*static grid / baseline* vs *Perps Agent with on-chain memory* over the same market
window — showing measurably higher winrate and lower drawdown. This proves the
on-chain layer is load-bearing, not decoration.

---

## 4. Why the on-chain role is strong in *strategy* (not just verify)

This is the design point that elevates the project above an "API wrapper + audit
log." The same `StrategyMemory` artifact does three jobs:

| Job | How Mantle is used | Judging line it serves |
|-----|--------------------|------------------------|
| **Analysis** | Agent reads memory as a first-class signal in regime classification + parameter recall (k-NN over verified episodes) | BGA Innovation & technical depth; Part A Technical/Innovation |
| **Strategy / learning** | Memory is the experience-replay buffer for a contextual policy; winrate compounds as records accumulate; population-level learning across public agents | BGA Strategy design; Part A Innovation; "AI Awakening" learning theme |
| **Verification** | Pre-commitment + attestation make every claim auditable; verifier dApp recomputes equity from chain | BGA Transparency & verifiability (maxed); BGA ethos |

**Bootstrapping at low data (honest engineering).** A hackathon won't generate
thousands of live episodes. We seed `StrategyMemory` with a backtest corpus
(6–12 months of historical Bybit data across regimes), each committed as a memory
record flagged `backtest`. Live/paper episodes are flagged `live`. The policy is
a **contextual bandit / k-NN with priors** — meaningful at small N, explainable
(good for the "explainable, defensible strategy" rubric), and upgradeable to RL
later. We are explicit about train/test discipline to address the BGA
**overfitting** criterion head-on.

**Objective = better system, not max PnL.** The learning reward is *risk-adjusted*
(Sortino / PnL-per-drawdown, consistency, time-in-range), never raw PnL; memory
recall ranks past episodes the same way. This bakes the BGA mandate (*"reward
better systems, not the highest PnL"*) into the optimization target itself, not
just the marketing.

---

## 5. Architecture

Reuses the proven `deltaperps` hexagonal design (pure `domain/` core, ports,
adapter registry, `GridService` facade, recovery/reconciliation, **multi-tenant**:
one service, many users, each on their own account). The seam rule carries over:
**the bot/UI never imports the engine, adapters, or any exchange/chain SDK** — it
talks to a typed facade only.

```
perps-agent/
├── contracts/                  # Mantle (Solidity / Foundry)
│   ├── StrategyLedger.sol      # L1: commit(configHash) + attest(outcome, merkleRoot)
│   ├── StrategyMemory.sol      # L1: structured regime→params→outcome records + queries
│   └── Vault.sol               # L2: deposits, performance bond, on-chain fee settlement
├── backend/                    # Python engine (adapted from deltaperps)
│   └── src/perpsagent/
│       ├── domain/             # pure: models · levels · grid · pnl · regime · safety · ports
│       ├── adapters/
│       │   ├── exchanges/      # ← drop-in venues, all behind one ExchangePort
│       │   │   ├── registry.py #   Venue → ExchangePort factory (add a venue here)
│       │   │   ├── bybit/      #   CEX perps (Bybit v5) — primary execution
│       │   │   ├── mantle_dex/ #   on-chain spot grid on a Mantle DEX (iZiSwap)
│       │   │   └── fake.py     #   in-memory ExchangePort (tests / dry-run)
│       │   ├── chain/          # Mantle contracts client: ledger · memory · vault (web3.py)
│       │   ├── signals/        # elfa · nansen clients (regime inputs)
│       │   ├── payments/       # x402 facilitator · vault fee settlement
│       │   └── store/          # sqlite → postgres
│       ├── agent/              # the Verifiable Learning Loop: sense·recall·decide·learn
│       ├── app/                # GridManager (1 task/grid) · GridService facade · kill-switch
│       └── config.py runner.py
├── frontend/
│   ├── telegram/               # aiogram bot — product UI (deploy/manage grids)
│   └── web/                    # public Verifier page: equity curve recomputed from chain
├── docs/  ·  plan/  ·  skills/
```

**One seam for all venues.** Every exchange implements the same `ExchangePort`
(place · cancel · stream fills · balance). Adding a venue = one folder under
`adapters/exchanges/` + a registry entry; the grid engine and agent never change.
This is the deltaperps adapter-registry pattern — Bybit, a Mantle DEX, or a future
venue are all drop-ins.

**Component map**

- **Execution venue — Bybit.** Bybit v5 USDT-perp API. Maker-only geometric
  grids, per-instance recovery, circuit breakers. Non-custodial: user supplies
  their own Bybit API keys; trading capital never leaves Bybit.
- **On-chain execution — Mantle DEX.** The same engine runs a spot grid on a
  Mantle DEX (default **iZiSwap**: deepest Mantle TVL + native on-chain limit
  orders that map 1:1 to grid levels; **Merchant Moe** Liquidity Book is the
  flagship alternative). Proves literal end-to-end execution on Mantle and gives
  real Mantle DeFi integration.
- **On-chain brain — Mantle.** `StrategyLedger` (commit/attest), `StrategyMemory`
  (the learnable buffer), `Vault` (L2). Low gas makes frequent attestation +
  memory writes economically viable — a concrete reason Mantle specifically.
- **AI agent.** The loop in §3. Decisions come from an explainable contextual policy over recalled on-chain experience; a deterministic per-decision rationale (`agent/reason.py`, optionally enriched by Surf's NL chat) is logged — no dependency on a general-purpose LLM.
- **Data fusion.** Elfa (real-time awareness), Nansen (on-chain intelligence),
  Bybit (microstructure), Mantle memory (verified experience).
- **Product UI.** Telegram bot to deploy/monitor/stop grids; web Verifier page
  anyone can use to audit a wallet's verified track record straight from chain.

**Credits → component mapping** (the LLM/agent budget)

| Credit | $ | Used for |
|--------|----|---------|
| **Elfa.ai** | $36K | Real-time social/mention momentum → `social_momentum`. **Claimed.** |
| **Surf.AI** | $30K | Market & exchange microstructure (realized vol, funding, RSI, liquidations) → regime fingerprint; optional NL chat for rationale. **Claimed.** |
| **Orbit AI** | $30K | Multi-agent orchestration + deployment. *Optional — not claimed.* |
| **Nansen** | $7K | Smart-money netflows (incl. Mantle) → `smart_money_flow`. **Claimed.** |
| **AltLLM / AltClaw** | $7K | Crypto-native agent runtime; x402 + ERC-8004. *Optional — not claimed.* |

---

## 6. Why Mantle specifically (defeats the "any chain" critique)

- **Low gas → the loop is affordable.** Frequent commitments, attestations, and
  memory writes are only viable on a low-fee L2. The economics of "write every
  episode on-chain" *require* Mantle-class fees.
- **CeDeFi narrative is the sponsors' own thesis.** Bybit incubated Mantle and
  Byreal; **Mantle Super Portal** already routes CEX liquidity to on-chain.
  Perps Agent's Bybit-execution + Mantle-trust barbell is the retail-facing
  embodiment of that exact strategy.
- **Asset integration.** Vault denominated in Mantle-native assets (mETH / USDe);
  fees + x402 settled in USDC/USDe on Mantle — real ecosystem complementarity,
  not a deployment target.

---

## 7. Business model & on-chain monetization

Perps Agent is built as a **production-grade, multi-tenant platform** (the deltaperps
pattern): one shared agent service, each user runs their own grids on their own
Bybit account (their keys) under their own on-chain identity (wallet / ERC-8004
Agent ID). This is what makes the "genuine user demand + sustainable revenue +
credible GTM" rubric real rather than aspirational — many people can run our
trading agent, and the platform earns on-chain.

**Principle: we charge for the *system*, not for PnL.** Taking a cut of profits
would make us a PnL-maximizer with an incentive to push user risk — the exact
opposite of the BGA mandate (*"reward better systems, not the highest PnL"*) — and
a fragile business that earns nothing in flat or losing markets (and grid bots do
lose in trends). So core revenue is priced on **usage** — transactions routed and
intelligence consumed — which is robust (works in any market) and on-ethos.

**Revenue streams (on-chain native — no subscription):**

1. **Builder fee per transaction (primary).** A small percentage of each trade we
   route for the user (the deltaperps builder-fee model) — **volume-based, not a
   profit cut** — accrued and settled on-chain in the Vault. We earn for providing
   execution + safety infrastructure even when a user breaks even or loses. Kept
   deliberately low (configurable, e.g. ≤0.5% and calibrated to a few bps for
   high-frequency grids) so it never eats the edge — on-ethos for BGA, not extractive.
2. **x402 pay-per-call (agent-native).** Perps Agent exposes its intelligence —
   regime classifier, signal feed, and **verified StrategyMemory queries** — as an
   HTTP API metered with **x402** (agents pay per request in USDC, settle on-chain,
   no accounts). The on-chain memory becomes a *two-sided asset*: our agent's
   brain, and a verifiable-alpha API others buy per call.

No subscriptions, no upfront lock-in: users pay only for transactions they make
and intelligence they consume. *Optional, deliberately minor:* a small **opt-in**
performance fee, capped and never the headline.

**The defensible wedge.** Because every record in StrategyMemory is
pre-committed and attested on-chain, the alpha we sell is **provably real** —
something an off-chain data vendor cannot offer. Selling cheap, per-call,
verifiable strategy intelligence to retail directly serves the BGA mandate of
reducing information asymmetry.

**GTM (credible, post-hackathon).**
- *Acquisition:* Telegram bot = near-zero friction for retail; the public
  Verifier page is a growth loop — every un-fakeable track record is marketing
  ("don't trust, verify").
- *Ecosystem:* ride the Bybit ⇄ Mantle ⇄ Byreal CeDeFi rails and Mantle Super
  Portal; list in the Mantle dApp directory.
- *Expansion:* open the x402 alpha API to the wider agent economy.

**Tokenomics (revenue-first, optional token).** No speculative token at launch —
builder fees + x402 revenue fund the platform. A *future* curation token could reward
agents that contribute high-quality verified records to the shared memory with a
share of x402 revenue, aligning token value to genuine contribution rather than
speculation (deliberately on-ethos for BGA).

> Note on x402: it is EVM/chain-agnostic (CAIP-2; today runs on Base, Ethereum,
> Arbitrum, Polygon, Solana, settling mostly in USDC). On Mantle we run it as an
> EVM L2 settling in USDC/USDe; if Mantle isn't on a public facilitator we
> self-host the facilitator.

---

## 8. Judging score map

Two scorecards, 100 pts total. Below: what we concretely do for **every** line.

### Part A — Mantle general (50 pts; all judges)

| Dimension | Pts | What Perps Agent does | Target |
|-----------|----:|--------------------|:------:|
| Technical | 15 | Trust + learning logic AND a live on-chain grid (Mantle DEX adapter) run end-to-end on Mantle; commit/attest/recall every episode; robust hexagonal engine | Good→Excellent |
| Ecosystem fit | 10 | Active Mantle DeFi integration (on-chain grid on iZiSwap/Merchant Moe); Vault in mETH/USDe; fees + x402 settled on Mantle; CeDeFi bridge with Bybit | Good→Excellent |
| Business potential | 10 | Multi-tenant platform; usage-priced on-chain revenue (builder fee per trade + x402 alpha API), robust in any market; retail PMF; Verifier-led GTM | Good→Excellent |
| Innovation | 10 | On-chain verifiable *experience replay* as a strategy brain — novel | Excellent |
| User experience | 5 | Telegram one-tap deploy; web Verifier; AA/gasless vault deposit | Good |

### Part B — BGA track-specific (50 pts; BGA judges)

| Dimension | Pts | What Perps Agent does | Target |
|-----------|----:|--------------------|:------:|
| Alignment with BGA ethos | 10 | Verifiable track records + shared public strategy memory + cheap x402 alpha reduce retail/institution info asymmetry; "better systems, not highest PnL" | Excellent |
| Innovation & technical depth | 10 | AI + on-chain experience replay; real Solidity + Python beyond a wrapper; meaningful Bybit API use | Excellent |
| Strategy design & risk management | 7.5 | Regime-adaptive grids; explicit overfitting discipline; position/leverage caps, circuit breakers, range-exit flatten | Good→Excellent |
| Transparency & verifiability | 7.5 | Pre-commitment + on-chain attestation + independent Verifier page; nothing is a black box | Excellent (maxed) |
| Real-world impact | 5 | Trust + access for retail; cheap verifiable alpha; bonus RWA-readiness (regime engine is asset-agnostic) | Good |
| User accessibility & UX | 5 | Non-institutional users deploy complex adaptive strategies via chat | Good |
| Execution & demo quality | 5 | Working MVP on Bybit testnet + Mantle testnet; live ablation demo | Good→Excellent |

**Where points are won:** Transparency (7.5) is maxed by construction; the
on-chain-memory-as-brain is the differentiated Innovation story on both
scorecards; the ablation demo evidences Strategy + Execution. **Where to defend:**
Part A Ecosystem Fit — closed by L2 Vault + x402 stablecoin settlement + CeDeFi
framing. Business Potential is now a strength via the multi-tenant + three-stream
model.

---

## 9. Scope & timeline (~1 month to Demo Day, 2–3 Jul)

Build order is dependency-first; **L1 ships before L2**. L1 alone is already a
competitive submission; L2 is upside.

- **Week 1 — Spine.** Bybit adapter (testnet) + port the deltaperps grid engine;
  `StrategyLedger` (commit/attest) on Mantle testnet; backtest harness to seed
  `StrategyMemory`.
- **Week 2 — Brain (L1 complete).** `StrategyMemory` contract + recall queries;
  agent loop (sense→recall→decide→commit→execute→attest); Elfa + Nansen signal
  clients; Telegram MVP.
- **Week 3 — Vault + on-chain grid + learning (L2).** `Vault` (deposit, bond,
  on-chain fee settlement) + x402 metering; **thin Mantle DEX (iZiSwap) grid
  adapter, one market** (literal on-chain execution); contextual policy with
  experience replay; public Verifier page.
- **Week 4 — Demo polish.** Ablation experiment (baseline vs memory-on);
  end-to-end run on testnet; deck, video, GitHub, deployed contract addresses.

**Team split** (mirrors deltaperps): Kiel — backend engine + agent + chain
adapter; Bima — Telegram + web Verifier; contracts — shared/whoever owns
Solidity. Defaults: **testnet always**; small green PRs referencing issues.

---

## 10. Demo Day plan

1. **Hook (Human vs AI).** Live: deploy a grid by chatting with the agent; it
   commits its plan on-chain *before* trading.
2. **The loop, visible.** Show regime classification pulling Elfa/Nansen, the
   recall from on-chain memory, and the resulting params.
3. **Proof.** Open the public Verifier page; recompute the equity curve straight
   from Mantle — independent of our backend.
4. **The payoff (ablation).** Side-by-side: static baseline vs memory-on agent on
   the same window — higher winrate, lower drawdown. "It learned, and you can
   prove it."

---

## 11. Risks & honest boundaries

- **Custody.** Trading capital stays on Bybit under the user's own keys
   (non-custodial to us). The Vault holds only bond + fees. We will *not* build an
   automated CEX deposit/withdraw bridge in a hackathon — stated plainly to judges.
- **Data volume / overfitting.** Backtest-seeded memory + contextual bandit with
   priors; explicit train/test split; we present limitations honestly rather than
   overclaim "the AI learned to print money."
- **On-chain execution = spot grid on a Mantle DEX** (iZiSwap / Merchant Moe) for
   literal end-to-end-on-Mantle execution + DeFi integration. On-chain *perp*
   execution stays deferred (no liquid Mantle-native perp; Byreal Perps is on
   Solana) — roadmap via the same adapter registry.
- **Real funds.** Demo on testnet; mainnet only after soak.

---

## 12. Open decisions / next steps

- ~~Confirm working name~~ → **Perps Agent** (X: @perpsagent). Locked.
- Choose Solidity toolchain (Foundry recommended) and confirm Mantle testnet
  params + which Mantle-native asset for the Vault (mETH vs USDe).
- Credits claimed: **Surf** (market microstructure), **Elfa** (social), **Nansen**
  (smart-money). Reasoning = explainable policy + optional Surf NL; AltLLM/Orbit optional.
- x402: use a public facilitator vs self-host on Mantle; settlement asset
  (USDC vs USDe).
- Token: revenue-only at launch (default) vs introduce a curation token later.
- Fee model: builder fee per transaction (primary, low %, e.g. ≤0.5%) + x402
  metering; no subscription; performance fee opt-in only (default off).
- Decide attestation cadence (per-fill batch vs hourly) given gas vs freshness.

> Next deliverables: pitch deck (Demo Day) + repo scaffold (contracts/be/fe).
