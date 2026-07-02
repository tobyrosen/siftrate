#!/usr/bin/env python3
"""siftrate — a single-file, config-driven rating/labeling server.

Point it at a JSON config describing your items and a scoring scheme, open the
URL on your phone, and tap to score. Results stream to a JSON file you own.
No database, no build step, no dependencies — just Python's standard library.

Quickstart:
    python3 siftrate.py --config examples/blog-post-drafts.json
    # then open the printed URL (defaults to http://127.0.0.1:8091/)

Config schema (JSON):
    {
      "title":      "Page heading",
      "port":       8091,                    // optional (default 8091)
      "host":       "127.0.0.1",             // optional (default 127.0.0.1)
      "items":      [ ... ],                 // inline array, OR use items_file
      "items_file": "items.json",            // alternative to inline items
      "labels": {
        "scale": {                           // optional — omit for flags-only
          "min": 1, "max": 5,
          "captions": {"1": "poor", "5": "great"}   // optional per-value labels
        },
        "flags": ["follow up", "off-brand"], // optional multi-select toggles
        "note":  true                         // optional free-text per item
      },
      "output": "results.json"               // required — where scores are written
    }

Each item needs an "id". Optional per item: "title", "body" (text OR HTML),
"url" (renders the title as an http(s) link), "meta" (small-print annotation).

Results file — one entry per item id, merged and rewritten atomically on every
save (a kill mid-write cannot corrupt it):
    { "score": 3, "flags": ["follow up"], "note": "...", "updated_at": "..." }

Security model (see README for the full write-up):
  * Binds 127.0.0.1 by default — safe on your own machine.
  * To rate from your phone: put the machine on a tailnet (Tailscale) and bind
    that interface, or bind 0.0.0.0 on a trusted LAN. Whenever you widen the
    bind past localhost, pass --token <secret> to gate every route.
  * No telemetry and no outbound network calls: siftrate only ever talks to
    the browser that connects to it.
"""
import argparse
import hmac
import json
import os
import tempfile
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091
MAX_BODY = 32 * 1024 * 1024  # 32 MB cap on /save payloads
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    # Resolve items (inline array wins; otherwise read items_file)
    if "items_file" in cfg and "items" not in cfg:
        cfg["items"] = json.loads(Path(cfg["items_file"]).read_text())
    if not cfg.get("items"):
        raise ValueError("Config must supply 'items' or 'items_file'")
    for i, item in enumerate(cfg["items"]):
        if not item.get("id"):
            raise ValueError(f"Item at index {i} missing required 'id' field")
    if not cfg.get("output"):
        raise ValueError("Config must supply 'output' path")
    return cfg


# ---------------------------------------------------------------------------
# Atomic write — temp file in the same dir, then os.replace (same-filesystem
# rename is atomic on POSIX and Windows). A crash mid-write leaves the old file
# intact, never a half-written one.
# ---------------------------------------------------------------------------

