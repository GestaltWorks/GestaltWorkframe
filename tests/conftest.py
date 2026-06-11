from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)


class _RecordedPost:
    def __init__(self, url, json, headers):
        self.url = url
        self.json = json
        self.headers = headers


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    def __init__(self, recorder, **_kwargs) -> None:
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def post(self, url, json=None, headers=None, **_kwargs):
        if self._recorder.raise_exc:
            raise self._recorder.raise_exc
        self._recorder.append(_RecordedPost(url, json, headers))
        return _FakeResponse(self._recorder.status_code)


@pytest.fixture
def fake_httpx_post(monkeypatch):
    """Patch ``httpx.AsyncClient`` on a module to record POSTs without network calls.

    Returns a function ``patch(module) -> recorder``. The recorder is a list of
    posted requests; set ``recorder.status_code`` or ``recorder.raise_exc`` to
    simulate webhook failures.
    """

    class _Recorder(list):
        status_code = 200
        raise_exc = None

    def _patch(httpx_module):
        recorder = _Recorder()
        monkeypatch.setattr(
            httpx_module, "AsyncClient", lambda *a, **k: _FakeAsyncClient(recorder, **k)
        )
        return recorder

    return _patch
