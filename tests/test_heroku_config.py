"""Heroku attachment swap: order, allowlist, and redaction. No network."""

from __future__ import annotations

import json
import urllib.error

from io import BytesIO
from typing import Any

import pytest

from mirroring.heroku_config import (
    HerokuConfigError,
    _default_request_json,
    assert_config_swap_allowed,
    assert_heroku_app_name,
    redact_heroku_error_body,
    resolve_heroku_app_name,
    swap_mirror_attachments,
)


APP = "reskinned-warehouse-production"
TOKEN = "platform-token-value"
PUBLISHED_PROCESS = "postgres://mirror-user:process-secret@pub.example:5432/pubdb"
SHADOW_PROCESS = "postgres://shadow-user:process-secret@shadow.example:5432/shadowdb"
PUBLISHED_CONFIG = "postgres://heroku:config-secret@pub.example:5432/pubdb"
SHADOW_CONFIG = "postgresql://heroku:other-secret@shadow.example:5432/shadowdb"
PUBLISHED_AFTER = "postgres://heroku:reattach-secret@shadow.example:5432/shadowdb"
SHADOW_AFTER = "postgres://heroku:reattach-secret@pub.example:5432/pubdb"
PRIMARY_SENTINEL = "primary-sentinel-not-for-post"
UNRELATED_SENTINEL = "unrelated-sentinel"
PUBLISHED_ADDON = "postgresql-published-1"
SHADOW_ADDON = "postgresql-shadow-2"

CONFIG_BEFORE = {
    "MIRROR_DATABASE_URL": PUBLISHED_CONFIG,
    "MIRROR_SHADOW_DATABASE_URL": SHADOW_CONFIG,
    "DATABASE_URL": f"postgres://u:{PRIMARY_SENTINEL}@primary.example:5432/live",
    "SECRET_KEY": UNRELATED_SENTINEL,
}
CONFIG_AFTER = {
    "MIRROR_DATABASE_URL": PUBLISHED_AFTER,
    "MIRROR_SHADOW_DATABASE_URL": SHADOW_AFTER,
    "DATABASE_URL": CONFIG_BEFORE["DATABASE_URL"],
    "SECRET_KEY": UNRELATED_SENTINEL,
}
ATTACHMENTS = [
    {"name": "MIRROR_DATABASE", "addon": {"name": PUBLISHED_ADDON}},
    {"name": "MIRROR_SHADOW_DATABASE", "addon": {"name": SHADOW_ADDON}},
    {"name": "DATABASE", "addon": {"name": "postgresql-primary"}},
]


def _attachments_response() -> list[dict[str, Any]]:
    return json.loads(json.dumps(ATTACHMENTS))


