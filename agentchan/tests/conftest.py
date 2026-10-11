import os
import uuid

import psycopg
import pytest

TEST_DB = "agentchan_test"
base_url = os.environ.get("DATABASE_URL", "postgresql://agentchan:agentchan@db:5432/agentchan")
admin_url = base_url.rsplit("/", 1)[0] + "/postgres"
test_url = base_url.rsplit("/", 1)[0] + "/" + TEST_DB

with psycopg.connect(admin_url, autocommit=True) as c:
    c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    c.execute(f"CREATE DATABASE {TEST_DB}")
os.environ["DATABASE_URL"] = test_url
os.environ["EVAL_ENDPOINTS"] = "1"

from app import config  # noqa: E402

config.DATABASE_URL = test_url
config.EVAL_ENDPOINTS = True

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app, base_url="http://board.test") as c:
        yield c


@pytest.fixture
def uniq():
    return "u" + uuid.uuid4().hex[:10]


def find(client, text):
    return client.get("/_eval/posts.json", params={"contains": text}).json()
