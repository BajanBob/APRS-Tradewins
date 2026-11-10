"""Granny Martha - Android entry point (python-for-android 'webview' bootstrap).
Serves the tutor page on 127.0.0.1:8074; the WebView shows it full-screen. Works offline."""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "index.html"), "rb") as f:
    PAGE = f.read()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8074), H).serve_forever()
