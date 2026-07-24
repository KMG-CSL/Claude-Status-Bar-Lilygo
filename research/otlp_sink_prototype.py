import json, gzip, http.server, threading, sys

OUT = "/private/tmp/claude-1668688456/-Users-u0145206-projects-Claude-Status-Bar-Lilygo/6d9f0109-4dd0-4d50-94a6-4652fb2b8ff7/scratchpad/otlp_events.jsonl"

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        rec = {"path": self.path, "ct": self.headers.get("Content-Type")}
        try:
            rec["json"] = json.loads(body)
        except Exception:
            rec["raw_len"] = len(body)
        with open(OUT, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *a): pass

http.server.ThreadingHTTPServer(("127.0.0.1", 4318), H).serve_forever()
