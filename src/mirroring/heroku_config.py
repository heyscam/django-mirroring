"""Swap Heroku Postgres attachments for a mirror refresh.

Heroku roles have ``rolcreatedb = false`` and cannot connect to the ``postgres``
or ``template1`` databases, so refresh cannot ``CREATE DATABASE``. When
``MIRROR_SHADOW_DATABASE_URL`` is set, the command loads that second addon and
cuts over by swapping the ``MIRROR_DATABASE`` attachment onto it.

``DATABASE_URL`` is never a legal side of the swap. Config-var bodies are not
logged: a ``GET /config-vars`` response contains every secret on the app.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from mirroring.management.postgres_clone import database_identity


HEROKU_API_HOST = "api.heroku.com"
HEROKU_ACCEPT = "application/vnd.heroku+json; version=3"
PUBLISHED_CONFIG_VAR = "MIRROR_DATABASE_URL"
SHADOW_CONFIG_VAR = "MIRROR_SHADOW_DATABASE_URL"
PUBLISHED_ATTACHMENT = "MIRROR_DATABASE"
SHADOW_ATTACHMENT = "MIRROR_SHADOW_DATABASE"
# Heroku app names are at most 30 characters. ``reskinned-warehouse-production`` fits.
_SAFE_HEROKU_APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
_SAFE_ADDON_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")
_POSTGRES_URL_RE = re.compile(r"postgres(?:ql)?://[^\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_ALLOWED_ATTACHMENT_NAMES = frozenset({PUBLISHED_ATTACHMENT, SHADOW_ATTACHMENT})

# ``request_json(method, path, *, payload, token) -> decoded JSON``.
HerokuRequestJson = Callable[..., Any]


class HerokuConfigError(Exception):
    """The attachment swap was refused or the Heroku API call failed.

    Messages must not include API tokens, config-var payloads, or database URLs.
    """


def assert_heroku_app_name(app_name: str) -> str:
    """Return ``app_name`` when it is a safe Heroku app name."""
    if not isinstance(app_name, str) or _SAFE_HEROKU_APP_NAME.fullmatch(app_name) is None:
        raise HerokuConfigError(
            f"Refusing unsafe Heroku app name {app_name!r}. "
            "MIRROR_HEROKU_APP_NAME or HEROKU_APP_NAME must match ^[a-z][a-z0-9-]{0,29}$."
        )
    return app_name


def resolve_heroku_app_name(environ: Mapping[str, str] | None = None) -> str:
    """Return ``MIRROR_HEROKU_APP_NAME`` when set, otherwise ``HEROKU_APP_NAME``."""
    env = os.environ if environ is None else environ
    override = env.get("MIRROR_HEROKU_APP_NAME", "")
    if isinstance(override, str) and override.strip():
        raw = override.strip()
    else:
        fallback = env.get("HEROKU_APP_NAME", "")
        raw = fallback.strip() if isinstance(fallback, str) else ""
    if not raw:
        raise HerokuConfigError("Heroku app name is not set. Set MIRROR_HEROKU_APP_NAME or HEROKU_APP_NAME.")
    return assert_heroku_app_name(raw)


def assert_config_swap_allowed(published_config_var: str, shadow_config_var: str) -> None:
    """Raise unless the pair is exactly the two mirror config vars.

    ``DATABASE_URL`` is never legal: swapping it would replace the live primary.
    """
    if published_config_var == "DATABASE_URL" or shadow_config_var == "DATABASE_URL":
        raise HerokuConfigError(
            "Refusing to swap DATABASE_URL. The only allowed pair is "
            "MIRROR_DATABASE_URL with MIRROR_SHADOW_DATABASE_URL."
        )
    if (published_config_var, shadow_config_var) != (PUBLISHED_CONFIG_VAR, SHADOW_CONFIG_VAR):
        raise HerokuConfigError(
            f"Refusing config swap of {published_config_var!r} with {shadow_config_var!r}. "
            "The only allowed pair is MIRROR_DATABASE_URL with MIRROR_SHADOW_DATABASE_URL."
        )


def redact_heroku_error_body(body: str, *, token: str | None = None) -> str:
    """Replace Postgres URLs and the API token so an error body is safe to raise."""
    redacted = _POSTGRES_URL_RE.sub("postgres://***", body)
    redacted = _BEARER_RE.sub("Bearer ***", redacted)
    if token and len(token) >= 8:
        redacted = redacted.replace(token, "***")
    return redacted


def swap_mirror_attachments(
    *,
    app_name: str,
    api_key: str,
    published_url: str,
    shadow_url: str,
    request_json: HerokuRequestJson | None = None,
) -> None:
    """Swap ``MIRROR_DATABASE`` onto the loaded addon and park the old one as the shadow.

    ``request_json`` defaults to the Platform API. Tests inject a fake so this
    never touches the network. Passwords in the returned config vars may differ
    after reattach; only host/port/database are compared.

    If attaching the loaded addon fails after ``MIRROR_DATABASE`` was detached,
    the original addon is reattached before raising. If parking the previous
    mirror as ``MIRROR_SHADOW_DATABASE`` fails after the publish succeeded, the
    publish is left in place.
    """
    app = assert_heroku_app_name(app_name)
    token = api_key.strip() if isinstance(api_key, str) else ""
    if not token:
        raise HerokuConfigError("HEROKU_API_KEY is not set or empty.")
    assert_config_swap_allowed(PUBLISHED_CONFIG_VAR, SHADOW_CONFIG_VAR)
    published_identity = database_identity(published_url)
    loaded_identity = database_identity(shadow_url)
    if published_identity == loaded_identity:
        raise HerokuConfigError(
            "MIRROR_DATABASE_URL and MIRROR_SHADOW_DATABASE_URL resolve to the same host/port/database."
        )

    transport = request_json or _default_request_json

    def call(method: str, path: str, *, payload: dict[str, str] | None) -> Any:
        return _invoke(transport, method, path, payload=payload, token=token)

    # Do not log config-var bodies. They contain every secret on the app.
    before = call("GET", f"/apps/{app}/config-vars", payload=None)
    _require_config_identity(before, PUBLISHED_CONFIG_VAR, published_identity, when="before swap")
    _require_config_identity(before, SHADOW_CONFIG_VAR, loaded_identity, when="before swap")

    attachments = call("GET", f"/apps/{app}/addon-attachments", payload=None)
    published_addon = _addon_name(attachments, PUBLISHED_ATTACHMENT)
    shadow_addon = _addon_name(attachments, SHADOW_ATTACHMENT)
    if published_addon == shadow_addon:
        raise HerokuConfigError("MIRROR_DATABASE and MIRROR_SHADOW_DATABASE are the same addon. Refusing to swap.")

    call("DELETE", f"/apps/{app}/addon-attachments/{PUBLISHED_ATTACHMENT}", payload=None)
    try:
        _post_attachment(call, app=app, addon=shadow_addon, name=PUBLISHED_ATTACHMENT)
    except Exception as exc:
        try:
            _post_attachment(call, app=app, addon=published_addon, name=PUBLISHED_ATTACHMENT)
        except Exception as rollback_exc:
            rollback_text = redact_heroku_error_body(str(rollback_exc), token=token)
            raise HerokuConfigError(
                "Detached MIRROR_DATABASE and failed to attach the loaded addon. "
                "Reattaching the original published addon as MIRROR_DATABASE also failed. "
                f"Attach addon {published_addon!r} as MIRROR_DATABASE before retrying. "
                f"{rollback_text}"
            ) from None
        redacted = redact_heroku_error_body(str(exc), token=token)
        if isinstance(exc, HerokuConfigError) and redacted == str(exc):
            raise
        raise HerokuConfigError(redacted) from None

    # The loaded database is already MIRROR_DATABASE. Do not detach it if the
    # previous mirror cannot be parked as MIRROR_SHADOW_DATABASE.
    try:
        call("DELETE", f"/apps/{app}/addon-attachments/{SHADOW_ATTACHMENT}", payload=None)
        _post_attachment(call, app=app, addon=published_addon, name=SHADOW_ATTACHMENT)
    except Exception as exc:
        detail = redact_heroku_error_body(str(exc), token=token)
        raise HerokuConfigError(
            "The loaded database is now MIRROR_DATABASE, and the previous mirror still "
            "needs to be attached as MIRROR_SHADOW_DATABASE. "
            f"Attach addon {published_addon!r} as MIRROR_SHADOW_DATABASE. "
            "The publish was not undone. "
            f"{detail}"
        ) from None

    after = call("GET", f"/apps/{app}/config-vars", payload=None)
    _require_config_identity(after, PUBLISHED_CONFIG_VAR, loaded_identity, when="after swap")
    _require_config_identity(after, SHADOW_CONFIG_VAR, published_identity, when="after swap")


def _require_config_identity(config: Any, key: str, expected: tuple[str, str, str], *, when: str) -> None:
    if not isinstance(config, dict):
        raise HerokuConfigError(f"Heroku config-vars response was not an object ({when}).")
    raw = config.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise HerokuConfigError(f"Heroku config var {key} is missing ({when}).")
    if database_identity(raw) != expected:
        raise HerokuConfigError(
            f"Heroku config var {key} does not match the process URL by host/port/database ({when}). "
            "Refusing to continue. Config-var values were not logged."
        )


def _addon_name(attachments: Any, attachment_name: str) -> str:
    if attachment_name not in _ALLOWED_ATTACHMENT_NAMES:
        raise HerokuConfigError(f"Refusing attachment name {attachment_name!r}.")
    if not isinstance(attachments, list):
        raise HerokuConfigError("Heroku addon-attachments response was not a list.")
    found: str | None = None
    for item in attachments:
        if not isinstance(item, dict) or item.get("name") != attachment_name:
            continue
        addon = item.get("addon")
        addon_name = addon.get("name") if isinstance(addon, dict) else None
        if not isinstance(addon_name, str) or _SAFE_ADDON_NAME.fullmatch(addon_name) is None:
            raise HerokuConfigError(f"Attachment {attachment_name} has an unsafe or missing addon name.")
        if found is not None:
            raise HerokuConfigError(f"Attachment {attachment_name} is ambiguous.")
        found = addon_name
    if found is None:
        raise HerokuConfigError(f"Attachment {attachment_name} is missing.")
    return found


def _attachment_payload(*, addon: str, app: str, name: str) -> dict[str, str]:
    if name not in _ALLOWED_ATTACHMENT_NAMES or name == "DATABASE":
        raise HerokuConfigError(f"Refusing attachment name {name!r}.")
    payload = {"addon": addon, "app": app, "name": name}
    if "DATABASE_URL" in payload or "DATABASE" in payload.values():
        raise HerokuConfigError("Refusing to create a DATABASE attachment.")
    return payload


def _invoke(
    transport: HerokuRequestJson,
    method: str,
    path: str,
    *,
    payload: dict[str, str] | None,
    token: str,
) -> Any:
    try:
        return transport(method, path, payload=payload, token=token)
    except HerokuConfigError as exc:
        redacted = redact_heroku_error_body(str(exc), token=token)
        if redacted != str(exc):
            raise HerokuConfigError(redacted) from None
        raise
    except Exception as exc:
        redacted = redact_heroku_error_body(str(exc), token=token)
        raise HerokuConfigError(f"Heroku API {method} {path} failed: {redacted}") from None


def _post_attachment(call: Callable[..., Any], *, app: str, addon: str, name: str) -> None:
    call(
        "POST",
        f"/apps/{app}/addon-attachments",
        payload=_attachment_payload(addon=addon, app=app, name=name),
    )


def _default_request_json(method: str, path: str, *, payload: dict[str, str] | None, token: str) -> Any:
    """Call the Heroku Platform API. The host is fixed to ``api.heroku.com``."""
    if not path.startswith("/") or path.startswith("//") or "://" in path or ".." in path:
        raise HerokuConfigError("Refusing Heroku API request: host is fixed to api.heroku.com.")
    url = f"https://{HEROKU_API_HOST}{path}"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HEROKU_API_HOST or parts.username or parts.password:
        raise HerokuConfigError("Refusing Heroku API request: host is fixed to api.heroku.com.")

    data = None
    headers = {
        "Accept": HEROKU_ACCEPT,
        "Authorization": f"Bearer {token}",
        "User-Agent": "django-mirroring",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Host is fixed to api.heroku.com. App and attachment names are validated before use.
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        redacted = redact_heroku_error_body(body, token=token)
        raise HerokuConfigError(f"Heroku API {method} {path} failed ({exc.code}): {redacted}") from None
    except Exception as exc:
        redacted = redact_heroku_error_body(str(exc), token=token)
        raise HerokuConfigError(f"Heroku API {method} {path} failed: {redacted}") from None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HerokuConfigError(f"Heroku API {method} {path} returned invalid JSON ({exc.msg}).") from None
