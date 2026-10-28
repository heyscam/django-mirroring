# django-mirroring

Django add-on for production database mirror refresh and staging restore. Uses
[Dumpling](https://github.com/ababic/dumpling) for in-stream anonymisation and
Postgres shadow cutover (rename on the same server, or a Heroku attachment swap)
so consumers never see a half-loaded mirror.

## Links

- [Quickstart (Heroku)](#quickstart-heroku)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Issues](https://github.com/ababic/django-mirroring/issues)

## Supported versions

This package supports **Django 6.0** and **Python 3.12+**. Dependencies are
compatible-release pinned; see [Pinned dependencies](#pinned-dependencies).

## Installation

```bash
uv add "django-mirroring~=0.2.4"
# or: pip install "django-mirroring~=0.2.4"
```

Add `"mirroring"` to `INSTALLED_APPS` and run migrations:

```bash
python manage.py migrate mirroring
```

## Quickstart (Heroku)

End-to-end setup for a Django app that already runs on Heroku Postgres + S3.
Substitute your app names and plan sizes. The package does not invent hostnames
or “safe” targets — **you** choose which URLs and buckets are written.

Architecture once configured:

```text
production follower ──pg_dump──► Dumpling ──► shadow addon (MIRROR_SHADOW_DATABASE)
                                              │
                         attachment swap ─────┘──► published mirror (MIRROR_DATABASE)
                         DATABASE_URL is not changed
                         restore_from_mirror ──► staging primary
                         sync_referenced_media ──► staging media bucket
```

### 1. Install and wire settings

```bash
uv add "django-mirroring~=0.2.4"
python manage.py migrate mirroring
```

In the host project (typical pattern):

```python
# settings.py
import mirroring.defaults as mirror_defaults
from myproject import mirror_policy  # project-owned lists + Dumpling path

INSTALLED_APPS = [
    # ...
    "mirroring",
]

MIRRORING_ADMIN_SITE = "myproject.admin.site"  # optional custom AdminSite
MIRROR_DUMPLING_CONFIG = mirror_defaults.mirror_dumpling_config_path(
    default=mirror_policy.DEFAULT_DUMPLING_CONFIG,
)
MIRROR_EXCLUDED_SCHEMA = mirror_policy.MIRROR_EXCLUDED_SCHEMA  # e.g. heroku_ext
MIRROR_EXCLUDED_TABLE_DATA = mirror_defaults.build_mirror_excluded_table_data(
    mirror_policy.MIRROR_EXTRA_EXCLUDED_TABLE_DATA,
)
MIRROR_RETAIN_MONTHS = mirror_defaults.mirror_retain_months()
MIRROR_ROW_RETAIN = mirror_policy.MIRROR_ROW_RETAIN
MIRROR_RESTORE_STAFF_EMAIL_DOMAINS = mirror_policy.mirror_restore_staff_email_domains()
```

### 2. Mirror database (production)

#### Provision two mirror addons (Heroku)

Heroku Postgres roles have `rolcreatedb = false` and cannot connect to the
`postgres` or `template1` databases, so refresh cannot `CREATE DATABASE`.
Provision a **published** addon and a **shadow** addon, both on the same major
as the follower. Heroku’s default for a new addon is 18, and a 15 client cannot
dump an 18 server — pass `--version=15` (or the follower’s major).

```bash
# Example: empty Standard-0 addons (adjust the plan — both must fit a full mirror;
# they trade places on every refresh)
heroku addons:create heroku-postgresql:standard-0 \
  --version=15 \
  -a your-app-production \
  --as MIRROR_DATABASE

heroku addons:create heroku-postgresql:standard-0 \
  --version=15 \
  -a your-app-production \
  --as MIRROR_SHADOW_DATABASE

heroku pg:wait -a your-app-production
```

`--as` sets `MIRROR_DATABASE_URL` and `MIRROR_SHADOW_DATABASE_URL`. Refresh loads
the shadow addon, then swaps the `MIRROR_DATABASE` attachment onto it. The
previous mirror becomes `MIRROR_SHADOW_DATABASE` and is wiped on the next run.
Heroku restarts the app when attachments move.

Do **not** cut over with `heroku pg:promote`. That replaces `DATABASE_URL` (the
live primary). This command does not PATCH config vars — Heroku owns attachment
URLs and rewrites them when attachments move. The only swapped pair is
`MIRROR_DATABASE` / `MIRROR_SHADOW_DATABASE`.

On a Postgres you administer yourself, leave `MIRROR_SHADOW_DATABASE_URL` unset.
Refresh then creates `{destination}_tmp` on the destination server and cuts over
by rename. That role needs `CREATEDB`, which Heroku does not grant.

#### Platform API token

Attachment swap needs a token that can manage addon attachments on the app.
Setting the config var restarts the app. Dynos already have `HEROKU_APP_NAME`;
set `MIRROR_HEROKU_APP_NAME` only to override it (it wins).

```bash
heroku config:set HEROKU_API_KEY="..." -a your-app-production
# Optional override of HEROKU_APP_NAME:
heroku config:set MIRROR_HEROKU_APP_NAME=your-app-production -a your-app-production
```

#### Point the dump source at a full-access follower

Prefer a **follower/replica** of the primary so `pg_dump` does not compete with
writes, and so the role can `SELECT` every table Dumpling anonymises.

```bash
# Example: follower of DATABASE_URL on the same app
heroku addons:create heroku-postgresql:standard-0 \
  -a your-app-production \
  --as MIRROR_SOURCE_DATABASE \
  -- --follow DATABASE_URL

heroku pg:wait -a your-app-production
# Ensure MIRROR_SOURCE_DATABASE_URL is set (Heroku sets it with --as above)
heroku config:get MIRROR_SOURCE_DATABASE_URL -a your-app-production
```

A follower matches the primary’s major. Pin both mirror addons to that major
(`--version=15`, or the follower’s major). Do **not** use a restricted /
allow-listed read role (missing `SELECT` on PII tables) as the dump source —
the dump will be incomplete or fail.

#### Exclude noisy / non-portable table data

Omit provider schemas and empty high-churn or secret tables (schema still
created). Common Heroku + Django starting point:

```python
# myproject/mirror_policy.py
MIRROR_EXCLUDED_SCHEMA = ["heroku_ext", "_heroku"]

MIRROR_EXTRA_EXCLUDED_TABLE_DATA = [
    "django_session",
    "django_admin_log",
    # tokens, rate-limit buckets, task trackers, bulk exports, …
]
```

Wire with `build_mirror_excluded_table_data(...)` as shown above. Optionally set
`MIRROR_ROW_RETAIN` + `MIRROR_RETAIN_MONTHS` so refresh keeps only recent rows
for large timestamped tables (orders, etc.).

#### Add a Dumpling policy and `MIRROR_DUMPLING_CONFIG`

Anonymisation rules are **project-owned** TOML (not shipped in this package).

```bash
# Draft from a fresh plain-SQL dump when the schema is ready (dumpling-cli >= 0.9):
pg_dump "$MIRROR_SOURCE_DATABASE_URL" -Fp --no-owner --no-acl -f /tmp/src.sql
dumpling scaffold-config -i /tmp/src.sql --infer-json-paths -o myproject/dumplingconf.toml
```

Edit rules for emails, names, addresses, JSON paths, etc. Keep salts out of git:

```bash
heroku config:set DUMPLING_GLOBAL_SALT="$(openssl rand -hex 32)" -a your-app-production
```

Point Django at the file (path on the slug, or resolve via
`mirror_dumpling_config_path(default=...)`). Set staff domains used both for
username keep on the mirror and rematerialisation on restore:

```bash
heroku config:set MIRROR_RESTORE_STAFF_EMAIL_DOMAINS="example.com" -a your-app-production
```

Include a `# MIRROR_STAFF_USERNAME_KEEP_RULE` marker in the TOML where the
refresh command should inject the staff username keep rule (see host examples).

#### First refresh (dry-run, then live)

```bash
heroku run python manage.py refresh_database_mirror --dry-run -a your-app-production
# When ready (scheduler or one-off):
heroku run python manage.py refresh_database_mirror --confirm -a your-app-production
```

`--dry-run` prints the attachment-swap plan and lints Dumpling. It does not reset
schema, dump, or call the Heroku API. `--confirm` loads the shadow addon and
swaps attachments. `DATABASE_URL` is not changed — do not use `heroku pg:promote`.
Expect a dyno restart when the attachments move. Schedule
`refresh_database_mirror` nightly on production once dry-runs look good.

#### Read-only URL for staging (store on production for safekeeping)

Staging should **restore from** the mirror using a credential that cannot
`DROP`/`CREATE` databases or write production data. Create a read-only role on
the **mirror** instance, put the URL in production config as
`MIRROR_DATABASE_READONLY_URL` (documentation / copy source only — this package
does not read that name), then set staging’s `MIRROR_DATABASE_URL` to that
read-only URL.

```bash
# On the mirror Postgres (example — adjust role/db names):
# CREATE ROLE mirror_readonly LOGIN PASSWORD '...';
# GRANT CONNECT ON DATABASE <mirror_db> TO mirror_readonly;
# GRANT USAGE ON SCHEMA public TO mirror_readonly;
# GRANT SELECT ON ALL TABLES IN SCHEMA public TO mirror_readonly;
# ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mirror_readonly;

heroku config:set MIRROR_DATABASE_READONLY_URL='postgres://mirror_readonly:...@.../<mirror_db>?sslmode=require' \
  -a your-app-production
```

### 3. Mirror media (production safekeeping + policy)

When staging uses a **different** S3 bucket than production, restore only brings
DB rows; object bytes are copied afterward with `sync_referenced_media`.

#### Read-only AWS credentials (store on production)

Create an IAM user/role that can `s3:GetObject` / `s3:ListBucket` on the
production media bucket only. Store the keys on the **production** app for
safekeeping (operators copy them to staging when configuring sync):

```bash
heroku config:set \
  MEDIA_SYNC_SOURCE_AWS_ACCESS_KEY_ID=... \
  MEDIA_SYNC_SOURCE_AWS_SECRET_ACCESS_KEY=... \
  -a your-app-production
```

(Your host may map those into the process env that `boto3` uses for the source
client, or you may pass a dedicated profile — the package reads
`MEDIA_SYNC_SOURCE_BUCKET` / region and the usual AWS credential chain.)

#### Skip or anonymise selected media

In host settings:

```python
# Skip CopyObject for PII-bearing models/fields; plant placeholders instead.
MIRRORING_ANONYMISE_MEDIA_FIELDS = [
    "listing.shipment",  # shipping labels
    "data_reporting.exporteddata",
    # or field-level: "myapp.model.field",
]
# Optional: "myapp.media_sync.anonymise_for_ref"
# MIRRORING_ANONYMISE_MEDIA_PROVIDER = "..."

# Extra keys not stored in FileField/ImageField (JSON path bags, CharFields, …).
# Omit a collector entirely to skip that bag.
MEDIA_SYNC_EXTRA_COLLECTORS = [
    "myapp.media_sync.collect_extra_keys",
]
```

Anything not referenced from the restored DB (and collectors) is never copied —
that is how “skip certain media completely” works at scale.

### 4. Mirror restore (staging)

#### Env vars on the staging app

| Var | Points at |
|-----|-----------|
| `MIRROR_DATABASE_URL` | Read-only URL of the **production mirror** (copy of `MIRROR_DATABASE_READONLY_URL`) |
| `MIRROR_RESTORE_TARGET_DATABASE_URL` | Staging primary to replace (usually staging `DATABASE_URL`) |
| `MIRROR_RESTORE_ALLOW` | Set to `1` only for live restore/revert one-offs or authorized jobs |
| `MIRROR_RESTORE_STAFF_EMAIL_DOMAINS` | Same domains as production (username rematerialise) |
| `DUMPLING_GLOBAL_SALT` | Not required for restore (already scrubbed on the mirror) |
| `MEDIA_SYNC_SOURCE_BUCKET` | Production media bucket name |
| `MEDIA_SYNC_ALLOW` | `1` for live media sync |
| `AWS_STORAGE_BUCKET_NAME` | **Staging** media bucket (destination) |
| AWS credentials | Staging write + read-only prod (or source) credentials as your IAM design requires |

```bash
heroku config:set \
  MIRROR_DATABASE_URL="$(heroku config:get MIRROR_DATABASE_READONLY_URL -a your-app-production)" \
  MIRROR_RESTORE_TARGET_DATABASE_URL="$(heroku config:get DATABASE_URL -a your-app-staging)" \
  MIRROR_RESTORE_STAFF_EMAIL_DOMAINS="example.com" \
  MEDIA_SYNC_SOURCE_BUCKET=your-prod-media-bucket \
  -a your-app-staging
```

Ensure staging dynos have `pg_dump` / `psql` at least major
`MIRRORING_POSTGRES_CLIENT_MAJOR` (default `15`).

#### Run restore, then media sync

```bash
heroku run python manage.py restore_from_mirror --dry-run -a your-app-staging

MIRROR_RESTORE_ALLOW=1 heroku run \
  python manage.py restore_from_mirror --confirm -a your-app-staging

# Optional rollback while {db}_preswap still exists:
# MIRROR_RESTORE_ALLOW=1 heroku run python manage.py revert_mirror_restore --confirm -a your-app-staging

heroku run python manage.py sync_referenced_media --dry-run -a your-app-staging
MEDIA_SYNC_ALLOW=1 heroku run \
  python manage.py sync_referenced_media --confirm -a your-app-staging
```

`restore_from_mirror` rematerialises staff `email` / `password` / names from the
**pre-restore staging** DB onto users matched by `UserModel.USERNAME_FIELD`
(usernames were kept on the mirror for allowlisted email domains).

### 5. Checklist

- [ ] `mirroring` installed, migrated, admin watermark visible
- [ ] Production: `MIRROR_SOURCE_DATABASE_URL` (follower) and `MIRROR_DATABASE_URL`
- [ ] Heroku: `MIRROR_SHADOW_DATABASE_URL` on the same Postgres major (`--version=15` or the follower’s major), `HEROKU_API_KEY` (can manage attachments), and `MIRROR_HEROKU_APP_NAME` when the dyno app name is not the swap target
- [ ] Heroku cutover swaps `MIRROR_DATABASE` / `MIRROR_SHADOW_DATABASE` — not `heroku pg:promote` (`DATABASE_URL` stays the primary)
- [ ] Production: Dumpling TOML + `DUMPLING_GLOBAL_SALT` + exclude/retain lists
- [ ] Production: nightly `refresh_database_mirror`
- [ ] Production: `MIRROR_DATABASE_READONLY_URL` (+ optional media source IAM) stored
- [ ] Staging: restore target + readonly mirror URL + staff domains
- [ ] Staging: `restore_from_mirror` then `sync_referenced_media` with anonymise list

## Pinned dependencies

Every dependency is pinned to a compatible release (`~=`) so a mirror run cannot
silently pick up an incompatible Dumpling policy format or AWS SDK behaviour.

| Dependency | Pin | Used by |
|------------|-----|---------|
| Python | `>=3.12` | all |
| `Django` | `~=6.0.0` | all |
| `dj-database-url` | `~=2.2.0` | temporary database aliases |
| `python-dateutil` | `~=2.9.0` | retain-window cutoffs |
| `dumpling-cli` | `~=0.9.0` | `refresh_database_mirror` anonymisation |
| `boto3` | `~=1.42.0` | `sync_referenced_media` |

`dumpling-cli` ships the `dumpling` executable, so installing this package also
pins the CLI. The commands verify it at start-up and refuse to run against a
different minor series.

**Postgres client tools** (`pg_dump`, `psql`) are system packages, not pip
dependencies, so they are pinned as a **minimum major** — `pg_dump` refuses to
dump a server newer than itself, while newer clients read older servers fine:

| Setting / env | Default | Purpose |
|---------------|---------|---------|
| `MIRRORING_POSTGRES_CLIENT_MAJOR` | `15` | Minimum `pg_dump` / `psql` major; set to the highest server major you mirror from |

Commands fail fast with a clear error when a tool is missing or too old, rather
than part-way through a dump.

## Dumpling policy (project-owned)

Anonymisation rules live in a **project-owned** Dumpling TOML file, not inside this
package. Point the refresh command at it with `MIRROR_DUMPLING_CONFIG` (env) or
`MIRROR_DUMPLING_CONFIG` (Django setting). The effective config may also embed
generated `row_filters` and staff username keep rules at refresh time.

## Settings overview

Configure via Django settings and/or environment variables (host projects typically
wire env → settings in one place).

| Setting / env | Purpose |
|---------------|---------|
| `MIRROR_SOURCE_DATABASE_URL` | `pg_dump` source (prefer a full-access follower/replica) |
| `MIRROR_DATABASE_URL` | Published mirror database (destination for refresh; source for restore) |
| `MIRROR_SHADOW_DATABASE_URL` | Second Heroku Postgres addon. When set, refresh loads it and swaps the `MIRROR_DATABASE` attachment onto it. Unset keeps same-server `CREATE DATABASE` + rename cutover |
| `MIRROR_HEROKU_APP_NAME` | App whose attachments are swapped. When set, wins over `HEROKU_APP_NAME` |
| `HEROKU_API_KEY` | Platform API token used only to swap `MIRROR_DATABASE` and `MIRROR_SHADOW_DATABASE`. Required when the shadow URL is set |
| `MIRROR_DATABASE_READONLY_URL` | *(convention)* read-only mirror URL stored on production for staging to copy — not read by this package |
| `MIRROR_DUMPLING_CONFIG` | Path to project Dumpling TOML (required for refresh) |
| `MIRROR_EXCLUDED_SCHEMA` | Schemas omitted from dump (default: none — set in host settings) |
| `MIRROR_EXCLUDED_TABLES` | Tables omitted entirely |
| `MIRROR_EXCLUDED_TABLE_DATA` | Tables whose data is omitted (schema kept) — build with `build_mirror_excluded_table_data()` |
| `MIRROR_ROW_RETAIN` | Per-table datetime retain specs for Dumpling `row_filters` |
| `MIRROR_RETAIN_MONTHS` | Months of row history to keep (0 disables) |
| `MIRROR_RESTORE_TARGET_DATABASE_URL` | Staging DB replaced by `restore_from_mirror` |
| `MIRROR_RESTORE_ALLOW` | Must be `1` to run restore/revert |
| `MIRROR_RESTORE_STAFF_EMAIL_DOMAINS` | Comma-separated staff email domains (username keep + restore rematerialisation) |
| `MIRRORING_AUTO_REGISTER_ADMIN` | Register admin model (default: `True`) |
| `MIRRORING_ADMIN_SITE` | Optional dotted path to a custom `AdminSite` (e.g. `"core.admin.site"`) |
| `MIRRORING_POSTGRES_CLIENT_MAJOR` | Minimum `pg_dump` / `psql` major (default: `15`) |

`DUMPLING_GLOBAL_SALT` must be set in the environment for Dumpling lint/run.

## Endpoint guidance (operators)

Refresh and restore only refuse when source and destination resolve to the
**same** host/port/database (restore/revert also require `MIRROR_RESTORE_ALLOW=1`
and `--confirm`). Which databases those env URLs point at is otherwise an
operator responsibility — document your project's URLs carefully; there is no
hostname allow/block list in the package:

- Prefer pointing `MIRROR_SOURCE_DATABASE_URL` at a **full-access** follower or
  offline replica so `pg_dump` can read every table Dumpling anonymises, and so
  refresh load does not compete with live writes. Dumping the primary is allowed
  but not recommended under load.
- Prefer pointing `MIRROR_DATABASE_URL` at a **separate** mirror database from the
  live app primary.
- On Heroku, also set `MIRROR_SHADOW_DATABASE_URL` to a second addon on the same
  major (`--version=15` or the follower’s major). Refresh loads that addon and
  swaps the `MIRROR_DATABASE` attachment. Do not `heroku pg:promote` — that
  replaces `DATABASE_URL`. Heroku restarts the app when attachments move.
  Heroku roles cannot `CREATE DATABASE`.
- When `MIRROR_SHADOW_DATABASE_URL` is unset, the destination role needs
  `CREATEDB` for `{db}_tmp` plus rename cutover on the same server.
- Point `MIRROR_RESTORE_TARGET_DATABASE_URL` only at a disposable staging (or
  equivalent) database you intend to replace.
- A restricted / allow-listed role that cannot `SELECT` PII tables will produce an
  incomplete or failing dump — use full-access credentials for the source.
- Put `sslmode` on connection URLs when the server requires TLS (no hostname-based
  SSL inference).
- Omit provider schemas (e.g. Heroku's `heroku_ext` / `_heroku`) via
  `MIRROR_EXCLUDED_SCHEMA` in the host project when needed.

## Management commands

| Command | Role |
|---------|------|
| `refresh_database_mirror` | Nightly production job: dump follower → Dumpling → shadow, then rename cutover or (Heroku) attachment swap |
| `restore_from_mirror` | Replace staging from the mirror via shadow load + rename cutover |
| `revert_mirror_restore` | Swap `{target}_preswap` back after a restore |
| `sync_referenced_media` | After restore: copy DB-referenced S3 keys from a source bucket into `AWS_STORAGE_BUCKET_NAME` |

## Selective media sync (separate buckets)

When staging must **not** share the production media bucket, run
`sync_referenced_media` after `restore_from_mirror`. It collects keys from every
`FileField` / `ImageField` (honouring private storage `location` prefixes) plus
optional host collectors, then `CopyObject`s only those keys.

| Setting / env | Purpose |
|---------------|---------|
| `MEDIA_SYNC_SOURCE_BUCKET` | Production (or mirror-source) media bucket to read from |
| `MEDIA_SYNC_SOURCE_REGION` | Optional source region (defaults to `AWS_DEFAULT_REGION`) |
| `MEDIA_SYNC_ALLOW` | Must be `1` for a live copy (`--dry-run` does not need it) |
| `MEDIA_SYNC_EXTRA_COLLECTORS` | List of dotted callables yielding extra relative keys (JSON path bags, CharFields, …) |
| `MIRRORING_ANONYMISE_MEDIA_FIELDS` | Models/fields to anonymise: `app.model` or `app.model.field` (skip CopyObject; plant placeholders) |
| `MIRRORING_ANONYMISE_MEDIA_PROVIDER` | Optional dotted callable `(MediaObjectRef) -> MediaAnonymiseSpec \| None` |
| `AWS_STORAGE_BUCKET_NAME` | Destination bucket (current env) |

```bash
python manage.py sync_referenced_media --dry-run
MEDIA_SYNC_ALLOW=1 python manage.py sync_referenced_media --confirm
```

Default behaviour skips keys already present on the destination (`--skip-existing`).
Missing source keys are counted and skipped (common when DB rows outlive deleted
objects).

### Anonymising PII media

List models or fields that must not be copied as-is. Those keys are **not**
copied from production; instead a placeholder is `PutObject`'d at the same
destination key. Image/PDF placeholders are seeded from the source object's
ETag (content fingerprint) so they stay visually distinct without copying real
bytes.

```python
# settings.py
MIRRORING_ANONYMISE_MEDIA_FIELDS = [
    "listing.shipment",  # dispatch/return labels + courier XML
    "ebay.ebaycoupondownload",  # coupon transaction CSVs
    "data_reporting.exporteddata",  # admin exports
    # or field-level: "reskinned_inventory.picture.preview",
]
# Optional override; return None to fall back to suffix defaults:
# MIRRORING_ANONYMISE_MEDIA_PROVIDER = "myapp.media_sync.anonymise_for_ref"
```

Omit a collector from `MEDIA_SYNC_EXTRA_COLLECTORS` to skip that path bag entirely.

## Admin

`MirrorDatabaseState` is a read-only singleton watermark (generation + restore time).
By default it registers on `django.contrib.admin.site`. Set `MIRRORING_ADMIN_SITE`
to your project's admin site (for example `"core.admin.site"`) or call
`mirroring.admin.register_admin(site)` yourself with `MIRRORING_AUTO_REGISTER_ADMIN = False`.

## Development

```bash
just install
just test
just lint
just coverage
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full recipe list and release process.
