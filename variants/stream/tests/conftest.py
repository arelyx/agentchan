"""Starts a second server process inside the container on a throwaway database and tests it over real
HTTP (so the raw-request-target handling is exercised). Run: docker compose ... exec app pytest -q tests"""
import os
import random
import socket
import subprocess
import sys
import time

import httpx
import psycopg
import pytest

PORT = 18999
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session", autouse=True)
def server():
    url = os.environ["DATABASE_URL"]
    admin, _, _ = url.rpartition("/")
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS stream_test WITH (FORCE)")
        c.execute("CREATE DATABASE stream_test")
    env = dict(os.environ, DATABASE_URL=f"{admin}/stream_test", PORT=str(PORT), TRUST_XFF="1",
               RATE_IP_MIN="5", RATE_ID_MIN="4", NEW_CHANNELS_IP_DAY="2")
    p = subprocess.Popen([sys.executable, "-m", "app.server"], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(BASE + "/robots.txt").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield BASE
    p.terminate()
    p.wait(5)


def new_ip():
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


class Client:
    def __init__(self):
        self.ip = new_ip()

    def get(self, path, **kw):
        h = {"X-Forwarded-For": self.ip, **kw.pop("headers", {})}
        return httpx.get(BASE + path, headers=h, timeout=10, **kw)

    def post(self, path, **kw):
        h = {"X-Forwarded-For": self.ip, **kw.pop("headers", {})}
        return httpx.post(BASE + path, headers=h, timeout=10, **kw)

    def raw(self, target: bytes, headers=b""):
        """Send an exact request target (no client-side encoding). Returns (status, body text)."""
        s = socket.create_connection(("127.0.0.1", PORT))
        s.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: t\r\nConnection: close\r\nX-Forwarded-For: "
                  + self.ip.encode() + b"\r\n" + headers + b"\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), body.decode("utf-8", "replace")


@pytest.fixture
def cl():
    return Client()


def stored(cl, receipt_text):
    """Fetch the stored body of the message named in a receipt."""
    mid = int(receipt_text.split("id: ", 1)[1].split()[0])
    return cl.get(f"/m/{mid}.json").json()["message"]
