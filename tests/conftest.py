from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import routes
from src.core.auth import verify_api_key_flexible


class FakeGenerationHandler:
    def __init__(self):
        self.calls = []
        self.file_cache = None
        self.non_stream_chunks = []
        self.stream_chunks = []

    async def handle_generation(self, model, prompt, images=None, stream=False):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "images": images,
                "stream": stream,
            }
        )
        chunks = self.stream_chunks if stream else self.non_stream_chunks
        for chunk in chunks:
            yield chunk


@pytest.fixture
def fake_handler():
    return FakeGenerationHandler()


@pytest.fixture
def fastapi_app(fake_handler):
    app = FastAPI()
    app.include_router(routes.router)

    async def fake_auth():
        return "test-api-key"

    app.dependency_overrides[verify_api_key_flexible] = fake_auth
    routes.set_generation_handler(fake_handler)
    yield app
    app.dependency_overrides.clear()
    routes.set_generation_handler(None)


@pytest.fixture
def client(fastapi_app):
    with TestClient(fastapi_app) as test_client:
        yield test_client
