"""The Synergy login window: the operating system's own web view, in a helper process.

Synergy's login runs an invisible reCAPTCHA score check. A browser driven by
automation tooling announces itself (navigator.webdriver) and fails it; the
system web view (WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux) is
an ordinary browser engine and passes. So the person logs in here, on
Synergy's real page, and the app never sees their password.

A web view must own its process's main thread, and the app's main thread runs
the web server, so this runs as a separate process started by
`browser_client.SynergyBrowserClient`. They talk over stdin/stdout, one JSON
object per line:

  app -> host   {"op": "post", "id": 7, "message": "<aura message json>"}
                {"op": "hide"} | {"op": "close"}
  host -> app   {"event": "ready"}       window is up
                {"event": "logged_in"}   the portal app is running (session ready)
                {"event": "captcha", "score": 0.9, "passed": true}
                {"event": "closed"}      the person closed the window
                {"id": 7, "status": 200, "text": "..."}   reply to a post

Data calls run inside the logged-in page (same-origin fetch), so the session
cookie and Aura token never leave the web view.
"""

import json
import os
import sys
import threading

PORTAL_URL = "https://my.synergy.net.au/s/"
TITLE = "Log in to Synergy"

# Watches the page's own Aura requests for the logged-in portal app's token and
# context (kept in page memory only), and reports the login bot-check result.
# Never reads form fields.
HOOK_JS = r"""
(() => {
  if (window.__synergyHook) return; window.__synergyHook = true;
  const report = o => { try { window.pywebview.api.event(JSON.stringify(o)); } catch (e) {} };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = String(u || ''); return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (typeof body === 'string' && this.__u.includes('/sfsites/aura')) {
        const p = new URLSearchParams(body);
        const ctx = p.get('aura.context') || '', tok = p.get('aura.token');
        if (ctx.includes('siteforce:communityApp') && tok) {
          const first = !window.__synergyAuth;
          window.__synergyAuth = {ctx, tok, page: p.get('aura.pageURI') || '/s/'};
          if (first) report({event: 'logged_in'});
        }
        if ((p.get('message') || '').includes('MyAccount_Captcha')) {
          this.addEventListener('load', () => {
            const t = this.responseText || '';
            const m = t.match(/score\\?"\s*:\s*([0-9.]+)/);
            report({event: 'captcha', score: m ? Number(m[1]) : null,
                    passed: !/\\?"error\\?"\s*:\s*true/.test(t)});
          });
        }
      }
    } catch (e) {}
    return send.apply(this, arguments);
  };
})();
"""

# One Aura POST from inside the page. %s is a JSON object {id, message}.
# `void`: return at once (the reply comes back through the bridge), so several
# requests can be in flight together.
POST_JS = r"""
void (async (req) => {
  const reply = (status, text) => window.pywebview.api.reply(req.id, status, text);
  const a = window.__synergyAuth;
  if (!a) { reply(0, 'not_logged_in'); return; }
  try {
    const body = new URLSearchParams({message: req.message, 'aura.context': a.ctx,
                                      'aura.token': a.tok, 'aura.pageURI': a.page});
    const r = await fetch('/s/sfsites/aura?r=' + req.id + '&aura.ApexAction.execute=1',
                          {method: 'POST', body, credentials: 'include'});
    reply(r.status, await r.text());
  } catch (e) {
    reply(-1, String(e));
  }
})(%s);
"""


class _Bridge:
    """Called from page JavaScript (pywebview js_api)."""

    def __init__(self, emit):
        self._emit = emit

    def event(self, payload):
        self._emit(json.loads(payload))

    def reply(self, request_id, status, text):
        self._emit({"id": request_id, "status": status, "text": text})


# Set by PyInstaller's GTK runtime hooks to point at bundled GTK files. The
# Linux binary uses the system's GTK/WebKitGTK instead (see the .spec), so
# these must not redirect it.
_BUNDLED_GTK_ENV = ("GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR",
                    "GTK_PATH", "GTK_DATA_PREFIX", "GTK_EXE_PREFIX", "GIO_MODULE_DIR",
                    "GSETTINGS_SCHEMA_DIR", "GTK_IM_MODULE_FILE")


def use_system_gtk() -> None:
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        return
    for name in _BUNDLED_GTK_ENV:
        os.environ.pop(name, None)
    # The hooks also put the bundle first in XDG_DATA_DIRS; drop it so GTK
    # finds the system's settings schemas and themes.
    bundle = getattr(sys, "_MEIPASS", None)
    dirs = [d for d in os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep)
            if d and not (bundle and d.startswith(bundle))]
    if dirs:
        os.environ["XDG_DATA_DIRS"] = os.pathsep.join(dirs)
    else:
        os.environ.pop("XDG_DATA_DIRS", None)  # GTK then uses the standard folders


def main() -> None:
    use_system_gtk()
    import webview

    # Keep the protocol on the real stdout; anything else that prints
    # (pywebview, native libraries) goes to stderr instead.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    lock = threading.Lock()

    def emit(obj):
        with lock:
            proto.write(json.dumps(obj) + "\n")
            proto.flush()

    from .cli import app_dir
    storage = app_dir() / "webview"  # cookies/history persist like a normal browser
    storage.mkdir(parents=True, exist_ok=True)

    hidden = os.environ.get("SYNERGY_LOGIN_HEADLESS") == "1"  # automated checks only
    window = webview.create_window(TITLE, PORTAL_URL, js_api=_Bridge(emit),
                                   width=1000, height=820, hidden=hidden)
    window.events.loaded += lambda: window.evaluate_js(HOOK_JS)
    window.events.closed += lambda: emit({"event": "closed"})

    def serve():
        emit({"event": "ready"})
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            op = cmd.get("op")
            if op == "post":
                window.evaluate_js(POST_JS % json.dumps({"id": cmd["id"], "message": cmd["message"]}))
            elif op == "hide":
                window.hide()
            elif op == "close":
                break
        # Asked to close, or the app went away (stdin closed).
        window.destroy()

    gui = "gtk" if sys.platform.startswith("linux") else None  # not Qt first
    webview.start(serve, gui=gui, private_mode=False, storage_path=str(storage))


if __name__ == "__main__":
    main()
