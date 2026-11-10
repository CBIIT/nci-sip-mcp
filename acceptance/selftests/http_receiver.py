"""Owned loopback receiver with physical wire observations and scripted raw replies."""

from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread


class WireReceiver(HTTPServer):
    """Keep TCP acceptance separate from complete HTTP request observations."""

    def __init__(self, replies=()):
        super().__init__(("127.0.0.1", 0), WireHandler)
        self.replies = deque(replies)
        self.requests = []
        self.contacts = 0
        self.failures = []
        self.thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01})

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    def get_request(self):
        connection, address = super().get_request()
        self.contacts += 1
        connection.settimeout(1)
        return connection, address

    def handle_error(self, request, client_address):
        self.failures.append("owned HTTP receiver failed")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()
        assert self.failures == []


class WireHandler(BaseHTTPRequestHandler):
    server: WireReceiver

    def log_message(self, format, *args):
        """The test owns observations; do not print request paths or header values."""

    def do_GET(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(
            (self.command, self.path, tuple(self.headers.raw_items()), self.rfile.read(length))
        )
        reply = (
            self.server.replies.popleft()
            if self.server.replies
            else b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{"ok": true}'
        )
        try:
            if callable(reply):
                reply(self)
                return
            self.wfile.write(reply)
        except BrokenPipeError, ConnectionResetError:
            # A deliberate client refusal can close before the scripted response finishes.
            return
        finally:
            self.close_connection = True

    def do_POST(self):
        self.do_GET()
