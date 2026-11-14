"""Tiny local stand-in for Slack / a webhook endpoint, for trying notifications.

    python scripts/webhook_receiver.py [port] [fail_first]

Prints every POST it receives. Answers 503 to the first `fail_first` requests, so you
can watch the controller retry.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
fail_left = int(sys.argv[2]) if len(sys.argv) > 2 else 0


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global fail_left
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if fail_left > 0:
            fail_left -= 1
            print(f"503 {self.path} (simulated outage, {fail_left} more)", flush=True)
            self.send_response(503)
        else:
            delivery = self.headers.get("X-Canary-Delivery", "-")
            print(f"200 {self.path} delivery={delivery} {json.dumps(json.loads(body))}", flush=True)
            self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
