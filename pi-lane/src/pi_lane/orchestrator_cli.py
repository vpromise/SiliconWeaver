"""CLI client for the durable local Pi Lane orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .orchestrator import (
    DEFAULT_DB,
    DEFAULT_LOCK,
    DEFAULT_LOG,
    DEFAULT_SOCKET,
    ORCHESTRATOR_BUILD_ID,
    PROTOCOL_VERSION,
    OrchestratorError,
    ensure_private_directory,
    error_envelope,
    rtl_closure_runtime_config_fingerprint,
    unix_socket_endpoint_state,
)


MAX_RESPONSE_BYTES = 1024 * 1024
MUTATION_TIMEOUT_SECONDS = 240
_DAEMON_PROCESSES: list[tuple[Path, subprocess.Popen[bytes]]] = []
_DAEMON_PROCESSES_LOCK = threading.Lock()


def _daemon_environment() -> dict[str, str]:
    """Resolve the daemon runtime without exposing credentials to Pi itself."""

    environment = os.environ.copy()
    configured_state = environment.get("XDG_STATE_HOME")
    state_home = (
        Path(configured_state).expanduser()
        if configured_state
        else Path.home() / ".local" / "state"
    )
    agent_dir = Path(
        environment.get(
            "PI_LANE_AGENT_DIR", state_home / "pi-lane" / "agent"
        )
    ).expanduser()
    # Imported lazily so the interactive launcher and control-plane CLI retain
    # independent startup paths while sharing the same bounded private reader.
    from .launcher import _rtl_closure_environment

    return _rtl_closure_environment(agent_dir, environment)


def _track_daemon(socket_path: Path, process: subprocess.Popen[bytes]) -> None:
    with _DAEMON_PROCESSES_LOCK:
        _DAEMON_PROCESSES.append((socket_path, process))


def _forget_daemon(process: subprocess.Popen[bytes]) -> None:
    with _DAEMON_PROCESSES_LOCK:
        _DAEMON_PROCESSES[:] = [item for item in _DAEMON_PROCESSES if item[1] is not process]


def _reap_daemons(socket_path: Path) -> None:
    with _DAEMON_PROCESSES_LOCK:
        selected = [item for item in _DAEMON_PROCESSES if item[0] == socket_path]
    for _, process in selected:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        _forget_daemon(process)


def _request_with_protocol(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    protocol_version: int,
    socket_path: Path = DEFAULT_SOCKET,
    timeout: float = 35,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    payload = {
        "protocol_version": protocol_version,
        "request_id": request_id,
        "method": method,
        "params": params or {},
    }
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(encoded)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise OrchestratorError("RESPONSE_TOO_LARGE", "daemon response exceeds 1 MiB")
                if b"\n" in chunk:
                    break
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise OrchestratorError(
            "DAEMON_UNAVAILABLE", f"Pi Lane orchestrator is unavailable: {exc}", retryable=True
        ) from exc
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError("INVALID_DAEMON_JSON", f"daemon returned invalid JSON: {exc}") from exc
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        raise OrchestratorError("INVALID_DAEMON_RESPONSE", "daemon response identity is invalid")
    if response.get("protocol_version") != protocol_version:
        raise OrchestratorError(
            "INVALID_DAEMON_RESPONSE", "daemon response protocol version is invalid"
        )
    expected_kind = method if response.get("ok") is True else "error"
    if response.get("kind") != expected_kind:
        raise OrchestratorError(
            "INVALID_DAEMON_RESPONSE", "daemon response method identity is invalid"
        )
    if (
        method == "daemon.stop"
        and response.get("ok") is True
        and isinstance(response.get("data"), dict)
        and response["data"].get("stopping") is True
    ):
        _reap_daemons(socket_path)
    return response


def request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    socket_path: Path = DEFAULT_SOCKET,
    timeout: float = 35,
) -> dict[str, Any]:
    """Send one request using the current public orchestrator protocol."""

    return _request_with_protocol(
        method,
        params,
        protocol_version=PROTOCOL_VERSION,
        socket_path=socket_path,
        timeout=timeout,
    )


def _recognized_daemon_response(
    response: Mapping[str, Any], *, protocol_version: int
) -> bool:
    data = response.get("data")
    return bool(
        response.get("ok") is True
        and isinstance(data, dict)
        and data.get("ready") is True
        and type(data.get("pid")) is int
        and data["pid"] > 0
        and data.get("protocol_version") == protocol_version
        and isinstance(data.get("build_id"), str)
        and data["build_id"].startswith("pi-lane-orchestrator-")
    )


def _daemon_ping(socket_path: Path) -> dict[str, Any] | None:
    try:
        response = request("daemon.ping", socket_path=socket_path, timeout=1)
    except OrchestratorError:
        return None
    if _recognized_daemon_response(response, protocol_version=PROTOCOL_VERSION):
        return response
    return None


def _daemon_matches_instance(
    response: dict[str, Any],
    *,
    socket_path: Path,
    db_path: Path,
    lock_path: Path,
    runtime_config_fingerprint: str,
) -> bool:
    data = response.get("data") or {}
    return bool(
        data.get("build_id") == ORCHESTRATOR_BUILD_ID
        and data.get("runtime_config_fingerprint") == runtime_config_fingerprint
        and _daemon_matches_paths(
            response,
            socket_path=socket_path,
            db_path=db_path,
            lock_path=lock_path,
        )
    )


def _daemon_matches_paths(
    response: dict[str, Any],
    *,
    socket_path: Path,
    db_path: Path,
    lock_path: Path,
) -> bool:
    data = response.get("data") or {}
    return bool(
        data.get("socket_path") == str(socket_path)
        and data.get("db_path") == str(db_path)
        and data.get("lock_path") == str(lock_path)
    )


def _deferred_handoff(
    response: dict[str, Any], *, reason: str
) -> dict[str, Any]:
    data = dict(response.get("data") or {})
    data.update({"reload_deferred": True, "reload_deferred_reason": reason})
    return {**response, "data": data}


def ensure_daemon(
    *,
    socket_path: Path = DEFAULT_SOCKET,
    db_path: Path | None = None,
    lock_path: Path | None = None,
    log_path: Path | None = None,
    wait_seconds: float = 8,
) -> dict[str, Any]:
    socket_path = socket_path.expanduser().resolve()
    root = socket_path.parent
    default_socket = DEFAULT_SOCKET.expanduser().resolve()
    db_path = (
        db_path
        or (DEFAULT_DB if socket_path == default_socket else root / "orchestrator.sqlite3")
    ).expanduser().resolve()
    lock_path = (
        lock_path
        or (DEFAULT_LOCK if socket_path == default_socket else root / "orchestrator.lock")
    ).expanduser().resolve()
    log_path = (
        log_path
        or (DEFAULT_LOG if socket_path == default_socket else root / "orchestrator.log")
    ).expanduser().resolve()
    daemon_environment = _daemon_environment()
    expected_runtime_config_fingerprint = rtl_closure_runtime_config_fingerprint(
        daemon_environment
    )
    existing = _daemon_ping(socket_path)
    if existing is None:
        if socket_path.exists():
            endpoint_state, _identity = unix_socket_endpoint_state(socket_path)
            if endpoint_state not in {"absent", "stale"}:
                raise OrchestratorError(
                    "DAEMON_SOCKET_IN_USE",
                    "the requested Unix socket is owned by an unrecognized live service",
                    category="state",
                )
    if existing is not None:
        paths_match = _daemon_matches_paths(
            existing,
            socket_path=socket_path,
            db_path=db_path,
            lock_path=lock_path,
        )
        if _daemon_matches_instance(
            existing,
            socket_path=socket_path,
            db_path=db_path,
            lock_path=lock_path,
            runtime_config_fingerprint=expected_runtime_config_fingerprint,
        ):
            return existing
        existing_data = existing.get("data") or {}
        if existing_data.get("handoff_protocol_version") != 1:
            if paths_match:
                return _deferred_handoff(
                    existing, reason="running daemon lacks drain-aware handoff"
                )
            raise OrchestratorError(
                "DAEMON_RELOAD_BUSY",
                "the running daemon cannot be replaced safely for different instance paths",
                category="state",
                retryable=True,
            )
        stopped = request("daemon.stop", socket_path=socket_path, timeout=2)
        if stopped.get("ok") is not True:
            raise OrchestratorError(
                "DAEMON_RELOAD_FAILED",
                "running orchestrator has a different build or runtime configuration "
                "and could not be stopped",
                retryable=True,
            )
        stop_data = stopped.get("data") or {}
        if stop_data.get("busy") is True:
            if paths_match:
                return _deferred_handoff(
                    existing, reason="running daemon is draining active mutations"
                )
            raise OrchestratorError(
                "DAEMON_RELOAD_BUSY",
                "the running daemon has active mutations and uses different instance paths",
                category="state",
                retryable=True,
            )
        if stop_data.get("stopping") is not True:
            raise OrchestratorError(
                "DAEMON_RELOAD_FAILED",
                "running orchestrator did not confirm a drain-safe handoff",
                retryable=True,
            )
        reload_deadline = time.monotonic() + min(wait_seconds, 5)
        while time.monotonic() < reload_deadline:
            replacement = _daemon_ping(socket_path)
            if (
                replacement is not None
                and _daemon_matches_instance(
                    replacement,
                    socket_path=socket_path,
                    db_path=db_path,
                    lock_path=lock_path,
                    runtime_config_fingerprint=expected_runtime_config_fingerprint,
                )
            ):
                return replacement
            if replacement is None and not socket_path.exists():
                # The server removes the socket immediately before releasing
                # its process lock. Let that short critical section finish.
                time.sleep(0.05)
                break
            time.sleep(0.05)
        else:
            raise OrchestratorError(
                "DAEMON_RELOAD_FAILED",
                "running orchestrator did not stop for automatic build handoff",
                retryable=True,
            )
    for directory, label in (
        (root, "orchestrator socket parent"),
        (db_path.parent, "orchestrator database parent"),
        (lock_path.parent, "orchestrator lock parent"),
        (log_path.parent, "orchestrator log parent"),
    ):
        ensure_private_directory(directory, label=label)
    output = log_path.open("ab", buffering=0)
    os.fchmod(output.fileno(), 0o600)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_lane.orchestrator_server",
            "--db",
            str(db_path),
            "--socket",
            str(socket_path),
            "--lock",
            str(lock_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=daemon_environment,
    )
    _track_daemon(socket_path, process)
    output.close()
    deadline = time.monotonic() + wait_seconds
    launcher_reaped = False
    while time.monotonic() < deadline:
        ready = _daemon_ping(socket_path)
        if (
            ready is not None
            and _daemon_matches_instance(
                ready,
                socket_path=socket_path,
                db_path=db_path,
                lock_path=lock_path,
                runtime_config_fingerprint=expected_runtime_config_fingerprint,
            )
        ):
            return ready
        # A concurrent launcher can lose the daemon lock and exit before the
        # winning process has bound its socket. Keep waiting for that winner.
        if not launcher_reaped and process.poll() is not None:
            process.wait()
            _forget_daemon(process)
            launcher_reaped = True
        time.sleep(0.05)
    detail = ""
    try:
        detail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
    except OSError:
        pass
    raise OrchestratorError(
        "DAEMON_START_FAILED",
        "Pi Lane orchestrator did not become ready" + (f": {detail}" if detail else ""),
        retryable=True,
    )


def _print(response: dict[str, Any], *, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(response, indent=2, ensure_ascii=False))
    elif response.get("ok"):
        data = response.get("data")
        if isinstance(data, dict):
            for key, value in data.items():
                if value is not None and key not in {"snapshot", "attempts"}:
                    print(f"{key}: {value}")
        else:
            print(data)
    else:
        error = response.get("error") or {}
        print(f"pi-lanectl: error: {error.get('code')}: {error.get('message')}", file=sys.stderr)
    return 0 if response.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--log", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a natural-language RTL-to-GDS workflow")
    start.add_argument("--spec", required=True, type=Path)
    start.add_argument("--idempotency-key")
    start.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="show a compact, read-only workflow snapshot")
    status.add_argument("--run-id", dest="workflow_id")
    status.add_argument("--compact", action="store_true")
    status.add_argument("--json", action="store_true")

    events = commands.add_parser("events", help="long-poll durable workflow events")
    events.add_argument("--run-id", dest="workflow_id")
    events.add_argument("--after-sequence", type=int, default=0)
    events.add_argument("--limit", type=int, default=256)
    events.add_argument("--wait-seconds", type=float, default=0)
    events.add_argument("--max-bytes", type=int, default=512 * 1024)
    events.add_argument("--client-id")
    events.add_argument("--acknowledge", action="store_true")
    events.add_argument("--json", action="store_true")

    listing = commands.add_parser("list", help="list workflows")
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json", action="store_true")

    stop = commands.add_parser("stop", help="stop a workflow")
    stop.add_argument("--run-id", dest="workflow_id", required=True)
    stop.add_argument("--json", action="store_true")

    retry = commands.add_parser("retry", help="retry an explicitly selected stage")
    retry.add_argument("--run-id", dest="workflow_id", required=True)
    retry.add_argument("--scope", choices=("formal", "model", "lane"), required=True)
    retry.add_argument("--json", action="store_true")

    rerun = commands.add_parser("rerun", help="create a successor for a terminal workflow")
    rerun.add_argument("--run-id", dest="workflow_id", required=True)
    rerun.add_argument("--idempotency-key")
    rerun.add_argument("--json", action="store_true")

    daemon = commands.add_parser("daemon", help="control the local orchestrator")
    daemon.add_argument("action", choices=("ensure", "status", "stop"))
    daemon.add_argument("--json", action="store_true")
    return parser


def _command_mutates(args: argparse.Namespace) -> bool:
    if args.command in {"start", "stop", "retry", "rerun"}:
        return True
    return args.command == "events" and bool(args.acknowledge)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_mode = bool(getattr(args, "json", False))
    try:
        if args.command == "daemon":
            if args.action == "ensure":
                return _print(
                    ensure_daemon(
                        socket_path=args.socket,
                        db_path=args.db,
                        lock_path=args.lock,
                        log_path=args.log,
                    ),
                    json_mode=json_mode,
                )
            if args.action == "status":
                response = request("daemon.ping", socket_path=args.socket, timeout=2)
                return _print(response, json_mode=json_mode)
            response = request("daemon.stop", socket_path=args.socket, timeout=2)
            return _print(response, json_mode=json_mode)

        daemon = ensure_daemon(
            socket_path=args.socket,
            db_path=args.db,
            lock_path=args.lock,
            log_path=args.log,
        )
        daemon_data = daemon.get("data") if isinstance(daemon, dict) else None
        if (
            isinstance(daemon_data, dict)
            and daemon_data.get("reload_deferred") is True
            and _command_mutates(args)
        ):
            raise OrchestratorError(
                "DAEMON_RELOAD_DEFERRED",
                "the running orchestrator has a different build or runtime configuration "
                "and is still draining; retry after handoff completes",
                category="state",
                retryable=True,
            )
        if args.command == "start":
            params = {"spec": str(args.spec.expanduser().resolve())}
            if args.idempotency_key:
                params["idempotency_key"] = args.idempotency_key
            response = request(
                "run.start",
                params,
                socket_path=args.socket,
                timeout=MUTATION_TIMEOUT_SECONDS,
            )
        elif args.command == "status":
            response = request(
                "run.get",
                {"workflow_id": args.workflow_id} if args.workflow_id else {},
                socket_path=args.socket,
            )
        elif args.command == "events":
            params = {
                "after_sequence": args.after_sequence,
                "limit": args.limit,
                "wait_seconds": args.wait_seconds,
                "acknowledge": args.acknowledge,
                "max_bytes": args.max_bytes,
            }
            if args.workflow_id:
                params["workflow_id"] = args.workflow_id
            if args.client_id:
                params["client_id"] = args.client_id
            response = request(
                "run.events", params, socket_path=args.socket, timeout=max(35, args.wait_seconds + 5)
            )
        elif args.command == "list":
            response = request(
                "run.list",
                {"active_only": args.active, "limit": args.limit},
                socket_path=args.socket,
            )
        elif args.command == "stop":
            response = request(
                "run.stop", {"workflow_id": args.workflow_id}, socket_path=args.socket
            )
        elif args.command == "retry":
            response = request(
                "run.retry",
                {"workflow_id": args.workflow_id, "scope": args.scope},
                socket_path=args.socket,
                timeout=MUTATION_TIMEOUT_SECONDS,
            )
        elif args.command == "rerun":
            params = {"workflow_id": args.workflow_id}
            if args.idempotency_key:
                params["idempotency_key"] = args.idempotency_key
            response = request(
                "run.rerun",
                params,
                socket_path=args.socket,
                timeout=MUTATION_TIMEOUT_SECONDS,
            )
        else:
            raise OrchestratorError(
                "INVALID_COMMAND", f"unsupported command: {args.command}"
            )
        return _print(response, json_mode=json_mode)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        response = error_envelope(exc)
        return _print(response, json_mode=json_mode)


if __name__ == "__main__":
    raise SystemExit(main())
