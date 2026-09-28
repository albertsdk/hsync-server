"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.
"""

import json
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from cryptography import x509
from hsync_common.endpoint import parse_host_port
from hsync_common.runtime import runtime_summary
from pydantic import ValidationError

from hsync_server.config import (
    get_log_level,
    get_port,
    get_reload,
    get_settings,
    get_ssl_cert_dir,
    get_ssl_certfile,
    get_ssl_enabled,
    get_ssl_keyfile,
    get_workers,
    load_env_file,
)
from hsync_server.constants import ACCOUNT_ID_LEN, HSYNC_VERSION
from hsync_server.logger import VALID_LOG_LEVELS, setup_logging
from hsync_server.ssl_utils import get_ssl_config

# Warn when the served TLS certificate is within this many days of expiry.
_CERT_EXPIRY_WARN_DAYS = 30

# =============================================================================
# CLI Application
# =============================================================================

app = typer.Typer(
    name="hsync-server",
    help="HSYNC Server - HTTP-based file synchronization server",
    add_completion=False,
    rich_markup_mode="rich",  # Enable rich formatting for grouped options
)


def _version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        print(f"hsync-server {HSYNC_VERSION}")
        raise typer.Exit()


def _validate_log_level(value: str | None) -> str | None:
    """Validate log level against valid options."""
    if value is not None and value not in VALID_LOG_LEVELS:
        raise typer.BadParameter(
            f"Invalid log level '{value}'. Must be one of: {', '.join(VALID_LOG_LEVELS)}"
        )
    return value


def _exit_account_not_found() -> None:
    """Emit the canonical "account not found" CLI error and exit with code 1."""
    typer.echo("Error: account not found", err=True)
    raise typer.Exit(1) from None


def _resolve_bind_host_port(host: str | None, port: int | None) -> tuple[str, int | None]:
    """Resolve the bind address/port from ``--host``/``--port``.

    ``--host`` may carry a port (``HOST[:PORT]``); that port and ``--port`` are
    mutually exclusive. ``port`` may be ``None`` so the env/default fallback is
    applied later by the caller.
    """
    bind_host = "0.0.0.0"
    bind_port = port
    if host is not None:
        try:
            hp = parse_host_port(host)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        bind_host = hp.host
        if hp.port is not None:
            if port is not None:
                raise typer.BadParameter(
                    "specify the port either in --host HOST:PORT or with --port, not both"
                )
            bind_port = hp.port
    return bind_host, bind_port


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    # -------------------------------------------------------------------------
    # Server options
    # -------------------------------------------------------------------------
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help=(
                "Bind address, optionally with a port (HOST[:PORT], e.g. "
                "0.0.0.0:9753 or [::1]:9753). Default: 0.0.0.0."
            ),
            metavar="HOST[:PORT]",
            rich_help_panel="Server",
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "-p",
            "--port",
            help=(
                "Server port (default: 9753). Mutually exclusive with a port in --host HOST:PORT."
            ),
            metavar="PORT",
            rich_help_panel="Server",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            help="Log level: debug, info, warning, error (default: info).",
            metavar="LEVEL",
            callback=_validate_log_level,
            rich_help_panel="Server",
        ),
    ] = None,
    no_color: Annotated[
        bool,
        typer.Option(
            "--no-color",
            help="Disable colored log output.",
            rich_help_panel="Server",
        ),
    ] = False,
    # -------------------------------------------------------------------------
    # SSL/TLS options
    # -------------------------------------------------------------------------
    ssl_enabled: Annotated[
        bool,
        typer.Option(
            "--ssl",
            help="Enable HTTPS/WSS. Auto-generates certificates if not provided.",
            rich_help_panel="SSL/TLS",
        ),
    ] = False,
    ssl_certfile: Annotated[
        str | None,
        typer.Option(
            "--ssl-cert",
            help="Path to SSL certificate file (PEM format).",
            metavar="FILE",
            rich_help_panel="SSL/TLS",
        ),
    ] = None,
    ssl_keyfile: Annotated[
        str | None,
        typer.Option(
            "--ssl-key",
            help="Path to SSL private key file (PEM format).",
            metavar="FILE",
            rich_help_panel="SSL/TLS",
        ),
    ] = None,
    ssl_cert_dir: Annotated[
        str | None,
        typer.Option(
            "--ssl-cert-dir",
            help="Directory for auto-generated certificates (default: ./certs).",
            metavar="DIR",
            rich_help_panel="SSL/TLS",
        ),
    ] = None,
    # -------------------------------------------------------------------------
    # General options
    # -------------------------------------------------------------------------
    env_file: Annotated[
        str | None,
        typer.Option(
            "--env-file",
            help="Load environment variables from file.",
            metavar="PATH",
        ),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option(
            "-V",
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """
    HSYNC Server - HTTP-based file synchronization server.

    Central server for HSYNC clients. Manages buckets (shared directories),
    handles file uploads/downloads via REST API, and broadcasts real-time
    events to connected clients via WebSocket.

    [bold]Option Groups:[/bold]

        [cyan]Server[/cyan]      Network port and logging configuration
        [cyan]SSL/TLS[/cyan]     HTTPS/WSS encryption settings

    [bold]Examples:[/bold]

        [dim]# Start server on default port (9753)[/dim]
        $ hsync-server

        [dim]# Start server on custom port[/dim]
        $ hsync-server --port 9000

        [dim]# Start with HTTPS enabled (auto-generates certificates)[/dim]
        $ hsync-server --ssl

        [dim]# Start with custom SSL certificates[/dim]
        $ hsync-server --ssl --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem

        [dim]# Start with debug logging[/dim]
        $ hsync-server --log-level debug

        [dim]# Load configuration from environment file[/dim]
        $ hsync-server --env-file /path/to/prod.env

    [bold]Environment Variables:[/bold]

        HSYNC_ADMIN_KEY         Bootstrap admin secret (64 hex, optional)
        HSYNC_BUCKETS           Path to buckets directory
        HSYNC_STATE_DIR         Path to state DB directory
        HSYNC_CORS_ENABLED      Enable CORS (true/false)
        HSYNC_CORS_ORIGINS      Allowed CORS origins (comma-separated)
        HSYNC_MAX_UPLOAD_SIZE   Maximum upload size in bytes
        HSYNC_RATE_LIMIT_*      Rate limit settings
        HSYNC_TRUSTED_PROXIES   Trusted reverse proxies for X-Forwarded-For

    [bold]Project:[/bold]

        https://github.com/albertsdk/hsync-server

    [bold]Authors:[/bold]

        Alberto Basso
    """
    # If a subcommand was invoked (e.g. "admin"), let it handle the call.
    if ctx.invoked_subcommand is not None:
        return

    # Load environment file if specified (before reading any config)
    if env_file:
        try:
            load_env_file(env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)

    # Get configuration values (CLI args take precedence over env vars).
    # Settings validation runs lazily here; translate any ValidationError into
    # a user-friendly message before the logger is even configured.
    try:
        effective_log_level = log_level if log_level else get_log_level()
        effective_host, port_candidate = _resolve_bind_host_port(host, port)
        effective_port = port_candidate if port_candidate else get_port()
        effective_ssl_enabled = ssl_enabled or get_ssl_enabled()
        effective_ssl_certfile = ssl_certfile if ssl_certfile else get_ssl_certfile()
        effective_ssl_keyfile = ssl_keyfile if ssl_keyfile else get_ssl_keyfile()
        effective_ssl_cert_dir = ssl_cert_dir if ssl_cert_dir else get_ssl_cert_dir()
    except ValidationError as exc:
        for err in exc.errors():
            print(f"Configuration error: {err['msg']}", file=sys.stderr)
        raise typer.Exit(1) from None

    # Configure logging before starting the server
    log = setup_logging(level=effective_log_level, no_color=no_color)

    # Log version at startup
    log.info(f"HSYNC Server v{HSYNC_VERSION}")
    log.info(runtime_summary())

    # Get SSL configuration (empty dict if SSL disabled)
    ssl_config = get_ssl_config(
        ssl_enabled=effective_ssl_enabled,
        ssl_certfile=effective_ssl_certfile or None,
        ssl_keyfile=effective_ssl_keyfile or None,
        ssl_cert_dir=effective_ssl_cert_dir,
    )

    # Warn when the persisted certificate is near or past expiry (a pinned
    # identity lives 20 years, so an expiring one means the deployment is old
    # and the operator must rotate it — see `admin tls rotate`).
    _warn_if_certificate_expiring(ssl_config.get("ssl_certfile") if ssl_config else None, log)

    # Log server startup mode
    if effective_ssl_enabled:
        log.info(f"Starting HSYNC server with HTTPS/WSS on port {effective_port}")
        log.info(f"Certificate: {ssl_config.get('ssl_certfile', 'N/A')}")
    else:
        log.info(f"Starting HSYNC server with HTTP/WS on port {effective_port}")
        log.warning("SSL is disabled. Communication is NOT encrypted!")

    # Canonical "serving" line: operators can find the exact URL (and whether
    # the API docs are exposed) without deriving SSL settings by themselves.
    scheme = "https" if effective_ssl_enabled else "http"
    log.info(f"Serving at {scheme}://localhost:{effective_port}")
    if get_settings().enable_docs:
        log.info(f"API documentation: {scheme}://localhost:{effective_port}/docs")

    # Auto-reload (dev mode) and multiple workers are mutually exclusive in
    # uvicorn: reload always runs single-process. Honor HSYNC_WORKERS only when
    # reload is off, mirroring the Docker entrypoint's --workers behavior.
    reload_enabled = get_reload()
    effective_workers = None if reload_enabled else get_workers()

    uvicorn.run(
        "hsync_server.main:app",
        host=effective_host,
        port=effective_port,
        log_level=effective_log_level,
        reload=reload_enabled,
        workers=effective_workers,
        **ssl_config,
    )


def run() -> None:
    """Entry point for the CLI application."""
    app()


# =============================================================================
# Admin CLI (operates directly on the store, no auth required)
# =============================================================================

admin_app = typer.Typer(
    name="admin",
    help="Local administration commands (filesystem authority, no key required).",
    add_completion=False,
    rich_markup_mode="rich",
)
app.add_typer(admin_app, name="admin")

client_app = typer.Typer(name="client", help="Account management.")
bucket_cli_app = typer.Typer(name="bucket", help="Bucket and membership management.")
admin_app.add_typer(client_app, name="client")
admin_app.add_typer(bucket_cli_app, name="bucket")

tls_app = typer.Typer(name="tls", help="Inspect the server TLS certificate.")
admin_app.add_typer(tls_app, name="tls")


def _warn_if_certificate_expiring(cert_path: str | None, log) -> None:
    """Log a warning when the served TLS certificate is near or past expiry.

    A pinned service identity lives for 20 years (ADR-0037), so an expiring
    certificate means the deployment is old and should be rotated with
    ``admin tls rotate``.
    """
    if not cert_path:
        return
    path = Path(cert_path)
    if not path.is_file():
        return
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return
    days_left = (cert.not_valid_after_utc - datetime.now(UTC)).days
    if days_left <= _CERT_EXPIRY_WARN_DAYS:
        log.warning(
            f"TLS certificate {path} expires in {days_left} day(s); rotate it with "
            "'hsync-server admin tls rotate'."
        )


def _server_identity_paths() -> tuple[Path, Path]:
    """Resolve the (certificate, private key) paths the server serves TLS with.

    Prefers explicit ``--ssl-certfile``/``--ssl-keyfile`` (``HSYNC_SSL_CERTFILE``
    / ``HSYNC_SSL_KEYFILE``); otherwise the auto-generated ``server.crt`` /
    ``server.key`` inside ``HSYNC_SSL_CERT_DIR``.
    """
    from hsync_server.config import get_ssl_cert_dir, get_ssl_certfile, get_ssl_keyfile

    cert = get_ssl_certfile()
    key = get_ssl_keyfile()
    if cert and key:
        return Path(cert), Path(key)
    cert_dir = Path(get_ssl_cert_dir())
    return cert_dir / "server.crt", cert_dir / "server.key"


def _server_certificate_path() -> Path | None:
    """Resolve the certificate the server serves TLS with, or None."""
    return _server_identity_paths()[0]


@tls_app.command("fingerprint")
def tls_fingerprint(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print the server certificate's SHA-256 fingerprint for out-of-band verification.

    Clients pin this value (``hsync trust server add``) when they use a self-signed
    server certificate. The fingerprint is stable across restarts (the certificate
    is persisted), so it is safe to distribute out of band.
    """
    from hsync_common.ssl_utils import fingerprint_from_der

    from hsync_server.config import get_ssl_enabled

    if not get_ssl_enabled():
        typer.echo("Error: server TLS is disabled; there is no certificate to inspect.", err=True)
        raise typer.Exit(1)
    cert_path = _server_certificate_path()
    if cert_path is None or not cert_path.is_file():
        typer.echo(
            f"Error: certificate not found at {cert_path}; start the server once to generate it.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: cannot read certificate {cert_path}: {exc}", err=True)
        raise typer.Exit(1) from None
    fingerprint = f"sha256:{fingerprint_from_der(der)}"
    if as_json:
        typer.echo(json.dumps({"fingerprint": fingerprint, "certificate": str(cert_path)}))
    else:
        typer.echo(fingerprint)


