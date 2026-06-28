# Pointing an agent at the gateway

The gateway is API-compatible, so an agent just needs its base URL repointed —
no code changes. Every call is then metered and (optionally) routed/cached.

## Anthropic-compatible agents (e.g. Claude Code, the Anthropic SDK)

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788
# identify the agent so traces attribute correctly (optional but recommended):
#   send header  x-vestal-agent: <name>   and   x-vestal-session: <run-id>
```

Raw request:

```bash
curl -s http://127.0.0.1:8788/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-vestal-agent: report_writer' \
  -H 'x-vestal-session: run-42' \
  -d '{"model":"claude-opus-4-8","max_tokens":256,
       "messages":[{"role":"user","content":"hello"}]}'
```

If `ANTHROPIC_API_KEY` is set on the gateway it forwards to the real API;
otherwise it returns a deterministic mock so you can try everything offline.

## Gemini-compatible agents (google-genai)

Point the base URL at the gateway; it proxies
`POST /v1beta/models/<model>:generateContent`.

## Agents you can't repoint — ingest instead

For an agent you don't proxy, POST a trace after each call so it still shows up
in the fleet view:

```bash
curl -s http://127.0.0.1:8788/v_ingest \
  -H 'content-type: application/json' \
  -d '{"agent":"ticket_triage","model":"gemini-3.1-flash-lite",
       "in_tok":1800,"out_tok":300,"action":"FORWARD"}'
```

## Virtual keys (identity without headers)

Mint a key that carries tenant/team/agent identity, then send it as
`x-vestal-key` (or `Authorization: Bearer <vk>`):

```bash
curl -s http://127.0.0.1:8788/v_keys \
  -H 'content-type: application/json' \
  -d '{"tenant":"acme","team":"data","agent":"report_writer"}'
```