class _Recorder:
    def __init__(
        self,
        *,
        config_after: dict[str, str] | None = None,
        fail_at: tuple[str, str] | None = None,
        fail_posts: int = 0,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.config_after = config_after if config_after is not None else CONFIG_AFTER
        self.fail_at = fail_at
        self.fail_posts = fail_posts
        self.tokens: list[str] = []

    def __call__(self, method: str, path: str, *, payload: dict[str, str] | None, token: str) -> Any:
        recorded_payload = dict(payload) if payload is not None else None
        self.calls.append((method, path, recorded_payload))
        self.tokens.append(token)
        if method == "POST" and self.fail_posts:
            self.fail_posts -= 1
            raise HerokuConfigError("attach failed postgres://role:leak-secret@db.example:5432/appdb")
        if self.fail_at == (method, path):
            raise HerokuConfigError("boom postgres://role:leak-secret@db.example:5432/appdb")
        if method == "GET" and path.endswith("/config-vars"):
            deleted = any(item[0] == "DELETE" for item in self.calls)
            return dict(self.config_after if deleted else CONFIG_BEFORE)
        if method == "GET" and path.endswith("/addon-attachments"):
            return _attachments_response()
        return {}


def _swap(recorder: _Recorder, **overrides: str) -> None:
    kwargs = {
        "app_name": APP,
        "api_key": TOKEN,
        "published_url": PUBLISHED_PROCESS,
        "shadow_url": SHADOW_PROCESS,
        "request_json": recorder,
    }
    kwargs.update(overrides)
    swap_mirror_attachments(**kwargs)


@pytest.mark.unit
def test_swap_order_posts_only_mirror_attachments() -> None:
    recorder = _Recorder()
    _swap(recorder)

    assert recorder.calls == [
        ("GET", f"/apps/{APP}/config-vars", None),
        ("GET", f"/apps/{APP}/addon-attachments", None),
        ("DELETE", f"/apps/{APP}/addon-attachments/MIRROR_DATABASE", None),
        (
            "POST",
            f"/apps/{APP}/addon-attachments",
            {"addon": SHADOW_ADDON, "app": APP, "name": "MIRROR_DATABASE"},
        ),
        ("DELETE", f"/apps/{APP}/addon-attachments/MIRROR_SHADOW_DATABASE", None),
        (
            "POST",
            f"/apps/{APP}/addon-attachments",
            {"addon": PUBLISHED_ADDON, "app": APP, "name": "MIRROR_SHADOW_DATABASE"},
        ),
        ("GET", f"/apps/{APP}/config-vars", None),
    ]
    assert recorder.tokens == [TOKEN] * len(recorder.calls)
    for method, path, payload in recorder.calls:
        assert TOKEN not in path
        if method != "POST":
            assert payload is None
            continue
        assert payload is not None
        assert payload["name"] != "DATABASE"
        assert "DATABASE_URL" not in payload
        assert set(payload) == {"addon", "app", "name"}
        encoded = json.dumps(payload)
        assert "DATABASE_URL" not in encoded
        assert PRIMARY_SENTINEL not in encoded
        assert UNRELATED_SENTINEL not in encoded
        assert TOKEN not in encoded


@pytest.mark.unit
def test_published_url_drift_refuses_before_delete() -> None:
    drifted = "postgres://u:unique-drift-secret@drift.example:5432/pubdb"

    def request_json(method: str, path: str, *, payload: dict[str, str] | None, token: str) -> dict[str, str]:
        assert method == "GET"
        assert path.endswith("/config-vars")
        assert token == TOKEN
        return {
            "MIRROR_DATABASE_URL": drifted,
            "MIRROR_SHADOW_DATABASE_URL": SHADOW_CONFIG,
            "DATABASE_URL": CONFIG_BEFORE["DATABASE_URL"],
        }

    with pytest.raises(HerokuConfigError, match="MIRROR_DATABASE_URL") as exc_info:
        swap_mirror_attachments(
            app_name=APP,
            api_key=TOKEN,
            published_url=PUBLISHED_PROCESS,
            shadow_url=SHADOW_PROCESS,
            request_json=request_json,
        )
    message = str(exc_info.value)
    assert "unique-drift-secret" not in message
    assert "drift.example" not in message
    assert TOKEN not in message


@pytest.mark.unit
def test_assert_config_swap_allowed_rejects_database_url() -> None:
    with pytest.raises(HerokuConfigError, match="DATABASE_URL"):
        assert_config_swap_allowed("DATABASE_URL", "MIRROR_SHADOW_DATABASE_URL")
    with pytest.raises(HerokuConfigError, match="DATABASE_URL"):
        assert_config_swap_allowed("MIRROR_DATABASE_URL", "DATABASE_URL")
    assert_config_swap_allowed("MIRROR_DATABASE_URL", "MIRROR_SHADOW_DATABASE_URL")


@pytest.mark.unit
def test_unsafe_app_name_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(HerokuConfigError, match=r"app/\.\./\.\./other"):
        assert_heroku_app_name("app/../../other")
    with pytest.raises(HerokuConfigError, match=r"app/\.\./\.\./other"):
        resolve_heroku_app_name({"MIRROR_HEROKU_APP_NAME": "app/../../other", "HEROKU_APP_NAME": "safe-app"})
    assert resolve_heroku_app_name({"MIRROR_HEROKU_APP_NAME": "warehouse-prod", "HEROKU_APP_NAME": "other-app"}) == (
        "warehouse-prod"
    )
    assert resolve_heroku_app_name({"HEROKU_APP_NAME": "other-app"}) == "other-app"
    assert assert_heroku_app_name("reskinned-warehouse-production") == "reskinned-warehouse-production"
    with pytest.raises(HerokuConfigError):
        assert_heroku_app_name("reskinned-warehouse-productionx")

    monkeypatch.setenv("MIRROR_HEROKU_APP_NAME", "from-override")
    monkeypatch.setenv("HEROKU_APP_NAME", "from-dyno")
    assert resolve_heroku_app_name() == "from-override"


@pytest.mark.unit
def test_redact_heroku_error_body_hides_urls_and_token() -> None:
    body = "failed postgres://role:s3cret@db.example:5432/appdb and postgresql://a:b@h/d Bearer platform-token-value"
    redacted = redact_heroku_error_body(body, token=TOKEN)
    assert "s3cret" not in redacted
    assert "postgresql://" not in redacted
    assert "postgres://***" in redacted
    assert TOKEN not in redacted
    assert "Bearer ***" in redacted


@pytest.mark.unit
def test_attach_failure_reattaches_original_published_addon() -> None:
    recorder = _Recorder(fail_posts=1)
    with pytest.raises(HerokuConfigError, match="attach failed") as exc_info:
        _swap(recorder)
    assert "leak-secret" not in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)
    posts = [call for call in recorder.calls if call[0] == "POST"]
    assert [post[2]["name"] for post in posts] == ["MIRROR_DATABASE", "MIRROR_DATABASE"]
    assert posts[0][2]["addon"] == SHADOW_ADDON
    assert posts[1][2]["addon"] == PUBLISHED_ADDON
    assert not any(call[0] == "DELETE" and call[1].endswith("MIRROR_SHADOW_DATABASE") for call in recorder.calls)