@tls_app.command("rotate")
def tls_rotate(
    hostname: Annotated[
        str | None,
        typer.Option("--hostname", help="Certificate common name (default: system hostname)."),
    ] = None,
) -> None:
    """Regenerate the server TLS certificate and key, and print the new fingerprint.

    The certificate is self-signed and validated by fingerprint pinning, so after
    rotating, point clients at the new fingerprint (accept both the old and the
    new value during the rotation window — see the client trust documentation).
    """
    from hsync_common.ssl_utils import fingerprint_from_der

    from hsync_server.config import get_ssl_certfile, get_ssl_enabled, get_ssl_keyfile
    from hsync_server.ssl_utils import generate_self_signed_cert

    if not get_ssl_enabled():
        typer.echo("Error: server TLS is disabled; there is no certificate to rotate.", err=True)
        raise typer.Exit(1)
    if get_ssl_certfile() and get_ssl_keyfile():
        typer.echo(
            "Error: the certificate is provided explicitly "
            "(HSYNC_SSL_CERTFILE/HSYNC_SSL_KEYFILE); rotate it outside HSYNC.",
            err=True,
        )
        raise typer.Exit(1)
    cert_path, key_path = _server_identity_paths()
    generate_self_signed_cert(str(cert_path), str(key_path), hostname=hostname)
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    typer.echo(f"Certificate rotated: {cert_path}")
    typer.echo(f"New fingerprint: sha256:{fingerprint_from_der(der)}")


