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


def ingest(agent, model, calls, lo, hi, corr=""):
    """Record observability-only traces for an agent we don't proxy directly."""
    for _ in range(calls):
        it, ot = random.randint(lo, hi), random.randint(lo // 4, hi // 4)
        gateway.record(agent, f"{agent}-{corr or 'run'}", model, model, "FORWARD", it, ot, "")


if __name__ == "__main__":
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
        gateway.record("report_writer", sess, "claude-opus-4-8", "claude-opus-4-8",
                       "FORWARD", ctx, random.randint(300, 900),
                       "loop" if i > 4 else "", tool="read_file:report.md" if i % 2 else "read_file:notes.md")

    # --- a runaway loop on a pricey model ---
    for i in range(11):
        gateway.record("code_reviewer", "code_reviewer-pr-204", "claude-opus-4-8", "claude-opus-4-8",
                       "FORWARD", 6000, 400, "loop" if i > 4 else "", tool="read_file:diff.patch")

    print("seeded demo fleet ->", gateway.DB)
    print("next: python3 adduser.py admin admin admin && python3 gateway.py")
