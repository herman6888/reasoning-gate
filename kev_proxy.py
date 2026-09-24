"""kev-proxy v2: OpenAI-compatible effort-decision middle layer for Hermes + Codex.

Chain:  client (Hermes chat_completions / Codex responses) -> :8910 kev-proxy
        -> Kev-4B HIP decision service (.80:8904 /v1/systemone)
        -> halogen backend (.89:8731) with reasoning_effort injected

Features:
  * /v1/chat/completions  (Hermes path)
  * /v1/responses         (Codex path; input items: message / function_call / function_call_output)
  * lease: Kev picks how many generations the decision stays valid (invalidated on new user input)
  * fail-open: Kev down / low confidence -> keep client/default effort, log fallback
  * every decision logged to JSONL for morning cron digest
"""
import json, os, sys, time, threading, hashlib, re, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEV_URL = os.environ.get("KEV_URL", "http://192.168.1.10:8905/v1/systemone")
BACKEND_CHAT = os.environ.get("BACKEND_CHAT", "http://192.168.1.20:8731/v1/chat/completions")
BACKEND_RESP = os.environ.get("BACKEND_RESP", "http://192.168.1.20:8731/v1/responses")
BACKEND_MODEL = os.environ.get("BACKEND_MODEL", "halogen-qwen3.8-flash-next")
DEFAULT_EFFORT = os.environ.get("KEV_DEFAULT_EFFORT", "medium")
LOG_PATH = os.environ.get("KEV_LOG", os.path.expanduser("~/.hermes/kev-proxy/decisions.jsonl"))
MAX_STATE_CHARS = 6000
LOW_CONF_KEEP = 0.20

EFFORT_MAP = {"none": "none", "minimal": "minimal", "low": "low", "medium": "medium",
             "high": "high", "xhigh": "xhigh", "max": "xhigh", "ultra": "xhigh"}
EFFORTS = list(EFFORT_MAP.keys())
DESCRIPTIONS = {
  "none": "No reasoning is needed: the next response is fully determined by explicit, verified facts.",
  "minimal": "An immediate, unambiguous next step with almost no inference or comparison required.",
  "low": "Routine exploration or continuation of an established plan. The next useful move and interpretation are clear, even if the overall task is complex.",
  "medium": "Focused reasoning over a few connected facts: compare local alternatives, explain a bounded behavior, or choose a well-scoped implementation or diagnostic step.",
  "high": "Resolve material uncertainty across interacting code paths, competing explanations, or design constraints. The next decision needs broad understanding or careful correctness analysis.",
  "xhigh": "Difficult synthesis across subsystems or conflicting evidence, with subtle invariants or failure paths. Substantial reasoning is needed to discriminate plausible solutions.",
  "max": "Exceptionally demanding reasoning from first principles, a novel algorithm, or a proof-like correctness argument. Additional computation is justified by the unresolved work.",
  "ultra": "The most demanding unresolved problems where the evidence specifically justifies reasoning beyond max. Task importance or impressive terminology alone is insufficient.",
}
EFFORT_INSTR = ("Which reasoning effort is sufficient for the NEXT generation of the coding agent? "
  "Judge the reasoning work ahead, not vocabulary, prompt length, tool names, or the effort already spent. "
  "Select the lowest effort that can advance the goal reliably, including the cost of a wrong decision or rework. "
  "Completed tool calls are evidence, not work awaiting execution.")
LEASE_INSTR = ("For how many upcoming model generations is the required reasoning depth likely to stay stable? "
  "Assess this from the task phase and available evidence, independently of the effort answer.")