def _resolve_account_id(name: str | None, account_id: str | None) -> str:
    """Resolve --name or --id to an account id."""
    from hsync_server.auth_store import TargetNotFoundError, get_auth_store

    if (name is None) == (account_id is None):
        typer.echo("Error: provide exactly one of --name or --id", err=True)
        raise typer.Exit(1)
    store = get_auth_store()
    try:
        target = store.resolve_target(account_id, name)
    except TargetNotFoundError:
        _exit_account_not_found()
    return target.id


def _resolve_client_target(client: str):
    """Resolve a --client value (account id or name) to an account.

    A 21-char alphanumeric value is treated as an id, anything else as a name.
    """
    from hsync_server.auth_store import TargetNotFoundError, get_auth_store

    store = get_auth_store()
    is_id = len(client) == ACCOUNT_ID_LEN and client.isalnum()
    try:
        return store.resolve_target(client if is_id else None, None if is_id else client)
    except TargetNotFoundError:
        _exit_account_not_found()


@client_app.command("add")
def client_add(
    name: Annotated[str, typer.Option("--name", help="Account name.")],
    role: Annotated[str, typer.Option("--role", help="Role: admin or client.")] = "client",
    valid_days: Annotated[int | None, typer.Option("--valid-days", min=1)] = None,
    expires_at: Annotated[str | None, typer.Option("--expires-at", help="RFC3339 UTC.")] = None,
) -> None:
    """Create a new account. The token is printed once."""
    from hsync_server.auth_store import ConflictError, get_auth_store, validate_name

    try:
        validate_name(name)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None

    if role not in ("admin", "client"):
        typer.echo("Error: role must be 'admin' or 'client'", err=True)
        raise typer.Exit(1)

    expiry = _cli_parse_expiry(expires_at, valid_days)
    store = get_auth_store()
    try:
        aid, token = store.create_account(name, role, expiry)
    except ConflictError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Account created: id={aid}, name={name}, role={role}")
    typer.echo(f"Token (save now, not recoverable): {token}")


