"""
Vestal gateway — the open-source meter for agent fleets.

Every agent's model call routes through here. The gateway is:

  - an Anthropic-compatible proxy at POST /v1/messages          -> intercept + record a trace
  - a Gemini-compatible proxy at POST /v1beta/models/<m>:generateContent
  - a generic ingest at POST /v_ingest                          -> trace agents you don't proxy
  - a JSON API (/v_stats, /v_traces, /v_channel) for the dashboard
  - the dashboard itself at GET /

Basic gateway controls are built in and toggle live: response cache, runaway-loop
kill, and model routing (right-sizing). The recommendation engine that decides
*which* control to pull and shadow-tests it first is a separate, commercial
product — this repo is the metering + manual control layer it sits on top of.

Trace store: sqlite (vestal.db), Python stdlib only, no external deps.
If ANTHROPIC_API_KEY / GEMINI_API_KEY is set the proxy forwards to the real API;
otherwise it uses a deterministic mock upstream so everything runs offline.
"""
import base64, hashlib, hmac, json, os, secrets, sqlite3, threading, time, urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_SECRET = os.environ.get("VESTAL_SESSION_SECRET", "vestal-dev-secret-change-me").encode()

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "vestal.db")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
UPSTREAM = "https://api.anthropic.com/v1/messages"
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/models"

CONFIG = {"cache": True, "route_map": {}, "loop_kill": None}

# $ per 1M tokens (input, output) — rough public list prices, edit per provider
PRICES = {
    "opus":   (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (0.80, 4.0),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini": (0.35, 1.05),
    "_default": (5.0, 15.0),
}

_cache, _session_calls = {}, {}
_login_attempts = {}
_lock = threading.Lock()
SECURE = bool(os.environ.get("VESTAL_SECURE"))  # set in production (HTTPS) for Secure cookies

# Demo org roster: which person owns which agents. In production this comes from
# the virtual-key registry (see create_key); here it scopes the dashboard so an
# 'admin' sees the whole org and a non-admin is locked to their own agents.
ORG = {
    "alex · data":     ["web_scraper", "report_writer", "weekly_summary"],
    "sam · platform":  ["code_reviewer", "doc_assistant"],
    "support team":    ["ticket_triage"],
}
AGENT_OWNER = {a: o for o, ags in ORG.items() for a in ags}


def _scope(view):
    """Return (sql_clause, params) limiting to an owner's agents; admin = no limit."""
    if not view or view == "admin":
        return "", []
    ags = ORG.get(view, ["__none__"])
    return " WHERE agent IN (%s)" % ",".join("?" * len(ags)), ags


def _db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS traces(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, session TEXT,
        model_in TEXT, model_out TEXT, action TEXT, in_tok INT, out_tok INT,
        cost REAL, waste TEXT, tool TEXT, tenant TEXT, corr TEXT, dur REAL DEFAULT 0)""")
    # dur = wall-clock seconds the upstream call took. Captured here at the chokepoint
    # (nowhere else can), it's what turns the meter from spend-only into a throughput
    # meter: out_tok / dur is tokens/sec of useful output. Migrate stores that predate it.
    if "dur" not in [r[1] for r in c.execute("PRAGMA table_info(traces)").fetchall()]:
        c.execute("ALTER TABLE traces ADD COLUMN dur REAL DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS keys(
        vk TEXT PRIMARY KEY, tenant TEXT, team TEXT, agent TEXT, created REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, pw_hash TEXT, role TEXT, display TEXT)""")
    return c


# ---- auth: users, passwords, signed sessions ----
def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def create_user(username, pw, role, display=""):
    with _lock:
        c = _db()
        c.execute("INSERT OR REPLACE INTO users(username,pw_hash,role,display) VALUES(?,?,?,?)",
                  (username, _hash_pw(pw), role, display or username))
        c.commit(); c.close()
    return {"username": username, "role": role}


def verify_user(username, pw):
    c = _db()
    row = c.execute("SELECT pw_hash,role,display FROM users WHERE username=?", (username,)).fetchone()
    c.close()
    if not row:
        return None
    salt = row[0].split("$", 1)[0]
    if hmac.compare_digest(_hash_pw(pw, salt), row[0]):
        return {"username": username, "role": row[1], "display": row[2]}
    return None


