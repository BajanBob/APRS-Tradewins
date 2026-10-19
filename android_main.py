# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Android entry point.

python-for-android's 'webview' bootstrap runs this file, waits until port
8073 answers, then shows http://127.0.0.1:8073/ full-screen in a WebView.

If anything goes wrong while starting, a tiny fallback page is served on the
same port showing the error, so it appears on the phone screen instead of a
blank page or a crash.
"""
import os
import sys
import traceback

PORT = 8073


def _show_error(text):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import html
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Tradewind error</title><body style='background:#15181b;color:#ffb547;font-family:monospace;"
            "padding:16px;font-size:13px'><h2 style='color:#ff7b70'>Tradewind could not start</h2>"
            "<p style='color:#ddd'>Please take a screenshot of this page and send it to the developers.</p>"
            "<pre style='white-space:pre-wrap;word-break:break-all'>" + html.escape(text) + "</pre>").encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    HTTPServer(("127.0.0.1", PORT), H).serve_forever()


try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.argv = ["app.py", "--no-browser", "--host", "127.0.0.1", "--port", str(PORT)]
    import app  # noqa: E402
    app.main()
except SystemExit:
    raise
except BaseException:
    err = traceback.format_exc()
    env = "\n".join(f"{k}={os.environ.get(k)}" for k in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH", "HOME"))
    try:
        _show_error(err + "\n\n" + env + f"\npython {sys.version}")
    except BaseException:
        traceback.print_exc()