@client_app.command("list")
def client_list() -> None:
    """List all accounts."""
    from hsync_server.auth_store import get_auth_store

    store = get_auth_store()
    accounts = store.list_accounts()
    if not accounts:
        typer.echo("(no accounts)")
        return
    for a in accounts:
        typer.echo(
            f"{a.id}  {a.name:20s}  {a.role:7s}  {a.status:8s}  "
            f"gen={a.key_generation}  prefix={a.key_prefix}  "
            f"exp={a.key_expires_at or '-'}"
        )


@client_app.command("rotate-key")
def client_rotate_key(
    name: Annotated[str | None, typer.Option("--name")] = None,
    account_id: Annotated[str | None, typer.Option("--id")] = None,
    grace: Annotated[int | None, typer.Option("--grace", min=1)] = None,
    valid_days: Annotated[int | None, typer.Option("--valid-days", min=1)] = None,
    expires_at: Annotated[str | None, typer.Option("--expires-at")] = None,
) -> None:
    """Rotate an account's key. The new token is printed once."""
    from hsync_server.auth_store import TargetNotFoundError, get_auth_store

    aid = _resolve_account_id(name, account_id)
    expiry = _cli_parse_expiry(expires_at, valid_days)
    store = get_auth_store()
    try:
        token = store.rotate_key(aid, grace_seconds=grace, expires_at=expiry)
    except TargetNotFoundError:
        _exit_account_not_found()
    typer.echo(f"Token (save now, not recoverable): {token}")


