"""Tiny demo service: answers 200 on every path, including /health."""
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok\n")


HTTPServer(("127.0.0.1", 8104), Handler).serve_forever()
