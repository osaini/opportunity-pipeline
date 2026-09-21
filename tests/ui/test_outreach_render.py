"""The headless-browser fallback for company sites built with JavaScript, in a real Chromium.

The page is served from this machine, but the browser is told it lives at a
public-looking host (shell.test) so the request guard lets it load. Everything
else the page asks for must still pass the guard: the loopback address it tries
to reach directly is refused, and images are never fetched.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opportunity_app.outreach_render import PlaywrightRenderer

PAGE = """<!doctype html><html><body><div id="root"></div><img src="/pixel.png">
<script>
  document.getElementById("root").innerHTML = "<p>Headquartered in Austin, TX.</p>";
  fetch("/api/team").catch(() => {});
  fetch("http://127.0.0.1:{port}/private").catch(() => {});
</script></body></html>"""


def serve():
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            requested.append(self.path)
            body = PAGE.replace("{port}", str(self.server.server_port)).encode() if self.path == "/" else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/" else "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requested


def render_on_own_thread(url, port):
    """Playwright's sync API cannot share a thread with the suite's own browser."""
    outcome = {}

    def work():
        renderer = PlaywrightRenderer(
            resolve=lambda host: ["93.184.216.34"] if host == "shell.test" else ["127.0.0.1"],
            launch_args=[f"--host-resolver-rules=MAP shell.test:{port} 127.0.0.1:{port}"],
        )
        with renderer:
            outcome["page"] = renderer.render(url)
            outcome["blocked"] = list(renderer.blocked)
            outcome["unavailable"] = renderer.unavailable

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(60)
    return outcome


def test_a_javascript_site_is_read_and_the_page_cannot_reach_this_machine():
    server, requested = serve()
    try:
        port = server.server_port
        outcome = render_on_own_thread(f"http://shell.test:{port}/", port)
    finally:
        server.shutdown()
    assert not outcome.get("unavailable"), outcome.get("unavailable")
    final_url, html = outcome["page"]
    assert final_url == f"http://shell.test:{port}/"
    assert "Headquartered in Austin, TX." in html, "the text the script wrote is what gets read"
    assert "/api/team" in requested, "the page's own requests to its public host go through"
    assert "/private" not in requested, "a request straight to a loopback address is refused"
    assert any(url.endswith("/private") for url in outcome["blocked"])
    assert "/pixel.png" not in requested, "images are never loaded"