def make_session(username, role):
    payload = f"{username}|{role}|{int(time.time()) + 86400}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_session(token):
    try:
        username, role, exp, sig = base64.urlsafe_b64decode(token).decode().split("|")
        good = hmac.new(SESSION_SECRET, f"{username}|{role}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return {"username": username, "role": role}
    except Exception:
        pass
    return None


# ---- virtual keys (onboarding / identity) ----
def create_key(tenant, team, agent):
    vk = "vk_" + hashlib.sha256(f"{tenant}/{team}/{agent}/{time.time()}".encode()).hexdigest()[:24]
    with _lock:
        c = _db()
        c.execute("INSERT INTO keys(vk,tenant,team,agent,created) VALUES(?,?,?,?,?)",
                  (vk, tenant, team, agent, time.time()))
        c.commit(); c.close()
    return {"vk": vk, "tenant": tenant, "team": team, "agent": agent}


def resolve_key(vk):
    if not vk:
        return None
    c = _db()
    row = c.execute("SELECT tenant,team,agent FROM keys WHERE vk=?", (vk,)).fetchone()
    c.close()
    return {"tenant": row[0], "team": row[1], "agent": row[2]} if row else None


def list_keys():
    c = _db()
    rows = c.execute("SELECT vk,tenant,team,agent FROM keys ORDER BY created").fetchall()
    c.close()
    return [{"vk": v, "tenant": t, "team": tm, "agent": a} for v, t, tm, a in rows]


def _price(model):
    m = (model or "").lower()
    for k, v in PRICES.items():
        if k != "_default" and k in m:
            return v
    return PRICES["_default"]


def _cost(model, in_tok, out_tok):
    pin, pout = _price(model)
    return round(in_tok / 1e6 * pin + out_tok / 1e6 * pout, 6)


def record(agent, session, model_in, model_out, action, in_tok, out_tok, waste, tool="", tenant="", corr="", dur=0.0):
    cost = _cost(model_out, in_tok, out_tok)
    with _lock:
        c = _db()
        c.execute("INSERT INTO traces(ts,agent,session,model_in,model_out,action,in_tok,out_tok,cost,waste,tool,tenant,corr,dur)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), agent, session, model_in, model_out, action, in_tok, out_tok, cost, waste, tool, tenant, corr, dur))
        c.commit(); c.close()
    tps = out_tok / dur if dur > 0 else 0
    print(f"    [vestal] agent={agent} action={action} model={model_out} "
          f"tok={in_tok}/{out_tok} ${cost} {dur * 1000:.0f}ms {tps:.0f}tok/s waste={waste or '-'}", flush=True)
    return cost


# ---- proxy / valve ----
def _resp(model, content, stop, in_tok, out_tok):
    return {"id": "msg_x", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}


def _mock(body):
    msgs = body.get("messages", [])
    ctx = max(120, len(json.dumps(msgs)) // 4)  # context grows as the conversation accumulates
    if "CACHE_ME" in json.dumps(msgs):
        return _resp(body["model"], [{"type": "text", "text": "The answer is 42."}], "end_turn", ctx, 8)
    return _resp(body["model"], [{"type": "tool_use", "id": "tu", "name": "read_file",
                 "input": {"path": "loop.txt"}}], "tool_use", ctx, 20)


def _real(body):
    b = dict(body); b.pop("stream", None)  # buffer upstream, we re-stream to the client
    req = urllib.request.Request(UPSTREAM, data=json.dumps(b).encode(),
        headers={"content-type": "application/json", "x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _sse(resp):
    """Serialize a full message into a valid Anthropic SSE event stream, so a
    streaming client (e.g. Claude Code) reconstructs it without breaking —
    even when the body is a cache hit or a synthetic loop-kill."""
    out = []
    def ev(t, d):
        out.append(f"event: {t}\ndata: {json.dumps(d)}\n\n")
    u = resp.get("usage", {})
    ev("message_start", {"type": "message_start", "message": {"id": resp.get("id"), "type": "message",
        "role": "assistant", "model": resp.get("model"), "content": [], "stop_reason": None,
        "usage": {"input_tokens": u.get("input_tokens", 0), "output_tokens": 0}}})
    for i, b in enumerate(resp.get("content", [])):
        if b.get("type") == "text":
            ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b.get("text", "")}})
        elif b.get("type") == "tool_use":
            ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": {}}})
            ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b.get("input", {}))}})
        ev("content_block_stop", {"type": "content_block_stop", "index": i})
    ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": resp.get("stop_reason")}, "usage": {"output_tokens": u.get("output_tokens", 0)}})
    ev("message_stop", {"type": "message_stop"})
    return "".join(out).encode()


def _key(body):
    sub = {k: body.get(k) for k in ("model", "system", "messages", "tools")}
    return hashlib.sha256(json.dumps(sub, sort_keys=True, default=str).encode()).hexdigest()[:12]


