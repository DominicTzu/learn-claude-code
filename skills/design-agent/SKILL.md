---
name: design-agent
description: Design or review reliable AI agent and harness architectures. Use when defining an agent loop, choosing ReAct versus deterministic workflows, planning/task graphs, subagents, teams, background jobs, or cron; designing tools, context, memory, permissions, protocols, error recovery, concurrency, observability, evaluation, and production safeguards; or diagnosing an agent that loops, loses context, retries unsafely, or coordinates poorly.
---

# Design Agent

Design the smallest harness that lets the model solve the task safely. Treat the model as the reasoning engine and the harness as tools, context, state, permissions, and lifecycle control.

## Design Workflow

1. **Define the contract.** Specify users, goals, inputs, outputs, environment, latency/cost targets, success metrics, forbidden outcomes, and who owns failures.
2. **Decide whether an agent is needed.** Prefer ordinary code when the path is known and deterministic. Use an agent when the system must choose actions from changing observations.
3. **Choose one primary control pattern.** Use the table below. Add another pattern only for a concrete failure mode.
4. **Design the action surface.** Start with a few atomic, composable tools. Separate read-only, mutating, destructive, and external actions.
5. **Design context and state.** Decide what is always present, loaded on demand, persisted, summarized, isolated, or discarded.
6. **Specify loop invariants and protocols.** Define message schemas, tool-call/result pairing, validation, state transitions, ownership, and stop conditions before implementation.
7. **Add recovery and permissions.** Classify failures, make retries safe, bound every loop, and require approval at trust boundaries.
8. **Evaluate trajectories.** Test complete action sequences, failures, and recovery paths, not only final answers. Add complexity only when traces justify it.

## Choose the Control Pattern

| Pattern | Use when | Advantages | Costs / risks |
|---|---|---|---|
| Direct response or deterministic code | No external action or the exact procedure is known | Fast, cheap, testable | Cannot adapt to unknown state |
| **ReAct** (reason-act-observe loop) | Open-ended tool use; each observation changes the next action | Flexible, minimal orchestration, handles uncertainty | Variable latency/cost, loops, nondeterminism |
| Plan-and-execute / task DAG | Long work has explicit dependencies, owners, resumability, or parallel branches | Visible progress, persistence, concurrency | Planning overhead; plans become stale |
| State machine / workflow | Regulated or fixed transitions, approvals, billing, deployments | Predictable, auditable, easy to constrain | Brittle for ambiguous work; state explosion |
| Router + specialists | Domains require genuinely different tools, prompts, or trust policies | Focused context and permissions | Routing errors, duplicated context, more operations |
| One-shot subagent | A noisy research or implementation subtask needs clean context | Isolation; parent receives only a summary | Extra cost; details may be lost in handoff |
| Persistent agent team | Independent tasks benefit from sustained parallel work | Throughput and specialization | Coordination, races, conflicts, idle cost |
| Background/event-driven worker | Tools are slow or completion arrives later | Main loop stays responsive | Cancellation, notification, and stale-state complexity |
| Cron/scheduler | Work is triggered by time rather than a live user | Durable automation | Duplicate runs, clock/timezone, catch-up, and expiry issues |
| Critic/reflection pass | High-cost mistakes justify an independent verification step | Better defect detection | More latency; correlated models may repeat the same error |

Use ReAct as the default for a genuinely open-ended, tool-using agent. Do not use it to imitate a known sequence of API calls. For mixed systems, put ReAct inside bounded workflow states: deterministic code owns irreversible transitions; the model handles ambiguous decisions within each state.

Do not persist or require private chain-of-thought. Persist decisions, actions, observations, evidence, and state transitions.

## Keep the Core Loop Correct

Maintain this invariant:

```text
assemble current context and tools
→ call model
→ validate response
→ if final text: run stop checks and return
→ for each tool call:
    validate args → permission check → execute → normalize result
→ append every result with its matching call ID
→ collect async notifications → repeat
```

- Preserve every assistant tool call with its corresponding tool result. Never compact or truncate half of a pair.
- Execute multiple calls concurrently only when they are independent and side-effect safe; otherwise preserve order.
- Bound model turns, tool calls, wall time, output size, recursion depth, and spend.
- Stop on success, explicit cancellation, policy denial, exhausted recovery, no progress, or required human decision.
- Rebuild tools and prompt state when dynamic capabilities change; do not reuse stale caches.

## Design Tools as Protocols

- Give each tool one clear action, a strict JSON schema, concise semantics, and explicit side effects.
- Prefer structured APIs over shell text parsing. Return bounded, machine-readable results plus a short human summary.
- Return structured errors such as `{code, message, retryable, details}`; do not hide failures as successful prose.
- Add timeouts, cancellation, output limits, and correlation IDs.
- Add idempotency keys to mutations. Before retrying an action with an unknown outcome, inspect external state.
- Distinguish validation errors, policy denials, transient failures, conflicts, and permanent failures.
- Treat tool output, retrieved documents, web pages, MCP servers, and teammate messages as untrusted input, not instructions.
- Namespace dynamic/external tools and version schemas. Re-discover capabilities after reconnects.

