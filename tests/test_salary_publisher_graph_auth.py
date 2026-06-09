from __future__ import annotations

import pytest

from salaryops import salary_publisher as graph


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload=None,
        text: str = "",
        headers=None,
        content: bytes = b"",
    ):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(payload or "")
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None, json=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "json": json,
            }
        )
        if not self.responses:
            raise AssertionError("No queued fake response")
        return self.responses.pop(0)


class _FakeCache:
    instances = []

    def __init__(self):
        self.has_state_changed = False
        self.deserialized = None
        _FakeCache.instances.append(self)

    def deserialize(self, raw: str) -> None:
        self.deserialized = raw

    def serialize(self) -> str:
        return "SERIALIZED-CACHE"


def test_graph_client_silent_token_uses_persisted_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.bin"
    cache_path.write_text("CACHE-BLOB", encoding="utf-8")

    class FakeApp:
        instances = []

        def __init__(self, client_id, authority=None, token_cache=None):
            self.client_id = client_id
            self.authority = authority
            self.token_cache = token_cache
            FakeApp.instances.append(self)

        def get_accounts(self):
            return [{"id": "acct"}]

        def acquire_token_silent(self, scopes, account):
            assert "offline_access" not in scopes
            assert scopes == ["User.Read", "Mail.Send"]
            assert account == {"id": "acct"}
            return {"access_token": "tok-silent"}

        def initiate_device_flow(self, scopes):  # pragma: no cover
            raise AssertionError("device flow should not run when silent token exists")

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: _FakeSession())

    gc = graph.GraphClient(
        client_id="cid",
        authority="consumers",
        scopes=["User.Read", "Mail.Send", "offline_access"],
        token_cache_path=str(cache_path),
        interactive_auth=False,
    )

    assert gc.token == "tok-silent"
    assert gc.session.headers["Authorization"] == "Bearer tok-silent"
    assert _FakeCache.instances[-1].deserialized == "CACHE-BLOB"
    assert FakeApp.instances[-1].authority.endswith("/consumers")


def test_graph_client_noninteractive_without_cache_raises_auth_required(
    monkeypatch, tmp_path
):
    cache_path = tmp_path / "cache.bin"

    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):  # noqa: ARG002
            pass

        def get_accounts(self):
            return []

        def acquire_token_silent(self, scopes, account):  # noqa: ARG002
            return None

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: _FakeSession())

    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        graph.GraphClient(
            client_id="cid",
            authority="consumers",
            token_cache_path=str(cache_path),
            interactive_auth=False,
        )


def test_graph_client_interactive_flow_persists_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.bin"

    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):  # noqa: ARG002
            self.token_cache = token_cache

        def get_accounts(self):
            return []

        def acquire_token_silent(self, scopes, account):  # noqa: ARG002
            return None

        def initiate_device_flow(self, scopes):
            assert "offline_access" not in scopes
            assert scopes == ["User.Read", "Mail.Send"]
            self.token_cache.has_state_changed = True
            return {"user_code": "ABCD", "message": "Go sign in"}

        def acquire_token_by_device_flow(self, flow):
            assert flow["user_code"] == "ABCD"
            return {"access_token": "tok-device"}

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: _FakeSession())

    gc = graph.GraphClient(
        client_id="cid",
        authority="consumers",
        scopes=["User.Read", "Mail.Send", "profile"],
        token_cache_path=str(cache_path),
        interactive_auth=True,
    )

    assert gc.token == "tok-device"
    assert cache_path.exists()
    assert cache_path.read_text(encoding="utf-8") == "SERIALIZED-CACHE"


def test_graph_client_retries_401_with_silent_refresh(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.bin"
    session = _FakeSession(
        responses=[
            _FakeResponse(401, payload={"error": "Unauthorized"}, text="Unauthorized"),
            _FakeResponse(200, payload={"value": [{"id": "ok"}]}),
        ]
    )

    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):  # noqa: ARG002
            self._silent_calls = 0
            self._account = {"id": "acct"}

        def get_accounts(self):
            return [self._account]

        def acquire_token_silent(self, scopes, account):  # noqa: ARG002
            self._silent_calls += 1
            if self._silent_calls == 1:
                return {"access_token": "tok-initial"}
            return {"access_token": "tok-refreshed"}

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: session)

    gc = graph.GraphClient(
        client_id="cid",
        authority="consumers",
        token_cache_path=str(cache_path),
        interactive_auth=False,
    )
    response = gc.get("https://graph.microsoft.com/v1.0/me/messages")

    assert response.json()["value"][0]["id"] == "ok"
    assert gc.session.headers["Authorization"] == "Bearer tok-refreshed"
    assert len(session.calls) == 2


def test_graph_client_retries_429_using_retry_after(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.bin"
    session = _FakeSession(
        responses=[
            _FakeResponse(
                429,
                payload={"error": "too many"},
                text="Too Many",
                headers={"Retry-After": "3"},
            ),
            _FakeResponse(200, payload={"value": []}),
        ]
    )
    sleeps = []

    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):  # noqa: ARG002
            pass

        def get_accounts(self):
            return [{"id": "acct"}]

        def acquire_token_silent(self, scopes, account):  # noqa: ARG002
            return {"access_token": "tok-silent"}

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: session)
    monkeypatch.setattr(graph.time, "sleep", lambda sec: sleeps.append(sec))

    gc = graph.GraphClient(
        client_id="cid",
        authority="consumers",
        token_cache_path=str(cache_path),
        interactive_auth=False,
    )
    response = gc.get("https://graph.microsoft.com/v1.0/me/messages")

    assert response.json()["value"] == []
    assert sleeps == [3.0]
    assert len(session.calls) == 2


def test_graph_client_strips_reserved_and_duplicate_scopes(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.bin"
    captured = {}

    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):  # noqa: ARG002
            pass

        def get_accounts(self):
            return [{"id": "acct"}]

        def acquire_token_silent(self, scopes, account):  # noqa: ARG002
            captured["scopes"] = scopes
            return {"access_token": "tok-silent"}

    monkeypatch.setattr(graph.msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(graph.msal, "PublicClientApplication", FakeApp)
    monkeypatch.setattr(graph.requests, "Session", lambda: _FakeSession())

    graph.GraphClient(
        client_id="cid",
        authority="consumers",
        scopes=["User.Read", "offline_access", "Mail.Send", "mail.send", "profile"],
        token_cache_path=str(cache_path),
        interactive_auth=False,
    )

    assert captured["scopes"] == ["User.Read", "Mail.Send"]
