"""A publicly reachable server (tunnel) only accepts phones that carry the access key."""
import asyncio
import copy

import aiohttp
import pytest

from pc_app.config import load_config
from pc_app.server import PhoneServer


@pytest.fixture
def server(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["server"]["http_port"] = 0
    cfg["server"]["cert_dir"] = str(tmp_path / "certs")
    cfg["recording"]["sessions_dir"] = str(tmp_path / "sessions")
    s = PhoneServer(cfg, enable_https=False)
    s.start()
    s.set_public_url("https://example-8080.devtunnels.ms/", {"fps": 15})
    yield s
    s.stop()


async def _connect(port: int, query: str):
    async with aiohttp.ClientSession() as http:
        try:
            async with http.ws_connect(f"ws://127.0.0.1:{port}/ws/phone{query}") as ws:
                await ws.send_json({"type": "hello", "session_id": "keytest"})
                return (await ws.receive_json(timeout=5))["type"]
        except aiohttp.WSServerHandshakeError as e:
            return e.status


def test_phone_needs_access_key(server):
    key = server.access_key
    assert server.phone_url == f"https://example-8080.devtunnels.ms/?k={key}"
    assert asyncio.run(_connect(server.http_port, "")) == 403
    assert asyncio.run(_connect(server.http_port, "?k=wrong")) == 403
    assert asyncio.run(_connect(server.http_port, f"?k={key}")) == "welcome"


def test_access_key_is_stable_and_tunnel_defaults_served(server):
    async def get_cfg():
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{server.http_port}/config.json") as r:
                return await r.json()
    assert asyncio.run(get_cfg())["fps"] == 15
    again = PhoneServer(server.cfg, enable_https=False)
    assert again.set_public_url("https://x.example/").endswith(server.access_key)  # same key after restart
