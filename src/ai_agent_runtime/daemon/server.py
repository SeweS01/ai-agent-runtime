"""Local Runtime daemon and fail-closed launch authorization."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import struct
import uuid
from collections import OrderedDict
from typing import Any

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.domain import LifecycleState, WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.ipc.protocol import read_frame, validate_version, versioned, write_frame
from ai_agent_runtime.process_safety import validate_owned_executable, validate_profile_path
from ai_agent_runtime.state.store import RuntimeStore
from ai_agent_runtime.systemd_notify import notify, watchdog_loop
from ai_agent_runtime.telegram_upstream.correlation import phase_a_correlate, phase_b_verify
from ai_agent_runtime.telegram_upstream.tmux_state import read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config

_PEERCRED = struct.Struct("3i")


class RuntimeServer:
    def __init__(self, config: RuntimeConfig, *, phase_b_timeout: float = 45.0) -> None:
        self.config = config
        self.phase_b_timeout = phase_b_timeout
        self.store = RuntimeStore(config.database_path)
        self.boot_id = str(uuid.uuid4())
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()

    async def serve(self) -> None:
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.socket_path.parent, 0o700)
        self.config.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=self.config.socket_path)
        os.chmod(self.config.socket_path, 0o600)
        watchdog = asyncio.create_task(watchdog_loop())
        notify("READY=1")
        try:
            async with server:
                await server.serve_forever()
        finally:
            notify("STOPPING=1")
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            self.config.socket_path.unlink(missing_ok=True)
            self.store.close()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id = "unknown"
        try:
            peer_pid, peer_uid = self._validate_peer(writer)
            payload = await read_frame(reader)
            validate_version(payload)
            request_id = _required_str(payload, "request_id")
            cached = self._results.get(request_id)
            if cached is not None:
                await write_frame(writer, cached)
                return
            action = _required_str(payload, "action")
            if action == "register":
                response = self._register(request_id, payload, peer_pid, peer_uid)
            elif action == "verify_started":
                response = await self._verify_started(request_id, payload, peer_pid, peer_uid)
            elif action == "status":
                response = self._ok(request_id, summary=self.store.summary())
            else:
                raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, f"unknown action: {action}")
            self._cache(request_id, response)
            await write_frame(writer, response)
        except RuntimeFailureError as exc:
            await write_frame(writer, self._error(request_id, exc.code, exc.detail))
        except (ValueError, KeyError, OSError) as exc:
            await write_frame(
                writer,
                self._error(request_id, ErrorCode.INVALID_REQUEST, str(exc)),
            )
        finally:
            writer.close()
            await writer.wait_closed()

    def _validate_peer(self, writer: asyncio.StreamWriter) -> tuple[int, int]:
        transport_socket = writer.get_extra_info("socket")
        if transport_socket is None:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "missing peer socket")
        raw = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
        _pid, uid, _gid = _PEERCRED.unpack(raw)
        if uid != os.getuid():
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "Unix peer UID mismatch")
        return _pid, uid

    def _register(
        self,
        request_id: str,
        payload: dict[str, Any],
        peer_pid: int,
        peer_uid: int,
    ) -> dict[str, Any]:
        if payload.get("agent_instance_id") != self.config.agent_instance_id:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "AgentInstance namespace mismatch")
        identity = _identity(payload)
        _verify_claimed_peer(identity, peer_pid=peer_pid, peer_uid=peer_uid)
        config_snapshot = read_topic_config(self.config.topic_config_path)
        binding = phase_a_correlate(
            identity=identity,
            bindings=self.store.list_bindings(self.config.agent_instance_id),
            topic_config=config_snapshot,
        )
        if not binding.configured:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "binding is not configured")
        effective = self.store.get_effective_spec(binding.binding_id)
        if effective is None:
            raise RuntimeFailureError(
                ErrorCode.UNKNOWN_BINDING, "binding has no verified local spec"
            )
        spec, codex_home, profile_fingerprint = effective
        if spec.agent_instance_id != self.config.agent_instance_id:
            raise RuntimeFailureError(
                ErrorCode.UNKNOWN_BINDING, "effective spec namespace mismatch"
            )
        resume_session_id = payload.get("resume_session_id")
        if resume_session_id is not None:
            if not isinstance(resume_session_id, str) or not resume_session_id:
                raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "invalid resume session id")
            owner = self.store.session_owner(resume_session_id)
            if owner != (binding.binding_id, spec.account_profile):
                raise RuntimeFailureError(
                    ErrorCode.RESUME_BINDING_MISMATCH,
                    "session does not belong to this binding and account",
                )
        real_codex = validate_owned_executable(self.config.real_codex)
        safe_home = validate_profile_path(codex_home, root=self.config.profiles_root)
        instance_id = str(uuid.uuid4())
        self.store.record_instance(
            instance_id=instance_id,
            binding_id=binding.binding_id,
            wrapper_pid=identity.pid,
            wrapper_starttime=identity.starttime,
            child_pid=None,
            child_starttime=None,
            tmux_session=identity.tmux_session,
            tmux_pane=identity.tmux_pane,
            lifecycle=LifecycleState.PROVISIONAL_STARTING,
            applied_digest=None,
        )
        return self._ok(
            request_id,
            instance_id=instance_id,
            binding_id=binding.binding_id,
            spec={
                "generation_id": spec.generation_id,
                "revision": spec.revision,
                "spec_digest": spec.spec_digest,
                "account_profile": spec.account_profile,
                "model": spec.model,
                "reasoning_effort": spec.reasoning_effort,
                "codex_home": str(safe_home),
                "profile_fingerprint": profile_fingerprint,
                "real_codex": str(real_codex),
            },
        )

    async def _verify_started(
        self,
        request_id: str,
        payload: dict[str, Any],
        peer_pid: int,
        peer_uid: int,
    ) -> dict[str, Any]:
        instance_id = _required_str(payload, "instance_id")
        binding_id = _required_str(payload, "binding_id")
        binding = self.store.get_binding(binding_id)
        if binding is None:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "binding disappeared")
        identity = _identity(payload)
        _verify_claimed_peer(identity, peer_pid=peer_pid, peer_uid=peer_uid)
        child_pid = _required_int(payload, "child_pid")
        child_starttime = _required_int(payload, "child_starttime")
        deadline = asyncio.get_running_loop().time() + self.phase_b_timeout
        last_failure: RuntimeFailureError | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = phase_b_verify(
                    binding=binding,
                    identity=identity,
                    states=read_tmux_states(self.config.tmux_state_path),
                    topic_config=read_topic_config(self.config.topic_config_path),
                )
                break
            except RuntimeFailureError as exc:
                last_failure = exc
                await asyncio.sleep(0.2)
        else:
            detail = str(last_failure) if last_failure else "Phase B timed out"
            raise RuntimeFailureError(ErrorCode.CORRELATION_MISMATCH, detail)
        effective = self.store.get_effective_spec(binding_id)
        if effective is None:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "effective spec disappeared")
        spec, _codex_home, _fingerprint = effective
        self.store.record_instance(
            instance_id=instance_id,
            binding_id=binding_id,
            wrapper_pid=identity.pid,
            wrapper_starttime=identity.starttime,
            child_pid=child_pid,
            child_starttime=child_starttime,
            tmux_session=identity.tmux_session,
            tmux_pane=identity.tmux_pane,
            lifecycle=LifecycleState.IDLE,
            applied_digest=spec.spec_digest,
        )
        if state.session_id:
            self.store.record_session_provenance(
                session_id=state.session_id,
                binding_id=binding_id,
                account_profile=spec.account_profile,
            )
        return self._ok(request_id, verified=True, session_id=state.session_id)

    def _cache(self, request_id: str, result: dict[str, Any]) -> None:
        self._results[request_id] = result
        self._results.move_to_end(request_id)
        while len(self._results) > 1024:
            self._results.popitem(last=False)

    def _ok(self, request_id: str, **payload: Any) -> dict[str, Any]:
        return versioned(
            "response",
            request_id,
            ok=True,
            daemon_boot_id=self.boot_id,
            **payload,
        )

    def _error(self, request_id: str, code: ErrorCode, detail: str) -> dict[str, Any]:
        return versioned(
            "response",
            request_id,
            ok=False,
            daemon_boot_id=self.boot_id,
            error={"code": code, "detail": detail},
        )


def _identity(payload: dict[str, Any]) -> WrapperIdentity:
    wrapper = payload.get("wrapper")
    if not isinstance(wrapper, dict):
        raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "wrapper identity is required")
    return WrapperIdentity(
        pid=_required_int(wrapper, "pid"),
        starttime=_required_int(wrapper, "starttime"),
        uid=_required_int(wrapper, "uid"),
        tmux_session=_required_str(payload, "tmux_session"),
        tmux_pane=_required_str(payload, "tmux_pane"),
        cwd=_required_str(payload, "cwd"),
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, f"{key} must be a non-empty string")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, f"{key} must be an integer")
    return value


def _verify_claimed_peer(
    identity: WrapperIdentity,
    *,
    peer_pid: int,
    peer_uid: int,
) -> None:
    if identity.pid != peer_pid or identity.uid != peer_uid:
        raise RuntimeFailureError(
            ErrorCode.INVALID_REQUEST,
            "claimed wrapper PID/UID does not match Unix peer credentials",
        )
