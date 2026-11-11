"""Owned loopback receiver with physical wire observations and scripted raw replies."""

import ssl
from collections import deque
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def tls_certificate(directory, *, address="127.0.0.1"):
    """Generate only synthetic loopback trust, with no user key material or trust changes."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "owned-loopback")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address(address))]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = directory / "synthetic-key.pem", directory / "synthetic-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path


class WireReceiver(HTTPServer):
    """Keep TCP acceptance separate from complete HTTP request observations."""

    def __init__(self, replies=(), *, tls=None):
        super().__init__(("127.0.0.1", 0), WireHandler)
        self.replies = deque(replies)
        self.requests = []
        self.contacts = 0
        self.failures = []
        self.tls = tls
        self.thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01})

    @property
    def origin(self):
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.server_port}"

    def get_request(self):
        connection, address = super().get_request()
        self.contacts += 1
        connection.settimeout(1)
        if self.tls is not None:
            try:
                connection = self.tls.wrap_socket(connection, server_side=True)
            except OSError:
                connection.close()
                raise
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