def atomic_write(path: str, data: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_results(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _esc_py(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_page(cfg: dict) -> str:
    labels = cfg.get("labels") or {}
    title = cfg.get("title", "Rate Items")
    items_js = json.dumps(cfg["items"], ensure_ascii=False)
    labels_js = json.dumps(labels, ensure_ascii=False)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc_py(title)}</title>
<style>
:root{{--ink:#16243A;--blue:#448BDE;--line:#e3e9f1;--muted:#5a6b82;--bg:#f7f9fc;}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;line-height:1.45;}}
.wrap{{max-width:760px;margin:0 auto;padding:14px 12px 90px;}}
h1{{font-size:21px;margin:4px 0 14px;}}
.bar{{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0 10px;
  border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;font-size:13.5px;}}
.bar b{{font-size:16px;}}
.prog{{flex:1;height:8px;background:#e3e9f1;border-radius:5px;overflow:hidden;}}
.prog>i{{display:block;height:100%;background:var(--blue);width:0;transition:width .2s;}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 13px;margin:10px 0;}}
.card.labeled{{border-left:4px solid var(--blue);}}
.tt{{font-weight:700;font-size:15.5px;}}
.tt a{{color:var(--ink);text-decoration:none;}} .tt a:active{{color:var(--blue);}}
.body{{font-size:14px;margin:5px 0 8px;color:var(--ink);}}
.body img{{max-width:100%;height:auto;border-radius:8px;}}
.meta{{color:var(--muted);font-size:12.5px;margin:3px 0 9px;}}
.scores{{display:flex;gap:7px;margin:4px 0;}}
.scores button{{flex:1;padding:11px 0;font-size:16px;font-weight:700;border:1.5px solid var(--line);
  background:#fff;border-radius:9px;color:var(--ink);cursor:pointer;}}
.scores button.on{{background:var(--blue);border-color:var(--blue);color:#fff;}}
.scl{{display:flex;justify-content:space-between;font-size:10.5px;color:var(--muted);margin:-1px 2px 8px;}}
.flags{{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px;}}
.flag-btn{{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600;
  cursor:pointer;user-select:none;background:#f2f5fa;border:1.5px solid var(--line);
  border-radius:8px;padding:7px 12px;color:var(--ink);}}
.flag-btn.on{{background:#dceeff;border-color:var(--blue);color:var(--blue);}}
.flag-btn input{{width:17px;height:17px;pointer-events:none;}}
.flag-btn:focus-visible{{outline:2px solid var(--blue);outline-offset:2px;}}
.note{{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px;
  font-size:13px;font-family:inherit;margin-top:8px;resize:vertical;min-height:38px;}}
.foot{{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid var(--line);
  padding:10px 14px;display:flex;align-items:center;gap:12px;font-size:13px;}}
.foot .st{{flex:1;color:var(--muted);}}
</style></head><body><div class="wrap">
<h1>{_esc_py(title)}</h1>
<div class="bar"><b id="cnt">0</b>/<span id="tot">0</span> labeled
  <div class="prog"><i id="pi"></i></div></div>
<div id="list"></div>
</div>
<div class="foot"><div class="st" id="status">Loading…</div></div>
<script>
const ITEMS = {items_js};
const LABELS = {labels_js};
const SCALE = LABELS.scale || null;
const FLAGS = LABELS.flags || [];
const HAS_NOTE = !!LABELS.note;
let R = {{}};
const $ = s => document.querySelector(s);

function esc(s){{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
// Only http(s) links are rendered as clickable — blocks javascript:/data: URLs.
function safeUrl(u){{ return /^https?:\\/\\//i.test(u||'') ? u : null; }}

function isLabeled(r){{
  if(!r) return false;
  if(SCALE && r.score) return true;
  if(FLAGS.length && (r.flags||[]).length) return true;
  if(HAS_NOTE && r.note && r.note.trim()) return true;
  return false;
}}

function render(){{
  const L=$('#list'); L.innerHTML='';
  ITEMS.forEach(item=>{{
    const r=R[item.id]||{{}};
    const card=document.createElement('div');
    card.className='card'+(isLabeled(r)?' labeled':'');

    let html='';
    // Title + optional http(s) URL
    if(item.title){{
      const u=safeUrl(item.url);
      if(u) html+=`<div class="tt"><a href="${{esc(u)}}" target="_blank" rel="noopener">${{esc(item.title)}}</a></div>`;
      else html+=`<div class="tt">${{esc(item.title)}}</div>`;
    }}
    // Body renders as raw HTML by design — feed it only config you trust.
    if(item.body) html+=`<div class="body">${{item.body}}</div>`;
    // Meta
    if(item.meta) html+=`<div class="meta">${{esc(item.meta)}}</div>`;

    // Scale buttons
    if(SCALE){{
      const mn=SCALE.min||1, mx=SCALE.max||5;
      const caps=SCALE.captions||{{}};
      let btns='';
      for(let v=mn;v<=mx;v++) btns+=`<button class="${{(r.score==v)?'on':''}}" onclick="setScore('${{esc(item.id)}}',this,${{v}})">${{v}}</button>`;
      html+=`<div class="scores">${{btns}}</div>`;
      // Caption bar — only if any captions defined
      const capEntries=Object.entries(caps);
      if(capEntries.length){{
        const capSpans=[];
        for(let v=mn;v<=mx;v++){{ const c=caps[String(v)]; capSpans.push(`<span>${{esc(c||'')}}</span>`); }}
        html+=`<div class="scl">${{capSpans.join('')}}</div>`;
      }}
    }}

    // Flag toggles
    if(FLAGS.length){{
      html+='<div class="flags">';
      FLAGS.forEach(flag=>{{
        const on=((r.flags||[]).includes(flag));
        html+=`<div class="flag-btn${{on?' on':''}}" role="checkbox" aria-checked="${{on}}" tabindex="0"
          onclick="toggleFlag('${{esc(item.id)}}','${{esc(flag)}}',this)"
          onkeydown="if(event.key===' '||event.key==='Enter'){{event.preventDefault();this.click();}}">
          <input type="checkbox" ${{on?'checked':''}} tabindex="-1" aria-hidden="true"> ${{esc(flag)}}</div>`;
      }});
      html+='</div>';
    }}

    // Note
    if(HAS_NOTE) html+=`<textarea class="note" placeholder="note (optional)" oninput="setNote('${{esc(item.id)}}',this.value)">${{esc(r.note||'')}}</textarea>`;

    card.innerHTML=html;
    L.appendChild(card);
  }});
  upd();
}}

function upd(){{
  const done=ITEMS.filter(item=>isLabeled(R[item.id])).length;
  $('#cnt').textContent=done;
  $('#tot').textContent=ITEMS.length;
  $('#pi').style.width=(100*done/Math.max(ITEMS.length,1))+'%';
}}

let saveTimer=null;
function scheduleSave(){{
  clearTimeout(saveTimer);
  saveTimer=setTimeout(()=>{{
    fetch('/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{ratings:R}})}})
      .then(r=>r.ok
        ? ($('#status').textContent='Saved ✓ '+new Date().toLocaleTimeString())
        : ($('#status').textContent='save error'))
      .catch(()=>{{ $('#status').textContent='offline — kept locally'; }});
    localStorage.setItem('siftrate_'+location.pathname, JSON.stringify(R));
  }},250);
}}

function setScore(id,btn,v){{
  R[id]=R[id]||{{}};
  const prev=R[id].score;
  R[id].score=(prev==v?null:v);
  // Update button states in-place instead of a full re-render (faster on long lists)
  const card=btn.closest('.card');
  card.querySelectorAll('.scores button').forEach((b,i)=>{{
    b.className=(R[id].score==(parseInt(b.textContent)))?'on':'';
  }});
  card.className='card'+(isLabeled(R[id])?' labeled':'');
  upd(); scheduleSave();
}}

function toggleFlag(id,flag,el){{
  R[id]=R[id]||{{}};
  R[id].flags=R[id].flags||[];
  const idx=R[id].flags.indexOf(flag);
  if(idx>=0) R[id].flags.splice(idx,1);
  else R[id].flags.push(flag);
  const on=R[id].flags.includes(flag);
  el.className='flag-btn'+(on?' on':'');
  el.setAttribute('aria-checked',on);
  el.querySelector('input').checked=on;
  const card=el.closest('.card');
  card.className='card'+(isLabeled(R[id])?' labeled':'');
  upd(); scheduleSave();
}}

function setNote(id,v){{
  R[id]=R[id]||{{}};
  R[id].note=v;
  upd(); scheduleSave();
}}

// Load existing results on page open (resumable), falling back to localStorage
// if the server is unreachable.
fetch('/ratings').then(r=>r.json()).then(d=>{{
  R=d||{{}};
  render();
  $('#status').textContent='Ready';
}}).catch(()=>{{
  const local=localStorage.getItem('siftrate_'+location.pathname);
  R=local?JSON.parse(local):{{}};
  render();
  $('#status').textContent='Ready (local)';
}});
</script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class RatingHandler(BaseHTTPRequestHandler):
    # Set on the class before serving (see main()):
    cfg: dict = {}
    results_path: str = ""
    token = None
    _page: str = ""

    def _send(self, code: int, body, ctype: str = "application/json", extra_headers: dict | None = None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    # -- auth ---------------------------------------------------------------

    def _authed(self) -> bool:
        """True when no token is configured, or the request carries the token
        via bearer header, ?token= query, or the sift_token cookie."""
        tok = self.__class__.token
        if not tok:
            return True

        def eq(x):
            return hmac.compare_digest(str(x), tok)

        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and eq(auth[7:]):
            return True
        xa = self.headers.get("X-Auth-Token", "")
        if xa and eq(xa):
            return True
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if any(eq(v) for v in qs.get("token", [])):
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "sift_token" and eq(urllib.parse.unquote(v)):
                    return True
        return False

    def _cookie_header(self) -> dict:
        """Plant the token as a cookie so later same-origin fetches authenticate
        without the ?token= query string. HttpOnly (an XSS can't read it) and
        SameSite=Lax; no Secure flag because siftrate is usually plain HTTP on a
        tailnet/LAN."""
        tok = self.__class__.token
        if not tok:
            return {}
        val = urllib.parse.quote(tok, safe="")
        return {"Set-Cookie": f"sift_token={val}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"}

    def _deny(self, path: str):
        if path in ("/", "/index.html"):
            msg = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                   "<body style='font-family:-apple-system,sans-serif;max-width:32em;"
                   "margin:12vh auto;padding:0 1em;color:#16243A'>"
                   "<h2>Authentication required</h2><p>Open this page with your access "
                   "token appended, e.g.<br><code>?token=YOUR_TOKEN</code></p></body>")
            self._send(401, msg, "text/html; charset=utf-8")
        else:
            self._send(401, json.dumps({"error": "auth required"}))

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if not self._authed():
            self._deny(path)
            return
        if path in ("/", "/index.html"):
            self._send(200, self.__class__._page, "text/html; charset=utf-8", self._cookie_header())
        elif path == "/ratings":
            self._send(200, json.dumps(_read_results(self.__class__.results_path)))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            self._deny(path)
            return
        if path != "/save":
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send(400, json.dumps({"error": "bad length"}))
            return
        if n < 0 or n > MAX_BODY:
            self._send(413, json.dumps({"error": "payload too large"}))
            return
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        incoming = payload.get("ratings") if isinstance(payload, dict) else None
        if not isinstance(incoming, dict):
            self._send(400, json.dumps({"error": "expected object at .ratings"}))
            return

        # Merge with existing results — one entry per item id
        existing = _read_results(self.__class__.results_path)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for item_id, vals in incoming.items():
            if not isinstance(vals, dict):
                continue
            entry = existing.get(item_id) or {}
            if vals.get("score") is not None:
                entry["score"] = vals["score"]
            elif "score" in vals and vals["score"] is None:
                entry.pop("score", None)
            if "flags" in vals:
                entry["flags"] = vals["flags"]
            if "note" in vals:
                entry["note"] = vals["note"]
            entry["updated_at"] = now
            existing[item_id] = entry

        atomic_write(self.__class__.results_path, existing)
        labeled = sum(1 for v in existing.values() if v.get("score") or v.get("flags") or v.get("note"))
        self._send(200, json.dumps({"ok": True, "labeled": labeled}))

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass  # quiet — also keeps ?token= out of any access log


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="siftrate",
        description="Config-driven rating/labeling server — tap-to-score from your phone, results to JSON.",
    )
    parser.add_argument("--config", required=True, help="path to the config JSON (see README for the schema)")
    parser.add_argument("--port", type=int, help="port to serve on (default: config 'port', else 8091)")
    parser.add_argument("--host", help="interface to bind (default 127.0.0.1). Use 0.0.0.0 to expose on your "
                                       "LAN/tailnet — pair that with --token.")
    parser.add_argument("--token", help="require this secret on every route (bearer header, ?token= query, or "
                                        "cookie). Use whenever --host is not localhost.")
    parser.add_argument("--version", action="version", version=f"siftrate {__version__}")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    host = args.host or cfg.get("host") or DEFAULT_HOST
    port = args.port or cfg.get("port") or DEFAULT_PORT
    output = cfg["output"]
    token = args.token or None

    RatingHandler.cfg = cfg
    RatingHandler.results_path = output
    RatingHandler.token = token
    RatingHandler._page = build_page(cfg)

    Path(output).parent.mkdir(parents=True, exist_ok=True)

    try:
        httpd = ThreadingHTTPServer((host, port), RatingHandler)
    except OSError as e:
        raise SystemExit(f"siftrate: cannot bind {host}:{port} — {e}")

    n = len(cfg["items"])
    print(f"siftrate {__version__}: http://{host}:{port}/  ({n} items)  ->  {output}")
    if host == "0.0.0.0":
        print("  bound to 0.0.0.0 — open via this machine's LAN/tailnet IP, not 0.0.0.0")
    if token:
        print("  auth: token required — append ?token=... the first time you open the page")
    elif host not in LOCAL_HOSTS:
        print("  WARNING: bound past localhost without --token. Anyone who can reach this port "
              "can read and write your results. Add --token <secret> or bind 127.0.0.1.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nsiftrate: stopped.")


if __name__ == "__main__":
    main()
