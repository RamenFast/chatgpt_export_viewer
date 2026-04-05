#!/usr/bin/env python3
"""
Simple HTTP server with Range request support for audio scrubbing.
Run from the AiCleanup output directory:
    python serve.py [port]
    # Default: http://localhost:8000
"""

import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from functools import partial


class RangeHTTPRequestHandler(SimpleHTTPRequestHandler):
    """HTTP handler that supports Range requests for audio/video seeking."""

    def send_head(self):
        path = self.translate_path(self.path)

        if os.path.isdir(path):
            return super().send_head()

        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return None

        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        # Parse Range: bytes=start-end
        try:
            range_spec = range_header.strip()
            if not range_spec.startswith("bytes="):
                return super().send_head()
            range_spec = range_spec[6:]
            parts = range_spec.split("-", 1)
            file_size = os.path.getsize(path)

            if parts[0] == "":
                # bytes=-N  (last N bytes)
                suffix_len = int(parts[1])
                start = max(0, file_size - suffix_len)
                end = file_size - 1
            elif parts[1] == "":
                # bytes=N-  (from N to end)
                start = int(parts[0])
                end = file_size - 1
            else:
                # bytes=N-M
                start = int(parts[0])
                end = int(parts[1])

            if start > end or start >= file_size:
                self.send_error(416, "Requested Range Not Satisfiable")
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return None

            end = min(end, file_size - 1)
            content_length = end - start + 1

            ctype = self.guess_type(path)
            f = open(path, "rb")
            f.seek(start)

            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            return _RangeFile(f, content_length)

        except (ValueError, IndexError):
            return super().send_head()

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()

    # Persist API: handle PUT/DELETE for userdata/ files
    def do_PUT(self):
        if self.path.startswith("/userdata/"):
            self._handle_userdata_write()
        else:
            self.send_error(405, "PUT only supported for /userdata/")

    def do_DELETE(self):
        if self.path.startswith("/userdata/"):
            self._handle_userdata_delete()
        else:
            self.send_error(405, "DELETE only supported for /userdata/")

    def _handle_userdata_write(self):
        """Save user data (bookmarks, comments, etc.) to userdata/ directory."""
        path = self.translate_path(self.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        with open(path, "wb") as f:
            f.write(body)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _handle_userdata_delete(self):
        """Delete a userdata file."""
        path = self.translate_path(self.path)
        if os.path.isfile(path):
            os.remove(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


class _RangeFile:
    """Wrapper that limits reads to the requested range."""
    def __init__(self, f, length):
        self._f = f
        self._remaining = length

    def read(self, n=-1):
        if self._remaining <= 0:
            return b""
        if n < 0:
            n = self._remaining
        n = min(n, self._remaining)
        data = self._f.read(n)
        self._remaining -= len(data)
        return data

    def close(self):
        self._f.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = RangeHTTPRequestHandler
    server = HTTPServer(("", port), handler)
    print(f"Serving on http://localhost:{port}")
    print(f"  Audio scrubbing:  ENABLED (Range requests supported)")
    print(f"  User data:        ENABLED (PUT/DELETE /userdata/*)")
    print(f"  Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