def intercept(body, agent, session, tenant="", corr=""):
    n = _session_calls.get(session, 0) + 1
    _session_calls[session] = n
    model_in = body.get("model")

    if CONFIG["loop_kill"] and n > CONFIG["loop_kill"]:
        record(agent, session, model_in, model_in, "LOOP_KILL", 0, 0, "runaway_loop", "", tenant, corr)
        return _resp(model_in, [{"type": "text", "text": f"[vestal] loop terminated after {CONFIG['loop_kill']} calls"}], "end_turn", 0, 0)

    ck = _key(body)
    if CONFIG["cache"] and ck in _cache:
        r = _cache[ck]
        record(agent, session, model_in, model_in, "CACHE_HIT", 0, 0, "cache_saved", "", tenant, corr)
        return r

    model_out = CONFIG["route_map"].get(model_in, model_in)
    body["model"] = model_out
    t0 = time.time()
    resp = _real(body) if API_KEY else _mock(body)
    dur = time.time() - t0  # the only place wall-clock per call can be measured
    u = resp.get("usage", {})
    waste = "downgraded" if model_out != model_in else ("loop" if n > 4 else "")
    tu = next((b for b in resp.get("content", []) if b.get("type") == "tool_use"), None)
    tool = f"{tu['name']}:{tu.get('input', {}).get('path', '')}" if tu else ""
    record(agent, session, model_in, model_out, "MODEL_SWAP" if model_out != model_in else "FORWARD",
           u.get("input_tokens", 0), u.get("output_tokens", 0), waste, tool, tenant, corr, dur)
    if CONFIG["cache"]:
        _cache[ck] = resp
    return resp


# ---- Gemini-shaped proxy (google-genai api-key agents) ----
def _gem_resp(model, parts, in_tok, out_tok):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": in_tok, "candidatesTokenCount": out_tok, "totalTokenCount": in_tok + out_tok}}


def _gem_mock(model, body):
    if "CACHE_ME" in json.dumps(body.get("contents", [])):
        return _gem_resp(model, [{"text": "The answer is 42."}], 120, 8)
    return _gem_resp(model, [{"functionCall": {"name": "read_file", "args": {"path": "loop.txt"}}}], 300, 20)


def _gem_real(model, body):
    url = f"{GEMINI_UPSTREAM}/{model}:generateContent?key={GEMINI_KEY}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def intercept_gemini(model_in, body, agent, session, tenant="", corr=""):
    n = _session_calls.get(session, 0) + 1
    _session_calls[session] = n

    if CONFIG["loop_kill"] and n > CONFIG["loop_kill"]:
        record(agent, session, model_in, model_in, "LOOP_KILL", 0, 0, "runaway_loop", "", tenant, corr)
        return _gem_resp(model_in, [{"text": f"[vestal] loop terminated after {CONFIG['loop_kill']} calls"}], 0, 0)

    ck = hashlib.sha256((model_in + json.dumps(body.get("contents", []), sort_keys=True, default=str)).encode()).hexdigest()[:12]
    if CONFIG["cache"] and ck in _cache:
        record(agent, session, model_in, model_in, "CACHE_HIT", 0, 0, "cache_saved", "", tenant, corr)
        return _cache[ck]

    model_out = CONFIG["route_map"].get(model_in, model_in)
    t0 = time.time()
    resp = _gem_real(model_out, body) if GEMINI_KEY else _gem_mock(model_out, body)
    dur = time.time() - t0
    u = resp.get("usageMetadata", {})
    waste = "downgraded" if model_out != model_in else ("loop" if n > 4 else "")
    fc = next((p["functionCall"] for p in resp["candidates"][0]["content"]["parts"] if "functionCall" in p), None)
    tool = f"{fc['name']}:{fc.get('args', {}).get('path', '')}" if fc else ""
    record(agent, session, model_in, model_out, "MODEL_SWAP" if model_out != model_in else "FORWARD",
           u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0), waste, tool, tenant, corr, dur)
    if CONFIG["cache"]:
        _cache[ck] = resp
    return resp