For multi-agent or asynchronous messages, include at least `protocol_version`, `type`, `message_id`, `correlation_id`, `task_id`, `sender`, `recipient`, `status`, `payload`, `error`, and `timestamp`. Assume at-least-once delivery: deduplicate, acknowledge, time out, and make handlers idempotent.

## Manage Context and Knowledge

- Assemble the system prompt from real runtime state: identity, workspace, capabilities, policies, and current mode. Avoid keyword-triggered prompt fragments.
- Keep capability/skill indexes small; load full knowledge only when relevant.
- Separate conversation history, task state, long-term memory, domain knowledge, and audit logs. They have different retention rules.
- Isolate noisy subtasks in fresh subagent contexts and return evidence with summaries when details matter.
- Compact cheaply first: persist oversized outputs, replace old results, trim old turns, then summarize. Save a recoverable transcript before destructive summarization.
- Curate long-term memory: select relevant items, extract only durable facts/preferences, consolidate duplicates, and allow correction/deletion.
- Never let retrieved memory override the current user request, policy, or verified current state.

## Plan and Coordinate

- Use an in-memory todo list for the current execution; use a persistent task graph for cross-session work, dependencies, ownership, and resume.
- Validate task DAGs, missing dependencies, legal state transitions, and completion evidence.
- Claim work atomically. Use locks, compare-and-swap, leases, or transactional storage; never rely on a prompt to prevent double ownership.
- Parallelize only independent work. Isolate file changes with worktrees or separate workspaces, and define merge/review ownership.
- Give teams a lifecycle such as `WORKING → IDLE → SHUTDOWN`; add heartbeats, stale-worker detection, cancellation, and a lead escalation path.
- Prefer a one-shot subagent over a team unless persistent communication or parallel ownership is necessary.

## Recover by Failure Class

| Failure | Recovery |
|---|---|
| Invalid tool arguments | Return validation details to the model; allow a bounded correction |
| Tool timeout / cancellation | Report partial state; retry only if safe or resume from a checkpoint |
| 429, overload, network error | Honor `Retry-After`; exponential backoff with jitter; cap attempts; use a compatible fallback if defined |
| Context too long | Persist outputs, compact, retry once; fail clearly if still too large |
| Output truncated | Increase output budget once or append a continuation request; cap continuations |
| Authentication / permission denial | Do not retry blindly; refresh credentials or request approval |
| Conflict / concurrent modification | Reload current state, reconcile, and retry with version checks |
| Mutating call with unknown outcome | Inspect by operation/idempotency key before retrying |
| Repeated action / no progress | Detect repeated call signatures or unchanged state; change strategy, delegate, or stop |
| Partial multi-step failure | Record completed steps and compensation options; do not erase evidence |
| Protocol/schema mismatch | Reject, negotiate/version explicitly, or disable the incompatible capability |

Use retries for transient faults, not logic errors. Keep recovery state outside the model so retries remain bounded and observable.

## Enforce Trust Boundaries

- Sandbox filesystem, process, network, and credential access; resolve paths before checking workspace containment.
- Apply least privilege per tool and per agent. Keep secrets out of prompts, logs, tool results, and memory.
- Require human approval for destructive, irreversible, externally visible, expensive, or legally significant actions.
- Re-check permission at execution time, not only when exposing the tool schema.
- Use hooks for logging, policy, metrics, and extensions; keep core loop semantics centralized.
- Make approval outcomes and denials explicit tool results so the model can choose a safe alternative.

## Observe and Evaluate

- Trace request IDs, model calls, tool calls/results, state transitions, retries, approvals, token/cost use, and final outcome.
- Keep replayable transcripts while redacting secrets and personal data.
- Evaluate task success, correctness, tool efficiency, latency, cost, policy violations, recovery rate, and human interventions.
- Test malformed arguments, prompt injection in tool output, duplicate messages, crashes between action and acknowledgement, stale memory, context limits, concurrent claims, and cancellation.
- Roll out with read-only tools, sandbox data, low autonomy, and tight budgets; expand privileges from evidence.

## Produce the Design

Return a concise design containing:

1. Contract and success criteria
2. Chosen control pattern and rejected alternatives
3. Core loop and stop conditions
4. Tool inventory with side-effect classes
5. Context, memory, and persistence plan
6. Permission and human-approval boundaries
7. Error-recovery matrix
8. Protocol/state schemas and concurrency rules
9. Observability, evaluation, and rollout plan

Challenge mechanisms that lack a demonstrated need. A production agent is not the sum of every pattern; it is the smallest coherent harness that remains correct under failure.
