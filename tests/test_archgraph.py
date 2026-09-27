"""Tests for the local server of archgraph.py."""

import socket
import threading
import urllib.request

from archgraph import create_server


def _get(server, path: str) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}") as response:
        return response.read()


def test_server_serves_frontend_and_result_from_elsewhere(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text('{"units": {}}', encoding="utf-8")
    server = create_server(result_path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _get(server, "/result.json") == b'{"units": {}}'
        assert _get(server, "/result.json?v=1") == b'{"units": {}}'
        assert b"<html" in _get(server, "/index.html")
    finally:
        server.shutdown()
        server.server_close()


def test_server_falls_back_to_free_port_when_taken(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        server = create_server(tmp_path / "result.json", taken)
        try:
            assert server.server_address[1] != taken
        finally:
            server.server_close()