# ---- dashboard data ----
def stats(view=None):
    w, p = _scope(view)
    aw = (w + " AND" if w else " WHERE") + " waste IN('cache_saved','runaway_loop','downgraded')"
    c = _db(); cur = c.cursor()
    row = cur.execute("SELECT COUNT(*), COALESCE(SUM(in_tok+out_tok),0), COALESCE(SUM(cost),0), COALESCE(SUM(out_tok),0), COALESCE(SUM(dur),0) FROM traces" + w, p).fetchone()
    by_agent = cur.execute("SELECT agent, COUNT(*), SUM(in_tok+out_tok), ROUND(SUM(cost),4), COALESCE(SUM(out_tok),0), COALESCE(SUM(dur),0) FROM traces" + w + " GROUP BY agent ORDER BY 4 DESC", p).fetchall()
    by_waste = cur.execute("SELECT COALESCE(NULLIF(waste,''),'productive'), COUNT(*), ROUND(SUM(cost),4) FROM traces" + w + " GROUP BY 1 ORDER BY 3 DESC", p).fetchall()
    saved = cur.execute("SELECT COUNT(*) FROM traces" + aw, p).fetchone()[0]
    c.close()
    # out_tps = output tokens per second of model wall-clock — the fleet's throughput,
    # the opportunity-cost metric that spend-only meters miss. Only rows that actually
    # ran upstream carry dur>0 (cache hits / loop kills are instant), so they don't dilute it.
    out_tps = round(row[3] / row[4], 1) if row[4] else 0
    return {"view": view or "admin", "calls": row[0], "tokens": row[1], "cost": round(row[2], 4), "saved_actions": saved,
            "out_tok": row[3], "gen_secs": round(row[4], 1), "out_tps": out_tps,
            "by_agent": [{"agent": a, "calls": n, "tokens": t, "cost": co, "out_tok": ot,
                          "tps": round(ot / d, 1) if d else 0, "owner": AGENT_OWNER.get(a, "unassigned")}
                         for a, n, t, co, ot, d in by_agent],
            "by_waste": [{"kind": k, "calls": n, "cost": co} for k, n, co in by_waste]}


def channel(agent):
    c = _db()
    rows = c.execute("SELECT session,model_out,in_tok,out_tok,COALESCE(tool,''),action,COALESCE(dur,0) FROM traces WHERE agent=? ORDER BY id", (agent,)).fetchall()
    c.close()
    runs = {}
    for sess, model, in_tok, out_tok, tool, action, dur in rows:
        runs.setdefault(sess, []).append({"model": model, "in": in_tok, "out": out_tok, "tool": tool, "action": action, "dur": round(dur, 3)})
    rl = [{"session": k, "turns": v} for k, v in runs.items()]
    rl.sort(key=lambda r: -len(r["turns"]))
    detail = rl[0] if rl else None
    peak = max((t["in"] for r in rl for t in r["turns"]), default=0)
    avg_turns = round(sum(len(r["turns"]) for r in rl) / len(rl), 1) if rl else 0
    cost = round(sum(_cost(t["model"], t["in"], t["out"]) for r in rl for t in r["turns"]), 4)
    out_sum = sum(t["out"] for r in rl for t in r["turns"])
    dur_sum = sum(t["dur"] for r in rl for t in r["turns"])
    tps = round(out_sum / dur_sum, 1) if dur_sum else 0
    return {"agent": agent, "runs": len(rl), "avg_turns": avg_turns, "peak": peak, "cost": cost,
            "tps": tps, "gen_secs": round(dur_sum, 1), "detail": detail}


def traces(view=None, limit=40):
    w, p = _scope(view)
    c = _db()
    rows = c.execute("SELECT ts,agent,session,model_in,model_out,action,in_tok,out_tok,cost,waste,COALESCE(dur,0) FROM traces" + w + " ORDER BY id DESC LIMIT ?", p + [limit]).fetchall()
    c.close()
    cols = ["ts", "agent", "session", "model_in", "model_out", "action", "in_tok", "out_tok", "cost", "waste", "dur"]
    return [dict(zip(cols, r)) for r in rows]


