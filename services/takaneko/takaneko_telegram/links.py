"""Serve live producer capabilities behind the existing HTTPS route."""
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import re
import shutil
import socketserver
from urllib.parse import quote, urlsplit

from media_bot.local_socket import reserve

from . import takaneko_feed as feed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with feed.deployment_guard() as available:
            if not available:
                self.send_error(503)
                return
            self.serve_link()

    def serve_link(self):
        prefix = urlsplit(feed.c.LINK_BASE).path
        match = re.fullmatch(re.escape(prefix) + '([A-Za-z0-9_-]{43})', self.path)
        try:
            item = feed.download_link(match[1]) if match else None
            if not item:
                self.send_error(404)
                return
            stream = item['path'].open('rb')
        except Exception:
            self.send_error(503)
            return
        with stream:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(item['name'], safe=''))
            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile, 256 * 1024)


class Server(socketserver.ThreadingUnixStreamServer):
    def handle_error(self, request, client_address):
        pass


def main():
    feed.c.validate_link_base()
    path = Path(os.getenv('TAKANEKO_LINK_SOCKET', '/run/telegram-takaneko-links/web.sock'))
    with reserve(path), Server(str(path), Handler) as server:
        path.chmod(0o660)
        server.serve_forever()