_log_lock = threading.Lock()
def log(obj):
    obj["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _log_lock:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def post_json(url, payload, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# Strip harness system-wrappers so Kev judges the real task, not the boilerplate.
# These prefixes dilute the semantic signal (cron/skill notices dominated night runs).
_WRAPPER_PATTERNS = [
    re.compile(r"\[IMPORTANT:\s*You are running as a scheduled cron job\..*?\]\s*", re.S | re.I),
    re.compile(r"\[IMPORTANT:\s*The user has invoked the .*?skill.*?\]\s*", re.S | re.I),
    re.compile(r"<memory-context>.*?</memory-context>\s*", re.S | re.I),
    re.compile(r"\[System note:.*?\]\s*", re.S | re.I),
]
def strip_wrappers(text):
    if not text:
        return text
    for pat in _WRAPPER_PATTERNS:
        text = pat.sub("", text)
    return text.strip()

def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
    return ""

def project_chat(messages):
    latest_user, original_user, notes, tools = "", "", [], []
    for m in messages:
        role, content = m.get("role"), _text(m.get("content"))
        if role == "user" and content.strip():
            if not original_user: original_user = content.strip()
            latest_user = content.strip()
        elif role == "assistant" and content.strip():
            notes.append(content.strip()[:500])
        elif role == "tool":
            tools.append((m.get("name", "tool"), content[:1000]))
    return _assemble(original_user, latest_user, notes, tools), latest_user

def project_responses(inp):
    """Codex /responses input: string or array of items."""
    if isinstance(inp, str):
        return strip_wrappers(inp)[:MAX_STATE_CHARS], inp
    latest_user, original_user, notes, tools = "", "", [], []
    for it in inp or []:
        if not isinstance(it, dict): continue
        t = it.get("type")
        if t == "message" or t is None:
            role = it.get("role")
            content = _text(it.get("content"))
            if role == "user" and content.strip():
                if not original_user: original_user = content.strip()
                latest_user = content.strip()
            elif role == "assistant" and content.strip():
                notes.append(content.strip()[:500])
        elif t == "function_call":
            notes.append(("调用工具 " + str(it.get("name", "")) + " " + str(it.get("arguments", ""))[:300])[:500])
        elif t == "function_call_output":
            tools.append((str(it.get("name", "tool")), _text(it.get("output"))[:1000]))
    return _assemble(original_user, latest_user, notes, tools), latest_user

def _assemble(orig, latest, notes, tools):
    orig, latest = strip_wrappers(orig), strip_wrappers(latest)
    parts = [f"原始任务: {orig[:2000]}", f"最新指令: {latest[:2000]}"]
    if notes:
        parts.append("近期助手说明: " + " | ".join(notes[-3:]))
    for name, out in tools[-6:]:
        parts.append(f"工具[{name}]输出: {out}")
    return "\n".join(parts)[:MAX_STATE_CHARS]

class Lease:
    def __init__(self):
        self.lock = threading.Lock(); self.effort = None; self.remaining = 0; self.h = None
    def get(self, h):
        with self.lock:
            if self.remaining > 0 and self.h == h:
                self.remaining -= 1
                return self.effort, True
            return None, False
    def set(self, effort, n, h):
        with self.lock:
            self.effort = effort; self.remaining = max(n - 1, 0); self.h = h

lease = Lease()

LEASE_CAP = int(os.environ.get("KEV_LEASE_CAP", "5"))  # night run showed lease=10 too aggressive (none rode 5 gens)
NONE_MIN_CONF = float(os.environ.get("KEV_NONE_MIN_CONF", "0.55"))  # 'none' (zero-think) needs higher confidence

def ask_kev(state):
    payload = {"state": {"text": state, "task": state[:200]},
        "questions": {
            "effort": {"type": "choice", "instructions": EFFORT_INSTR,
                       "criteria": {e: DESCRIPTIONS[e] for e in EFFORTS}},
            "lease": {"type": "choice", "instructions": LEASE_INSTR,
                      "criteria": {"1": "Depth changes very soon; decide again next generation.",
                                   "2": "Depth stable for about 2 more generations.",
                                   "3": "Depth stable for about 3 more generations.",
                                   "5": "Depth stable for a sustained stretch of similar work."}}}}
    t0 = time.time()
    resp = post_json(KEV_URL, payload, timeout=90)
    ms = round((time.time() - t0) * 1000, 1)
    ans = resp.get("answers", {})
    eff = ans.get("effort", {})
    probs = eff.get("probabilities") or {}
    top = max(probs.values()) if probs else 0.0
    try: lease_n = int(ans.get("lease", {}).get("choice", "1") or 1)
    except Exception: lease_n = 1
    return eff.get("choice"), top, lease_n, ms

def decide(client_effort, state, tag, latest_user=""):
    ih = hashlib.sha256(latest_user.encode()).hexdigest()[:16]
    cached, hit = lease.get(ih)
    if hit and cached:
        log({"type": "lease_hit", "tag": tag, "effort": cached, "input_hash": ih})
        return cached
    try:
        choice, top, lease_n, kev_ms = ask_kev(state)
        if choice and top >= LOW_CONF_KEEP:
            # Guard: 'none' (zero thinking) is the most damaging misjudgement.
            # Night run showed a multi-step research task judged 'none' at conf 0.37.
            # Require higher confidence for 'none'; otherwise bump to 'minimal'.
            if choice == "none" and top < NONE_MIN_CONF:
                log({"type": "none_bumped", "tag": tag, "from": "none", "to": "minimal",
                     "confidence": round(top, 3), "input_hash": ih})
                choice = "minimal"
            lease.set(choice, min(lease_n, LEASE_CAP), ih)
            log({"type": "decision", "tag": tag, "effort": choice, "confidence": round(top, 3),
                 "lease": min(lease_n, LEASE_CAP), "kev_ms": kev_ms, "input_hash": ih})
            return choice
        log({"type": "fallback_low_conf", "tag": tag, "effort": client_effort,
             "confidence": round(top, 3), "input_hash": ih})
        return client_effort
    except Exception as e:
        log({"type": "fallback_error", "tag": tag, "effort": client_effort,
             "error": str(e)[:200], "input_hash": ih})
        return client_effort

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, body_bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, json.dumps({"ok": True, "kev": KEV_URL,
                "backend_chat": BACKEND_CHAT, "backend_resp": BACKEND_RESP}).encode())
        if self.path == "/v1/models":
            return self._send(200, json.dumps({"object": "list", "data": [
                {"id": "kev-auto", "object": "model", "owned_by": "kev-proxy"}]}).encode())
        return self._send(404, b'{"error":"not found"}')

    def _forward(self, url, body, tag):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as r:
                resp_bytes = r.read()
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read())
        except Exception as e:
            return self._send(502, json.dumps({"error": f"backend: {e}"}).encode())
        backend_ms = round((time.time() - t0) * 1000, 1)
        try:
            rd = json.loads(resp_bytes.decode())
            u = rd.get("usage", {})
            rt = (u.get("output_tokens_details") or {}).get("reasoning_tokens") or \
                 (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            log({"type": "applied", "tag": tag, "effort": body.get("reasoning_effort"),
                 "backend_ms": backend_ms,
                 "completion_tokens": u.get("completion_tokens") or u.get("output_tokens"),
                 "reasoning_tokens": rt})
        except Exception:
            pass
        return self._send(200, resp_bytes)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._send(400, json.dumps({"error": f"bad json: {e}"}).encode())
        client_effort = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort") or DEFAULT_EFFORT
        if self.path == "/v1/chat/completions":
            state, latest_user = project_chat(body.get("messages", []))
            tag = "chat"
        elif self.path == "/v1/responses":
            state, latest_user = project_responses(body.get("input", ""))
            tag = "responses"
        else:
            return self._send(404, b'{"error":"not found"}')
        decided = decide(client_effort, state, tag, latest_user)
        mapped = EFFORT_MAP.get(decided, DEFAULT_EFFORT)
        body["model"] = BACKEND_MODEL
        body["reasoning_effort"] = mapped
        if tag == "responses":
            body["reasoning"] = {"effort": mapped}
        # audit trail: keep a state preview so morning digest can review decisions
        log({"type": "state_preview", "tag": tag, "effort": mapped,
             "state_head": state[:300], "state_chars": len(state)})
        return self._forward(BACKEND_RESP if tag == "responses" else BACKEND_CHAT, body, tag)

if __name__ == "__main__":
    port = int(os.environ.get("KEV_PROXY_PORT", "8910"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"kev-proxy v2 listening :{port} kev={KEV_URL}", flush=True)
    srv.serve_forever()