def org_roster():
    """Every owner, their agents, and their spend — the org rollup for an admin view."""
    c = _db()
    out = []
    for owner, ags in ORG.items():
        ph = ",".join("?" * len(ags))
        row = c.execute(f"SELECT COUNT(*), COALESCE(SUM(cost),0) FROM traces WHERE agent IN ({ph})", ags).fetchone()
        out.append({"owner": owner, "agents": ags, "calls": row[0], "cost": round(row[1], 4)})
    c.close()
    return sorted(out, key=lambda x: -x["cost"])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(200); self.send_header("content-type", ctype); self.end_headers(); self.wfile.write(body)

    def _ident(self):
        # resolve a virtual key (onboarding identity) + optional correlation id
        vk = self.headers.get("x-vestal-key") or (self.headers.get("authorization", "")[7:] if self.headers.get("authorization", "").lower().startswith("bearer ") else "")
        ik = resolve_key(vk)
        agent = ik["agent"] if ik else self.headers.get("x-vestal-agent", "unknown")
        tenant = ik["tenant"] if ik else self.headers.get("x-vestal-tenant", "")
        corr = self.headers.get("x-vestal-corr", "")
        session = self.headers.get("x-vestal-session", "default")
        return agent, tenant, corr, session

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return read_session(c["vestal_session"].value) if "vestal_session" in c else None

    def _page(self, name):
        p = os.path.join(HERE, name)
        self._send(open(p).read() if os.path.exists(p) else "<h1>vestal</h1>", "text/html")

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == "/login": return self._page("login.html")
        if path == "/logout":
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "vestal_session=; Path=/; Max-Age=0"); self.end_headers(); return
        sess = self._session()
        if not sess:
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return
        role = sess["role"]
        view = (q.get("as", [None])[0] or "admin") if role == "admin" else role  # non-admins locked to their scope
        if path == "/v_me": return self._send({**sess, "is_admin": role == "admin"})
        if path == "/v_stats": return self._send(stats(view))
        if path == "/v_traces": return self._send(traces(view))
        if path == "/v_config": return self._send(CONFIG)
        if path == "/v_org":
            if role == "admin": return self._send({"owners": org_roster(), "you_can_view": ["admin"] + list(ORG)})
            return self._send({"owners": [o for o in org_roster() if o["owner"] == role], "you_can_view": [role]})
        if path == "/v_keys": return self._send(list_keys())
        if path == "/v_channel":
            agent = q.get("agent", [""])[0]
            if role != "admin" and agent not in ORG.get(role, []):
                return self._send({"error": "forbidden"})
            return self._send(channel(agent))
        self._page("dashboard.html")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)) or 0)
        if self.path.startswith("/login"):
            from urllib.parse import parse_qs
            ip = self.client_address[0]; now = time.time()
            att = [t for t in _login_attempts.get(ip, []) if now - t < 300]
            if len(att) >= 8:  # rate limit: 8 attempts / 5 min / IP
                self.send_response(429); self.end_headers(); return
            _login_attempts[ip] = att + [now]
            f = parse_qs(raw.decode())
            usr = verify_user(f.get("username", [""])[0], f.get("password", [""])[0])
            if usr:
                tok = make_session(usr["username"], usr["role"])
                sec = "; Secure" if SECURE else ""
                self.send_response(302); self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"vestal_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{sec}")
            else:
                self.send_response(302); self.send_header("Location", "/login?e=1")
            self.end_headers(); return
        body = json.loads(raw or b"{}")
        if self.path.startswith("/v_control"):  # change gateway controls from the UI — admin only
            sess = self._session()
            if not sess or sess.get("role") != "admin":
                self.send_response(403); self.end_headers()
                self.wfile.write(b'{"error":"admin only"}'); return
            if "loop_kill" in body: CONFIG["loop_kill"] = body["loop_kill"]
            if "cache" in body: CONFIG["cache"] = bool(body["cache"])
            if "route_map" in body: CONFIG["route_map"] = body["route_map"] or {}
            return self._send({"ok": True, "config": CONFIG})
        if self.path.startswith("/v_keys"):
            return self._send(create_key(body.get("tenant", ""), body.get("team", ""), body.get("agent", "")))
        agent, tenant, corr, session = self._ident()
        if self.path.startswith("/v_ingest"):
            record(body.get("agent", agent), body.get("session", session), body.get("model"), body.get("model"),
                   body.get("action", "FORWARD"), body.get("in_tok", 0), body.get("out_tok", 0), body.get("waste", ""),
                   "", body.get("tenant", tenant), body.get("corr", corr), body.get("dur", 0.0))
            return self._send({"ok": True})
        if "/models/" in self.path and ":generateContent" in self.path:
            model = self.path.split("/models/")[1].split(":")[0]
            return self._send(intercept_gemini(model, body, body.get("_agent", agent), session, tenant, corr))
        try:
            resp = intercept(body, agent, session, tenant, corr)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(_sse(resp))
            else:
                self._send(resp)
        except Exception as e:
            self.send_response(500); self.end_headers(); self.wfile.write(json.dumps({"error": str(e)}).encode())


def serve(port=8788, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8788))          # container platforms inject PORT
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    if os.environ.get("VESTAL_DEMO"):                 # one-shot demo: seed fictional fleet + an admin login
        try:
            import seed_demo
            seed_demo.seed()
            create_user(os.environ.get("VESTAL_DEMO_USER", "admin"),
                        os.environ.get("VESTAL_DEMO_PASS", "admin"), "admin")
            print("[demo] seeded fleet + admin login", flush=True)
        except Exception as e:
            print("[demo] seed skipped:", e, flush=True)
    serve(port, host)
    print(f"vestal gateway on {host}:{port}  (dashboard at /)  upstream={'REAL' if API_KEY else 'MOCK'}  secure={SECURE}", flush=True)
    while True:
        time.sleep(3600)
