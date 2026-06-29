"""
Seed the trace store with a synthetic demo fleet so the dashboard has something
to show on first run. All agents/data here are fictional.

    python3 seed_demo.py

Then start the gateway and sign in:
    python3 adduser.py admin admin admin
    python3 gateway.py
"""
import json, os, random, urllib.request
import gateway

random.seed(7)
BASE = "http://127.0.0.1:8788"


def _dur(model, out_tok):
    """Synthetic but realistic latency: base + generation time at a model-typical
    output speed (tok/s). Fast small models clear quickly; big models are slower per
    token — so the seeded fleet shows a real throughput spread on the dashboard."""
    m = model.lower()
    spd = 180 if "flash" in m else 130 if "haiku" in m else 45 if "opus" in m else 90
    return round(random.uniform(0.3, 0.8) + out_tok / spd, 3)


def ingest(agent, model, calls, lo, hi, corr=""):
    """Record observability-only traces for an agent we don't proxy directly."""
    for _ in range(calls):
        it, ot = random.randint(lo, hi), random.randint(lo // 4, hi // 4)
        gateway.record(agent, f"{agent}-{corr or 'run'}", model, model, "FORWARD", it, ot, "", dur=_dur(model, ot))


def seed():
    if os.path.exists(gateway.DB):
        os.remove(gateway.DB)
    gateway._db()

    # --- a few cheap, well-behaved agents (observability via record/ingest) ---
    ingest("ticket_triage",         "gemini-3.1-flash-lite", 55, 400, 2200)
    ingest("weekly_summary",        "gemini-3.1-flash-lite", 12, 4000, 16000)
    ingest("web_scraper",           "gemini-3.1-flash-lite", 31, 2500, 14000)
    ingest("doc_assistant",         "claude-haiku-4-5",      26, 1200, 7000)

    # --- an expensive, context-growing agent: the kind the controls target ---
    sess = "report_writer-deepdive"
    ctx = 4000
    for i in range(14):
        ctx = int(ctx * 1.35)  # context balloons every turn — re-sent each call
        out = random.randint(300, 900)
        gateway.record("report_writer", sess, "claude-opus-4-8", "claude-opus-4-8",
                       "FORWARD", ctx, out, "loop" if i > 4 else "",
                       tool="read_file:report.md" if i % 2 else "read_file:notes.md",
                       dur=_dur("claude-opus-4-8", out))

    # --- a runaway loop on a pricey model ---
    for i in range(11):
        gateway.record("code_reviewer", "code_reviewer-pr-204", "claude-opus-4-8", "claude-opus-4-8",
                       "FORWARD", 6000, 400, "loop" if i > 4 else "", tool="read_file:diff.patch",
                       dur=_dur("claude-opus-4-8", 400))

    # --- an orchestrator that fans out to sub-agents: a nested observation tree.
    #     A planning span (r0) spawns generations and a web-research sub-agent span
    #     (s1) with its own child generations; cost/tokens/dur roll up to the root. ---
    def obs(model, in_tok, out_tok, oid, parent="", kind="generation", action="FORWARD", tool=""):
        gateway.record("research_lead", "research_lead-q3-report", model, model, action, in_tok, out_tok, "",
                       tool, "", "", _dur(model, out_tok) if kind == "generation" else 0.0, oid, parent, kind)

    obs("orchestrator", 0, 0, "r0", kind="span", action="PLAN", tool="plan:q3-report")
    obs("claude-opus-4-8", 8200, 640, "g1", parent="r0", tool="decompose")
    obs("sub-agent", 0, 0, "s1", parent="r0", kind="span", action="SPAWN", tool="agent:web_research")
    for i in range(4):
        obs("gemini-3.1-flash-lite", random.randint(2000, 9000), random.randint(400, 1200),
            f"g2_{i}", parent="s1", tool=f"read_file:source{i}.html")
    obs("claude-haiku-4-5", 6400, 1500, "g3", parent="r0", tool="draft:section")
    obs("claude-opus-4-8", 9800, 720, "g4", parent="r0", tool="synthesize")
    return gateway.DB


if __name__ == "__main__":
    seed()
    print("seeded demo fleet ->", gateway.DB)
    print("next: python3 adduser.py admin admin admin && python3 gateway.py")
