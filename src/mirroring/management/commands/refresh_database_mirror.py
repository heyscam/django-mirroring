"""Refresh the production database mirror from a full-access follower dump.

The mirror is a standalone Postgres database consumed by agents, staging restore,
and local pulls. Anonymisation + trim via Dumpling is part of the refresh pipeline,
not the product name.

Safe refresh algorithm
----------------------
Load into a shadow database first, then cut over so consumers never see a
half-loaded mirror.

Same-server (``MIRROR_SHADOW_DATABASE_URL`` unset) — the destination role needs
``CREATEDB``. Heroku Postgres does not grant that, and cannot connect to the
``postgres`` or ``template1`` databases:

1. Preflight — distinct source and destination URLs.
2. Recreate shadow ``{destination}_tmp`` on the same cluster (``DROP`` if it
   already exists, then ``CREATE``) using ``MIRROR_DATABASE_URL`` credentials.
3. Stream ``pg_dump`` → Dumpling → ``psql`` into the shadow (failure leaves the
   published mirror untouched). Dumpling ``--report`` writes a provenance sidecar.
4. Post-restore password-hash reset on the shadow.
5. Migrate the ``mirroring`` app on the shadow and upsert ``MirrorDatabaseState``
   (generation timestamp, source host/db, Dumpling report; ``restored_at`` cleared).
6. Cutover by rename — terminate connections, rename live mirror → ``*_old_<ts>``,
   shadow → published name (``MIRROR_DATABASE_URL`` keeps working). Drop the
   retired database.

Heroku (``MIRROR_SHADOW_DATABASE_URL`` set):

1. Preflight, before the dump and on ``--dry-run`` — the shadow URL is a different
   host/port/database from both the source and the published mirror,
   ``HEROKU_API_KEY`` is non-empty, and the app name (``MIRROR_HEROKU_APP_NAME``,
   else ``HEROKU_APP_NAME``) matches ``^[a-z][a-z0-9-]{0,29}$``.
2. ``--dry-run`` prints the attachment-swap plan and lints Dumpling. It does not
   reset schema, dump, or call Heroku.
3. ``--confirm`` resets ``public`` on the shadow addon, streams the dump into it,
   resets password hashes, migrates ``mirroring``, and writes ``MirrorDatabaseState``.
   A failure here leaves the published mirror untouched.
4. Cut over by swapping the ``MIRROR_DATABASE`` and ``MIRROR_SHADOW_DATABASE``
   attachments. ``DATABASE_URL`` is not changed. Do not ``heroku pg:promote`` and
   do not PATCH config vars (Heroku owns attachment URLs). Heroku restarts the
   app when attachments move. The next refresh wipes whichever addon is then
   ``MIRROR_SHADOW_DATABASE`` and swaps again.

Recommended production topology
-------------------------------
* **Source** (``MIRROR_SOURCE_DATABASE_URL``) — prefer a production **follower**
  (or other offline replica) with **full** credentials so ``pg_dump`` can read
  every table Dumpling will scrub, and so refresh load does not compete with live
  writes. Dumping the primary works, but a follower is better practice. Do **not**
  point this at a restricted / allow-listed role that cannot ``SELECT`` customer
  or PII tables — the dump will silently omit them or fail mid-stream.
* **Published destination** (``MIRROR_DATABASE_URL``) — the database consumers
  read. Prefer a **separate** mirror database from the live app primary. When
  ``MIRROR_SHADOW_DATABASE_URL`` is unset, nightly load targets ``{db}_tmp`` and
  rename cutover replaces the published database name (role needs ``CREATEDB``).
* **Heroku shadow addon** (``MIRROR_SHADOW_DATABASE_URL``) — a second Postgres
  addon on the same major as the source. Refresh loads it, then swaps the
  ``MIRROR_DATABASE`` attachment onto it. Both addons must be large enough to
  hold the mirror; they trade places each run.

Trim levers
-----------
1. ``--exclude-table-data`` / settings ``MIRROR_EXCLUDED_TABLE_DATA`` —
   omit bulky/ephemeral tables entirely (sessions, logs, tokens, …).
2. Dumpling ``row_filters`` generated for ``MIRROR_ROW_RETAIN`` — keep rows
   with timestamp ``gte`` cutoff (``format = "datetime"``) or ``is_null``, plus
   optional parent→child ``cascade`` retain for related tables.

Anonymisation rules live in a project-owned Dumpling TOML (``MIRROR_DUMPLING_CONFIG``
or ``--config``). Staff email domains (``MIRROR_RESTORE_STAFF_EMAIL_DOMAINS``) keep
their **username** via Dumpling ``column_cases`` + ``keep``; email and password are
still scrubbed. Staging restore
(``restore_from_mirror``) rematerialises staging credentials by ``USERNAME_FIELD``
just before cutover.

Endpoints are fixed (no CLI overrides)::

    source = MIRROR_SOURCE_DATABASE_URL
    destination = MIRROR_DATABASE_URL
    shadow = {destination_dbname}_tmp    # when MIRROR_SHADOW_DATABASE_URL is unset
    shadow = MIRROR_SHADOW_DATABASE_URL  # Heroku second addon; attachment swap

Examples::

    # Preview the planned pipeline (no writes)
    python backend/manage.py refresh_database_mirror --dry-run

    # Nightly job / one-off on production
    python backend/manage.py refresh_database_mirror --confirm
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.management.base import CommandError
from django.utils import timezone

from mirroring.base import BaseMirroringCommand
from mirroring.database import migrate_database_url, temporary_database_alias
from mirroring.defaults import auth_user_db_table
from mirroring.heroku_config import (
    HerokuConfigError,
    redact_heroku_error_body,
    resolve_heroku_app_name,
    swap_mirror_attachments,
)
from mirroring.management.postgres_clone import (
    DumpLineFilter,
    cutover_by_rename,
    database_identity,
    database_name,
    drop_database_if_exists,
    keep_dump_line,
    libpq_environ,
    recreate_shadow_database,
    redact_database_url,
    shadow_database_name,
)
from mirroring.models import MirrorDatabaseState
from mirroring.versions import require_dumpling, require_postgres_clients


if TYPE_CHECKING:
    from argparse import ArgumentParser
    from datetime import date

# Documented password applied to every user after restore (matches sanitize_database_pii).
SANITIZED_USER_PASSWORD = "SanitizedDevPassword1!"
# Precomputed pbkdf2_sha256 hash of SANITIZED_USER_PASSWORD (production hasher iterations).
SANITIZED_USER_PASSWORD_HASH = (
    "pbkdf2_sha256$1200000$zSLEMtxgHM17K9i551Hhq1$eyBkkOhz+Q0MBT3jlfbIhbKCZm3G6PLD/hJXGaX6R7Q="
)

ALLOWED_PRODUCTION_ENV = "production"
SOURCE_URL_ENV = "MIRROR_SOURCE_DATABASE_URL"
DESTINATION_URL_ENV = "MIRROR_DATABASE_URL"
SHADOW_URL_ENV = "MIRROR_SHADOW_DATABASE_URL"
SUBPROCESS_TIMEOUT_SECONDS = 300
STAFF_USERNAME_KEEP_RULE_MARKER = "# MIRROR_STAFF_USERNAME_KEEP_RULE"

# Re-export for existing tests that import helpers from this module.
__all__ = [
    "Command",
    "build_row_retain_toml",
    "database_identity",
    "keep_dump_line",
    "libpq_environ",
    "strip_unsupported_session_settings",
]


def build_row_retain_toml(
    retain_specs: list[dict[str, Any]],
    *,
    cutoff: date,
) -> str:
    """Render Dumpling ``[row_filters."…"]`` with datetime ``gte`` and optional cascades.

    Each spec is ``{"table", "column", "cascades"?}``. Cascades are
    ``{"child_table", "child_fk", "parent_pk"?}`` (default parent PK ``id``).
    Null timestamps are kept via ``is_null`` so sparse rows are not dropped.
    Requires dumpling-cli >= 0.9.0.
    """
    if not retain_specs:
        return ""

    cutoff_value = cutoff.isoformat()
    chunks: list[str] = [
        "",
        "# --- Generated row retention (refresh_database_mirror) ---",
        f"# cutoff={cutoff_value} (datetime gte + is_null; dumpling-cli >= 0.9)",
    ]
    for spec in retain_specs:
        table = spec["table"]
        column = spec["column"]
        chunks.append(f'[row_filters."{table}"]')
        chunks.append("retain = [")
        chunks.append(f'  {{ column = "{column}", op = "gte", value = "{cutoff_value}", format = "datetime" }},')
        chunks.append(f'  {{ column = "{column}", op = "is_null" }},')
        chunks.append("]")
        for cascade in spec.get("cascades") or []:
            child_table = cascade["child_table"]
            child_fk = cascade["child_fk"]
            parent_pk = cascade.get("parent_pk", "id")
            chunks.append(f'[[row_filters."{table}".cascade]]')
            chunks.append(f'child_table = "{child_table}"')
            chunks.append(f'child_fk = "{child_fk}"')
            chunks.append(f'parent_pk = "{parent_pk}"')
        chunks.append("")
    return "\n".join(chunks)


def build_staff_username_keep_toml(staff_domains: list[str], *, user_table: str) -> str:
    """Build the username keep rule from the domains used by staging restore."""
    if not staff_domains:
        return "# No staff username domains configured."
    predicates = "\n".join(
        (f'  {{ column = "email", op = "ilike", value = {json.dumps(f"%@{domain.strip().lower()}")} }},')
        for domain in staff_domains
        if domain.strip()
    )
    if not predicates:
        return "# No staff username domains configured."
    return (
        f'[[column_cases."{user_table}".username]]\nwhen.any = [\n{predicates}\n]\nstrategy = {{ strategy = "keep" }}'
    )


def strip_unsupported_session_settings(line: str) -> bool:
    """Return True when ``line`` should be kept in the restore stream."""
    return keep_dump_line(line.encode("utf-8"))


class Command(BaseMirroringCommand):
    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--config",
            default="",
            help="Path to Dumpling TOML policy (default: settings.MIRROR_DUMPLING_CONFIG).",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to load the shadow DB and cut over the published mirror.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the planned pipeline and validate tooling/config; do not write.",
        )
        parser.add_argument(
            "--allow-non-production",
            action="store_true",
            help="Allow ENV values other than production (local/staging experiments only).",
        )
        parser.add_argument(
            "--retain-months",
            type=int,
            default=None,
            help="Override settings.MIRROR_RETAIN_MONTHS for generated row_filters (0 disables).",
        )
        parser.add_argument(
            "--keep-work-dir",
            action="store_true",
            help="Keep the temporary work directory (dump + generated config) after success.",
        )
        parser.add_argument(
            "--dumpling-bin",
            default="",
            help="Dumpling executable (default: DUMPLING_BIN env, else 'dumpling' on PATH).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        confirm = bool(options["confirm"])
        if not dry_run and not confirm:
            raise CommandError("Refusing to refresh the mirror without --confirm (or pass --dry-run).")

        env_name = getattr(settings, "ENV", None)
        if env_name != ALLOWED_PRODUCTION_ENV and not options["allow_non_production"]:
            raise CommandError(
                f"Refusing to run when ENV={env_name!r}. "
                f"Nightly clones are for {ALLOWED_PRODUCTION_ENV!r}; "
                "pass --allow-non-production for deliberate non-prod experiments."
            )

        src_url = self.require_env_url(SOURCE_URL_ENV)
        dst_url = self.require_env_url(DESTINATION_URL_ENV)
        self.assert_safe_endpoints(src_url, dst_url)
        heroku_shadow_url = self.optional_env_url(SHADOW_URL_ENV)
        heroku_app_name = ""
        heroku_api_key = ""
        if heroku_shadow_url is not None:
            # Fail before Dumpling or a multi-gigabyte dump, including on --dry-run.
            self.assert_shadow_distinct(src_url, dst_url, heroku_shadow_url)
            heroku_app_name, heroku_api_key = self.require_heroku_swap_plan()
            shadow_name = ""
        else:
            try:
                shadow_name = shadow_database_name(database_name(dst_url))
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        config_path = Path(options["config"] or settings.MIRROR_DUMPLING_CONFIG).resolve()
        if not config_path.is_file():
            raise CommandError(f"Dumpling config not found: {config_path}")

        dumpling_bin = require_dumpling(options["dumpling_bin"] or None)
        require_postgres_clients("pg_dump", "psql")

        if not os.environ.get("DUMPLING_GLOBAL_SALT"):
            raise CommandError("DUMPLING_GLOBAL_SALT is not set (required by dumplingconf.toml).")

        retain_months = options["retain_months"]
        if retain_months is None:
            retain_months = int(settings.MIRROR_RETAIN_MONTHS)
        if retain_months < 0:
            raise CommandError("--retain-months must be >= 0.")

        excluded_schemas = list(settings.MIRROR_EXCLUDED_SCHEMA)
        excluded_tables = list(settings.MIRROR_EXCLUDED_TABLES)
        excluded_table_data = list(settings.MIRROR_EXCLUDED_TABLE_DATA)
        retain_specs = list(settings.MIRROR_ROW_RETAIN) if retain_months else []
        staff_domains = list(settings.MIRROR_RESTORE_STAFF_EMAIL_DOMAINS)

        cutoff = timezone.now().date() - relativedelta(months=retain_months) if retain_months else None

        self.render_h1("Refresh database mirror" + (" (dry run)" if dry_run else ""))
        self.info(f"Source ({SOURCE_URL_ENV}):      {self.redact_url(src_url)}")
        self.info(f"Destination ({DESTINATION_URL_ENV}): {self.redact_url(dst_url)}")
        if heroku_shadow_url is not None:
            self.info(f"Shadow ({SHADOW_URL_ENV}): {self.redact_url(heroku_shadow_url)}")
            self.info(
                "Cutover: swap Heroku attachments MIRROR_DATABASE and "
                f"MIRROR_SHADOW_DATABASE on {heroku_app_name}. "
                "DATABASE_URL is not changed. "
                "Heroku restarts the app when attachments move. "
                "The next refresh wipes whichever addon is then MIRROR_SHADOW_DATABASE and swaps again."
            )
        else:
            self.info(f"Shadow: DROP/CREATE {shadow_name!r} on destination server, then rename cutover")
        self.info(f"Dumpling config: {config_path}")
        self.info(f"Exclude schemas: {excluded_schemas or '(none)'}")
        self.info(f"Exclude tables:  {excluded_tables or '(none)'}")
        self.info(f"Exclude data:    {len(excluded_table_data)} table(s)")
        if cutoff is not None:
            self.info(f"Row retain:      {len(retain_specs)} table(s) with timestamp >= {cutoff.isoformat()}")
        else:
            self.info("Row retain:      disabled")

        work_dir = Path(tempfile.mkdtemp(prefix="refresh_database_mirror_"))
        shadow_url: str | None = None
        try:
            effective_config = self.write_effective_config(
                config_path,
                work_dir,
                retain_specs,
                staff_domains=staff_domains,
                cutoff=cutoff,
            )
            self.lint_dumpling_policy(dumpling_bin, effective_config)
            if dry_run:
                self.success("Dry run complete — no destination writes.")
                return

            if heroku_shadow_url is not None:
                self._refresh_via_attachment_swap(
                    src_url=src_url,
                    dst_url=dst_url,
                    shadow_url=heroku_shadow_url,
                    app_name=heroku_app_name,
                    api_key=heroku_api_key,
                    dumpling_bin=dumpling_bin,
                    effective_config=effective_config,
                    work_dir=work_dir,
                    excluded_schemas=excluded_schemas,
                    excluded_tables=excluded_tables,
                    excluded_table_data=excluded_table_data,
                    cutoff=cutoff,
                )
            else:
                try:
                    shadow_url = recreate_shadow_database(dst_url, run_checked=self.run_checked)
                except ValueError as exc:
                    raise CommandError(str(exc)) from exc
                self.info(f"Recreated shadow database {shadow_name!r}.")

                self.reset_destination_schema(shadow_url)
                dumpling_report = self.stream_dump_through_dumpling(
                    src_url=src_url,
                    dst_url=shadow_url,
                    dumpling_bin=dumpling_bin,
                    config_path=effective_config,
                    work_dir=work_dir,
                    excluded_schemas=excluded_schemas,
                    excluded_tables=excluded_tables,
                    excluded_table_data=excluded_table_data,
                )
                self.post_restore_fixes(shadow_url)
                self.record_mirror_generation(
                    shadow_url,
                    source_url=src_url,
                    retain_cutoff=cutoff,
                    dumpling_report=dumpling_report,
                )

                try:
                    retired_name = cutover_by_rename(dst_url, shadow_url, run_checked=self.run_checked)
                except ValueError as exc:
                    raise CommandError(str(exc)) from exc
                shadow_url = None
                drop_database_if_exists(dst_url, retired_name, run_checked=self.run_checked)
                self.success(
                    f"Cut over by rename. Published mirror is again {database_name(dst_url)!r}; "
                    f"previous mirror {retired_name!r} dropped. "
                    f"All user passwords are now: {SANITIZED_USER_PASSWORD}"
                )
        except Exception:
            if shadow_url:
                self.warning(
                    f"Leaving shadow database {shadow_name!r} in place for inspection ({self.redact_url(shadow_url)})."
                )
            raise
        finally:
            if options["keep_work_dir"]:
                self.warning(f"Keeping work dir: {work_dir}")
            else:
                shutil.rmtree(work_dir, ignore_errors=True)

    def optional_env_url(self, env_var: str) -> str | None:
        value = os.environ.get(env_var, "").strip()
        if not value:
            return None
        if urlparse(value).scheme not in {"postgres", "postgresql"}:
            raise CommandError(f"{env_var} must be a postgres:// or postgresql:// URL.")
        return value

    def assert_shadow_distinct(self, src_url: str, dst_url: str, shadow_url: str) -> None:
        shadow_id = database_identity(shadow_url)
        if shadow_id == database_identity(src_url):
            raise CommandError(f"{SHADOW_URL_ENV} and {SOURCE_URL_ENV} resolve to the same host/port/database.")
        if shadow_id == database_identity(dst_url):
            raise CommandError(f"{SHADOW_URL_ENV} and {DESTINATION_URL_ENV} resolve to the same host/port/database.")

    def require_heroku_swap_plan(self) -> tuple[str, str]:
        """Validate the Heroku app name and API key before any dump starts."""
        api_key = os.environ.get("HEROKU_API_KEY", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise CommandError(
                "HEROKU_API_KEY is not set or empty. "
                "It is required to swap MIRROR_DATABASE attachments before the dump starts."
            )
        try:
            app_name = resolve_heroku_app_name()
        except HerokuConfigError as exc:
            raise CommandError(str(exc)) from exc
        return app_name, api_key.strip()

    def _refresh_via_attachment_swap(
        self,
        *,
        src_url: str,
        dst_url: str,
        shadow_url: str,
        app_name: str,
        api_key: str,
        dumpling_bin: str,
        effective_config: Path,
        work_dir: Path,
        excluded_schemas: list[str],
        excluded_tables: list[str],
        excluded_table_data: list[str],
        cutoff: date | None,
    ) -> None:
        """Load the shadow addon, then swap attachments. Rename cutover is not used."""
        try:
            self.reset_destination_schema(shadow_url)
            dumpling_report = self.stream_dump_through_dumpling(
                src_url=src_url,
                dst_url=shadow_url,
                dumpling_bin=dumpling_bin,
                config_path=effective_config,
                work_dir=work_dir,
                excluded_schemas=excluded_schemas,
                excluded_tables=excluded_tables,
                excluded_table_data=excluded_table_data,
            )
            self.post_restore_fixes(shadow_url)
            self.record_mirror_generation(
                shadow_url,
                source_url=src_url,
                retain_cutoff=cutoff,
                dumpling_report=dumpling_report,
            )
        except Exception:
            self.warning(
                "Published mirror left untouched. "
                f"Shadow addon may be partially loaded ({self.redact_url(shadow_url)})."
            )
            raise
        try:
            swap_mirror_attachments(
                app_name=app_name,
                api_key=api_key,
                published_url=dst_url,
                shadow_url=shadow_url,
            )
        except HerokuConfigError as exc:
            raise CommandError(redact_heroku_error_body(str(exc), token=api_key)) from None
        self.success(
            "Swapped Heroku attachments. MIRROR_DATABASE is the loaded addon and "
            "MIRROR_SHADOW_DATABASE is the previous mirror. DATABASE_URL was not changed. "
            "Heroku restarts the app when attachments move. The next refresh wipes whichever "
            "addon is now MIRROR_SHADOW_DATABASE and swaps again. "
            f"All user passwords are now: {SANITIZED_USER_PASSWORD}"
        )

    def require_env_url(self, env_var: str) -> str:
        try:
            value = os.environ[env_var]
        except KeyError as exc:
            raise CommandError(f"Environment variable {env_var} is not set.") from exc
        if not value.strip():
            raise CommandError(f"Environment variable {env_var} is empty.")
        if urlparse(value).scheme not in {"postgres", "postgresql"}:
            raise CommandError(f"{env_var} must be a postgres:// or postgresql:// URL.")
        return value

    def assert_safe_endpoints(self, src_url: str, dst_url: str) -> None:
        if database_identity(src_url) == database_identity(dst_url):
            raise CommandError(f"{SOURCE_URL_ENV} and {DESTINATION_URL_ENV} resolve to the same host/port/database.")

    def redact_url(self, url: str) -> str:
        return redact_database_url(url)

    def lint_dumpling_policy(self, dumpling_bin: str, config_path: Path) -> None:
        self.info("Running dumpling lint-policy…")
        self.run_checked(
            [dumpling_bin, "lint-policy", "--config", str(config_path)],
            label="dumpling lint-policy",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

    def write_effective_config(
        self,
        base_config: Path,
        work_dir: Path,
        retain_specs: list[dict[str, Any]],
        *,
        staff_domains: list[str],
        cutoff: date | None,
    ) -> Path:
        base_text = base_config.read_text(encoding="utf-8")
        staff_rule = build_staff_username_keep_toml(staff_domains, user_table=auth_user_db_table())
        if STAFF_USERNAME_KEEP_RULE_MARKER in base_text:
            base_text = base_text.replace(STAFF_USERNAME_KEEP_RULE_MARKER, staff_rule)
        else:
            base_text = f"{base_text.rstrip()}\n\n{staff_rule}\n"
        retain_toml = ""
        if cutoff is not None and retain_specs:
            retain_toml = build_row_retain_toml(retain_specs, cutoff=cutoff)
        effective = work_dir / "dumpling.effective.toml"
        effective.write_text(base_text + retain_toml, encoding="utf-8")
        return effective

    def run_checked(
        self,
        cmd: list[str],
        *,
        label: str,
        env: dict[str, str] | None = None,
        timeout: int | None = SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"{label} timed out after {timeout}s.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise CommandError(f"{label} failed ({result.returncode}): {detail}")

    def reset_destination_schema(self, dst_url: str) -> None:
        self.info("Resetting shadow public schema…")
        sql = (
            "DROP SCHEMA IF EXISTS public CASCADE; "
            "CREATE SCHEMA public; "
            "GRANT ALL ON SCHEMA public TO public; "
            "GRANT ALL ON SCHEMA public TO CURRENT_USER;"
        )
        self.run_checked(
            ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1", "-c", sql],
            label="psql reset shadow schema",
            env=libpq_environ(dst_url),
        )

    def stream_dump_through_dumpling(
        self,
        *,
        src_url: str,
        dst_url: str,
        dumpling_bin: str,
        config_path: Path,
        work_dir: Path,
        excluded_schemas: list[str],
        excluded_tables: list[str],
        excluded_table_data: list[str],
    ) -> dict[str, Any] | None:
        """``pg_dump | dumpling | psql`` with PG17 ``transaction_timeout`` lines stripped.

        Credentials travel via libpq env vars (not argv). Subprocess stderr is
        redirected to files under ``work_dir`` so a large diagnostic write cannot
        deadlock the stdout pipeline. Dumpling ``--report`` writes a JSON
        provenance sidecar under ``work_dir``; its contents are returned when
        present.

        Managed Postgres often forbids ``session_replication_role`` (superuser-only),
        so FK integrity during restore depends on Dumpling cascade retain + dump order.
        Put ``sslmode`` on the connection URLs when the server requires TLS.
        """
        dump_cmd = [
            "pg_dump",
            "--format=plain",
            "--no-owner",
            "--no-acl",
            "--no-privileges",
            *[f"--exclude-schema={schema}" for schema in excluded_schemas],
            *[f"--exclude-table={table}" for table in excluded_tables],
            *[f"--exclude-table-data={table}" for table in excluded_table_data],
        ]
        report_path = work_dir / "dumpling-report.json"
        dumpling_cmd = [
            dumpling_bin,
            "--config",
            str(config_path),
            "--stats",
            "--report",
            str(report_path),
        ]
        psql_cmd = ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1"]
        src_env = libpq_environ(src_url)
        dst_env = libpq_environ(dst_url)

        self.info("Streaming pg_dump → dumpling → psql (follower → shadow)…")
        dump_err_path = work_dir / "pg_dump.stderr"
        dumpling_err_path = work_dir / "dumpling.stderr"
        psql_err_path = work_dir / "psql.stderr"
        line_filter = DumpLineFilter()

        with (
            dump_err_path.open("w+b") as dump_err,
            dumpling_err_path.open("w+b") as dumpling_err,
            psql_err_path.open("w+b") as psql_err,
        ):
            dump_proc = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=dump_err, env=src_env)
            if dump_proc.stdout is None:
                raise CommandError("pg_dump stdout pipe was not created.")

            dumpling_proc = subprocess.Popen(
                dumpling_cmd,
                stdin=dump_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=dumpling_err,
            )
            dump_proc.stdout.close()  # allow dump_proc to receive SIGPIPE if dumpling exits
            if dumpling_proc.stdout is None:
                raise CommandError("dumpling stdout pipe was not created.")

            psql_proc = subprocess.Popen(
                psql_cmd,
                stdin=subprocess.PIPE,
                stderr=psql_err,
                env=dst_env,
            )
            if psql_proc.stdin is None:
                raise CommandError("psql stdin pipe was not created.")

            children = (dump_proc, dumpling_proc, psql_proc)
            completed = False
            downstream_broken = False
            try:
                while True:
                    raw = dumpling_proc.stdout.readline()
                    if not raw:
                        completed = True
                        break
                    if line_filter.keep(raw):
                        try:
                            psql_proc.stdin.write(raw)
                        except BrokenPipeError:
                            downstream_broken = True
                            break
            except BaseException:
                for proc in children:
                    if proc.poll() is None:
                        proc.kill()
                raise
            finally:
                try:
                    psql_proc.stdin.close()
                except BrokenPipeError:
                    pass
                if not completed:
                    # Stop upstream writers so wait() cannot block on a full pipe.
                    for proc in (dump_proc, dumpling_proc):
                        if proc.poll() is None:
                            proc.kill()
                dumpling_proc.stdout.close()
                dump_rc = dump_proc.wait()
                dumpling_rc = dumpling_proc.wait()
                psql_rc = psql_proc.wait()

        dump_stderr = dump_err_path.read_text(encoding="utf-8", errors="replace")
        dumpling_stderr = dumpling_err_path.read_text(encoding="utf-8", errors="replace")
        psql_stderr = psql_err_path.read_text(encoding="utf-8", errors="replace")

        if dumpling_stderr.strip():
            self.warning(dumpling_stderr.strip())
        if downstream_broken and psql_rc != 0:
            raise CommandError(f"psql restore failed ({psql_rc}): {psql_stderr.strip()}")
        if dump_rc != 0:
            raise CommandError(f"pg_dump failed ({dump_rc}): {dump_stderr.strip()}")
        if dumpling_rc != 0:
            raise CommandError(f"dumpling failed ({dumpling_rc}): {dumpling_stderr.strip()}")
        if psql_rc != 0:
            raise CommandError(f"psql restore failed ({psql_rc}): {psql_stderr.strip()}")

        return self.load_dumpling_report(report_path)

    def load_dumpling_report(self, report_path: Path) -> dict[str, Any] | None:
        """Parse Dumpling ``--report`` JSON when the sidecar was written."""
        if not report_path.is_file():
            self.warning(f"Dumpling report missing at {report_path}; continuing without provenance blob.")
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Dumpling report at {report_path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise CommandError(f"Dumpling report at {report_path} must be a JSON object.")
        return payload

    def record_mirror_generation(
        self,
        database_url: str,
        *,
        source_url: str,
        retain_cutoff: date | None,
        dumpling_report: dict[str, Any] | None,
    ) -> None:
        """Ensure ``mirroring`` schema on the shadow and upsert generation watermark."""
        self.info("Migrating mirroring app on shadow and recording MirrorDatabaseState…")
        migrate_database_url(
            database_url,
            alias="mirroring_refresh_shadow",
            app_labels=["mirroring"],
            verbosity=min(self.verbosity, 1),
        )
        source_host, _port, source_database = database_identity(source_url)
        report = dumpling_report or {}
        with temporary_database_alias(database_url, alias="mirroring_refresh_shadow") as alias:
            state = MirrorDatabaseState.get_solo(using=alias)
            state.generated_at = timezone.now()
            state.source_host = source_host
            state.source_database = source_database
            state.retain_cutoff = retain_cutoff
            state.dumpling_version = str(report.get("dumpling_version") or "")[:64]
            state.dumpling_config_sha256 = str(report.get("config_sha256") or "")[:64]
            state.dumpling_report = dumpling_report
            state.restored_at = None
            state.save(
                using=alias,
                update_fields=[
                    "generated_at",
                    "source_host",
                    "source_database",
                    "retain_cutoff",
                    "dumpling_version",
                    "dumpling_config_sha256",
                    "dumpling_report",
                    "restored_at",
                ],
            )
        self.info(
            f"Recorded mirror generation from {source_host}/{source_database} "
            f"(dumpling_version={state.dumpling_version or 'unknown'})."
        )

    def post_restore_fixes(self, dst_url: str) -> None:
        """Apply restore follow-ups Dumpling cannot express (valid password hashes)."""
        self.info("Applying post-restore fixes (user passwords)…")
        user_table = auth_user_db_table()
        sql = (
            f"UPDATE {user_table} "
            f"SET password = '{SANITIZED_USER_PASSWORD_HASH}' "
            "WHERE password IS NOT NULL AND password <> '';"
        )
        self.run_checked(
            ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1", "-c", sql],
            label="psql post-restore password reset",
            env=libpq_environ(dst_url),
        )