@pytest.mark.unit
def test_shadow_park_failure_does_not_undo_publish() -> None:
    recorder = _Recorder(fail_at=("DELETE", f"/apps/{APP}/addon-attachments/MIRROR_SHADOW_DATABASE"))
    with pytest.raises(HerokuConfigError, match="now MIRROR_DATABASE") as exc_info:
        _swap(recorder)
    message = str(exc_info.value)
    assert "needs to be attached as MIRROR_SHADOW_DATABASE" in message
    assert "not undone" in message
    assert "leak-secret" not in message
    assert TOKEN not in message
    assert [call[0] for call in recorder.calls] == ["GET", "GET", "DELETE", "POST", "DELETE"]
    assert recorder.calls[3][2]["name"] == "MIRROR_DATABASE"
    assert recorder.calls[3][2]["addon"] == SHADOW_ADDON


@pytest.mark.unit
def test_same_addon_or_missing_attachment_refuses_before_delete() -> None:
    def same_addon(method: str, path: str, *, payload: dict[str, str] | None, token: str) -> Any:
        assert token == TOKEN
        if path.endswith("/config-vars"):
            return dict(CONFIG_BEFORE)
        return [
            {"name": "MIRROR_DATABASE", "addon": {"name": "postgresql-same"}},
            {"name": "MIRROR_SHADOW_DATABASE", "addon": {"name": "postgresql-same"}},
        ]

    with pytest.raises(HerokuConfigError, match="same addon"):
        swap_mirror_attachments(
            app_name=APP,
            api_key=TOKEN,
            published_url=PUBLISHED_PROCESS,
            shadow_url=SHADOW_PROCESS,
            request_json=same_addon,
        )

    def missing_shadow(method: str, path: str, *, payload: dict[str, str] | None, token: str) -> Any:
        if method == "DELETE":
            raise AssertionError("DELETE must not run when an attachment is missing")
        if path.endswith("/config-vars"):
            return dict(CONFIG_BEFORE)
        return [{"name": "MIRROR_DATABASE", "addon": {"name": PUBLISHED_ADDON}}]

    with pytest.raises(HerokuConfigError, match="MIRROR_SHADOW_DATABASE is missing"):
        swap_mirror_attachments(
            app_name=APP,
            api_key=TOKEN,
            published_url=PUBLISHED_PROCESS,
            shadow_url=SHADOW_PROCESS,
            request_json=missing_shadow,
        )


@pytest.mark.unit
def test_default_request_uses_fixed_host_and_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request: Any, timeout: int = 0) -> _Response:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["authorization"] = request.get_header("Authorization")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        captured["data"] = request.data
        return _Response()

    monkeypatch.setattr("mirroring.heroku_config.urllib.request.urlopen", fake_urlopen)
    payload = {"addon": SHADOW_ADDON, "app": "demo-app", "name": "MIRROR_DATABASE"}
    body = _default_request_json("POST", "/apps/demo-app/addon-attachments", payload=payload, token=TOKEN)
    assert body == {"ok": True}
    assert captured["url"] == "https://api.heroku.com/apps/demo-app/addon-attachments"
    assert captured["method"] == "POST"
    assert captured["authorization"] == f"Bearer {TOKEN}"
    assert captured["accept"] == "application/vnd.heroku+json; version=3"
    assert captured["timeout"] == 60
    assert TOKEN not in captured["url"]
    assert json.loads(captured["data"]) == payload


@pytest.mark.unit
def test_http_error_redacts_database_url_and_omits_token(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"message":"bad postgres://role:s3cret@db.example:5432/appdb","token":"platform-token-value"}'

    def boom(request: Any, timeout: int = 0) -> Any:
        raise urllib.error.HTTPError(
            request.full_url,
            422,
            "Unprocessable Entity",
            hdrs=None,
            fp=BytesIO(body),
        )

    monkeypatch.setattr("mirroring.heroku_config.urllib.request.urlopen", boom)
    with pytest.raises(HerokuConfigError, match="postgres://\\*\\*\\*") as exc_info:
        _default_request_json("GET", "/apps/demo-app/config-vars", payload=None, token=TOKEN)
    message = str(exc_info.value)
    assert "s3cret" not in message
    assert TOKEN not in message
    assert exc_info.value.__cause__ is None


@pytest.mark.unit
def test_default_request_refuses_off_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("urlopen should not run")

    monkeypatch.setattr("mirroring.heroku_config.urllib.request.urlopen", boom)
    with pytest.raises(HerokuConfigError, match="api.heroku.com"):
        _default_request_json("GET", "https://evil.example/steal", payload=None, token=TOKEN)