@client_app.command("set-expiry")
def client_set_expiry(
    name: Annotated[str | None, typer.Option("--name")] = None,
    account_id: Annotated[str | None, typer.Option("--id")] = None,
    valid_days: Annotated[int | None, typer.Option("--valid-days", min=1)] = None,
    expires_at: Annotated[str | None, typer.Option("--expires-at")] = None,
    none: Annotated[bool, typer.Option("--none", help="Remove expiry.")] = False,
) -> None:
    """Set, extend, or remove the key expiry."""
    from hsync_server.auth_store import TargetNotFoundError, get_auth_store

    aid = _resolve_account_id(name, account_id)
    if none:
        expiry = None
    else:
        expiry = _cli_parse_expiry(expires_at, valid_days)
        if expiry is None:
            typer.echo("Error: provide --valid-days, --expires-at, or --none", err=True)
            raise typer.Exit(1)
    store = get_auth_store()
    try:
        store.set_expiry(aid, expiry)
    except TargetNotFoundError:
        _exit_account_not_found()
    typer.echo(f"Expiry set: {expiry or 'none'}")


@client_app.command("disable")
def client_disable(
    name: Annotated[str | None, typer.Option("--name")] = None,
    account_id: Annotated[str | None, typer.Option("--id")] = None,
    force: Annotated[bool, typer.Option("--force", help="Allow disabling the last admin.")] = False,
) -> None:
    """Disable an account."""
    from hsync_server.auth_store import ConflictError, TargetNotFoundError, get_auth_store

    aid = _resolve_account_id(name, account_id)
    store = get_auth_store()
    try:
        store.disable(aid, force=force)
    except TargetNotFoundError:
        _exit_account_not_found()
    except ConflictError as e:
        typer.echo(f"Error: {e} (use --force to override)", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Disabled: {aid}")


@client_app.command("enable")
def client_enable(
    name: Annotated[str | None, typer.Option("--name")] = None,
    account_id: Annotated[str | None, typer.Option("--id")] = None,
) -> None:
    """Enable a disabled account."""
    from hsync_server.auth_store import TargetNotFoundError, get_auth_store

    aid = _resolve_account_id(name, account_id)
    store = get_auth_store()
    try:
        store.enable(aid)
    except TargetNotFoundError:
        _exit_account_not_found()
    typer.echo(f"Enabled: {aid}")


@client_app.command("remove")
def client_remove(
    name: Annotated[str | None, typer.Option("--name")] = None,
    account_id: Annotated[str | None, typer.Option("--id")] = None,
    cascade: Annotated[bool, typer.Option("--cascade", help="Delete owned buckets.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Allow removing the last admin.")] = False,
) -> None:
    """Delete an account."""
    from hsync_server.auth_store import ConflictError, TargetNotFoundError, get_auth_store

    aid = _resolve_account_id(name, account_id)
    store = get_auth_store()
    try:
        store.delete_account(aid, cascade=cascade, force=force)
    except TargetNotFoundError:
        _exit_account_not_found()
    except ConflictError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Deleted: {aid}")


@bucket_cli_app.command("list")
def bucket_list() -> None:
    """List all buckets (FS ∪ membership)."""
    from hsync_server.auth_store import get_auth_store

    store = get_auth_store()
    buckets = store.list_all_buckets()
    if not buckets:
        typer.echo("(no buckets)")
        return
    for b in buckets:
        owner = b["owner"]
        owner_str = f"{owner['name']} ({owner['id']})" if owner else "(orphan)"
        members_str = ", ".join(m["name"] for m in b["members"]) or "(none)"
        disk = "on-disk" if b["on_disk"] else "ghost"
        typer.echo(f"{b['name']:20s}  {disk:8s}  owner={owner_str}  members=[{members_str}]")


@bucket_cli_app.command("create")
def bucket_create(
    bucket: Annotated[
        str,
        typer.Option("--bucket", help="Bucket name."),
    ],
) -> None:
    """Create a new bucket. The bootstrap admin becomes its owner."""
    from hsync_server.auth_store import BOOTSTRAP_ADMIN_ID
    from hsync_server.bucket_provisioning import create_bucket
    from hsync_server.exceptions import ConflictError

    try:
        create_bucket(
            bucket,
            owner_account_id=BOOTSTRAP_ADMIN_ID,
            owner_name="admin",
        )
    except ConflictError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Bucket created: {bucket}")


@bucket_cli_app.command("delete")
def bucket_delete(
    bucket: Annotated[str, typer.Option("--bucket", help="Bucket name.")],
    db_only: Annotated[
        bool,
        typer.Option(
            "--db-only",
            help="Remove only the DB rows (cleanup of ghost buckets whose directory no longer exists).",
        ),
    ] = False,
) -> None:
    """Delete a bucket and all its contents."""
    from hsync_server.bucket_provisioning import delete_bucket
    from hsync_server.exceptions import BucketNotFoundError

    try:
        delete_bucket(bucket, delete_files=not db_only)
    except BucketNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Bucket deleted: {bucket}")


@bucket_cli_app.command("grant")
def bucket_grant(
    bucket: Annotated[str, typer.Option("--bucket")],
    client: Annotated[str, typer.Option("--client", help="Account name or id.")],
) -> None:
    """Grant membership to an account on a bucket."""
    from hsync_server.auth_store import (
        BOOTSTRAP_ADMIN_ID,
        ConflictError,
        ForbiddenError,
        get_auth_store,
    )

    store = get_auth_store()
    # CLI operates as admin (filesystem authority).
    target = _resolve_client_target(client)
    try:
        store.grant_member(bucket, target, BOOTSTRAP_ADMIN_ID, caller_is_admin=True)
    except (ConflictError, ForbiddenError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Granted: {target.name} → member of {bucket}")


@bucket_cli_app.command("revoke")
def bucket_revoke(
    bucket: Annotated[str, typer.Option("--bucket")],
    client: Annotated[str, typer.Option("--client", help="Account name or id.")],
) -> None:
    """Revoke membership from a bucket."""
    from hsync_server.auth_store import (
        BOOTSTRAP_ADMIN_ID,
        ConflictError,
        ForbiddenError,
        TargetNotFoundError,
        get_auth_store,
    )

    store = get_auth_store()
    target = _resolve_client_target(client)
    try:
        store.revoke_member(bucket, target.id, BOOTSTRAP_ADMIN_ID, caller_is_admin=True)
    except (ConflictError, ForbiddenError, TargetNotFoundError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Revoked: {target.name} from {bucket}")


@bucket_cli_app.command("set-owner")
def bucket_set_owner(
    bucket: Annotated[str, typer.Option("--bucket")],
    client: Annotated[str, typer.Option("--client", help="Account name or id.")],
) -> None:
    """Set or transfer bucket ownership."""
    from pathlib import Path

    from hsync_server.auth_store import (
        BOOTSTRAP_ADMIN_ID,
        ConflictError,
        ForbiddenError,
        get_auth_store,
    )
    from hsync_server.config import get_buckets_path

    store = get_auth_store()
    target = _resolve_client_target(client)

    bucket_dir = Path(get_buckets_path()) / bucket
    if not bucket_dir.exists():
        typer.echo("Error: bucket directory does not exist", err=True)
        raise typer.Exit(1)

    current_owner = store.get_owner(bucket)
    try:
        if current_owner is None:
            store.reclaim_orphan(bucket, target, caller_is_admin=True)
            action = "reclaimed"
        else:
            store.transfer_owner(bucket, target, BOOTSTRAP_ADMIN_ID, caller_is_admin=True)
            action = "transferred"
    except (ConflictError, ForbiddenError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Ownership {action}: {bucket} → {target.name}")


def _cli_parse_expiry(expires_at: str | None, valid_days: int | None) -> int | None:
    """Parse expires_at or valid_days into a Unix UTC timestamp."""
    from hsync_server.auth_store import parse_expiry

    try:
        return parse_expiry(expires_at, valid_days)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    run()
