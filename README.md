# Vestal Gateway

**An open-source meter for enterprise agent fleets.**

Vestal sits at the one seam every agent shares — the model call — and makes agent
spend visible. Point your agents at it and every call is metered: tokens, cost,
model, latency, tools, the sub-agent/turn tree, rolled up by person, team, and
agent. Basic gateway controls (response cache, runaway-loop kill, model routing)
are built in and toggle live.

Drop-in and API-compatible. No process rewrites — just repoint a base URL.

```
agent ──▶ vestal gateway ──▶ model provider
              │
              └──▶ trace store + dashboard
```

## Why

Teams are spending fast and blind on coding agents. Orchestration engines see a
task's duration — never tokens, waste, or the sub-agents an agent spawns in code.
Generic LLM-observability sits *beside* the calls. Vestal sits *astride* them, at
the model call, so the same chokepoint both measures spend and can act on it.

## Quickstart

No dependencies — Python 3.10+ standard library only.

```bash
git clone https://github.com/thomasgraham-1/vestal-gateway
cd vestal-gateway

python3 seed_demo.py                  # synthetic demo fleet so the dashboard isn't empty
python3 adduser.py admin admin admin  # create an admin login (username admin / pw admin)
python3 gateway.py                    # serves the dashboard on http://127.0.0.1:8788
```

Open <http://127.0.0.1:8788>, sign in as `admin` / `admin`, and you'll see the
seeded fleet: spend by agent, a $/call plot, an off-nominal event log, and
(as admin) live gateway controls. Click an agent to drill into its longest run —
context growth per turn and a full step trace.

To meter real agents, see [`examples/point_an_agent.md`](examples/point_an_agent.md).
With no API key set the gateway returns deterministic mocks, so everything above
works fully offline.

## What it does

- **Meter** — a trace per call (tokens, cost, model, latency, tool, action),
  stored in sqlite, rolled up by owner / agent. Role-scoped: an admin sees the
  whole org; a non-admin is locked to their own agents, enforced server-side.
- **Throughput** — because the gateway times the upstream call, every trace also
  carries duration, so the meter surfaces **output tokens/sec** per agent and across
  the fleet — the velocity/opportunity-cost lens, not just spend. Cache hits and
  loop kills are instant (dur 0) and don't dilute it.
- **Observation tree** — each call is an observation with an `obs_id`, a `parent_id`,
  and a `kind` (`generation` = an LLM call, `span` = a unit of work / sub-agent,
  `event` = an instant marker). They nest into a per-session tree and cost / tokens /
  duration roll up it, so you see a sub-agent's full subtree cost, not just flat rows.
  Agents declare the tree by sending `x-vestal-obs` (this call) and `x-vestal-parent`
  (its parent) — the same run_id / parent_run_id convention orchestration frameworks use.
- **Proxy** — Anthropic (`/v1/messages`, streaming or not) and Gemini
  (`/v1beta/models/<m>:generateContent`) compatible. Forwards to the real API
  when a key is set, otherwise mocks.
- **Ingest** — `POST /v_ingest` for agents you can't route through the proxy.
- **Controls** — response cache, runaway-loop kill, and model routing
  (right-sizing), all toggled live from the dashboard or `POST /v_control`.
- **Identity** — virtual keys carrying tenant/team/agent, or `x-vestal-*` headers.

## Configuration

All via environment variables:

| Variable | Purpose |
|---|---|
| `VESTAL_SESSION_SECRET` | Signing secret for session cookies. **Set a strong value in production.** |
| `ANTHROPIC_API_KEY` | If set, the Anthropic proxy forwards to the real API (else mock). |
| `GEMINI_API_KEY` | If set, the Gemini proxy forwards to the real API (else mock). |
| `VESTAL_SECURE` | Set when serving over HTTPS — marks session cookies `Secure`. |
| `PORT` | When set, the gateway binds `0.0.0.0:$PORT` (for container platforms). |

Edit token prices and the demo org roster at the top of
[`gateway.py`](gateway.py) (`PRICES`, `ORG`).

A `Dockerfile` is included for container deploys.

## Scope

This repo is the **metering + manual control layer**. It is deliberately
self-contained and stdlib-only so it's easy to audit and self-host on the
critical path. The recommendation engine that decides *which* control to pull,
shadow-tests it before proposing it, and ties spend to outcomes is a separate,
commercial product built on top of this gateway.

## License

[Apache 2.0](LICENSE).
