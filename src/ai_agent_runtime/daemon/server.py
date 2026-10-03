"""Local Runtime daemon and fail-closed launch authorization."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import struct
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.domain import (
    DesiredSpec,
    LifecycleState,
    ManagementMode,
    RuntimeBinding,
    WrapperIdentity,
    sha256_digest,
)
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.ipc.protocol import read_frame, validate_version, versioned, write_frame
from ai_agent_runtime.process_safety import (
    profile_fingerprint,
    validate_owned_executable,
    validate_profile_path,
    validate_project_path,
    verify_wrapper_identity,
)
from ai_agent_runtime.state.store import RuntimeStore
from ai_agent_runtime.systemd_notify import notify, watchdog_loop
from ai_agent_runtime.telegram_upstream.adapter import TelegramUpstreamAdapter
from ai_agent_runtime.telegram_upstream.correlation import phase_a_correlate, phase_b_verify
from ai_agent_runtime.telegram_upstream.tmux_state import read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import (
    read_topic_config,
    reconcile_managed_topic,
)

_PEERCRED = struct.Struct("3i")


class RuntimeServer:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        phase_b_timeout: float = 45.0,
        identity_verifier: Callable[[WrapperIdentity], None] = verify_wrapper_identity,
    ) -> None:
        self.config = config
        self.phase_b_timeout = phase_b_timeout
        self.store = RuntimeStore(config.database_path)
        self.boot_id = str(uuid.uuid4())
        self.identity_verifier = identity_verifier
        self._results: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

    async def serve(self) -> None:
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.socket_path.parent, 0o700)
        self.config.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=self.config.socket_path)
        os.chmod(self.config.socket_path, 0o600)
        watchdog = asyncio.create_task(watchdog_loop())
        reconciler = asyncio.create_task(self._reconcile_topic_config_loop())
        notify("READY=1")
        try:
            async with server:
                await server.serve_forever()
        finally:
            notify("STOPPING=1")
            watchdog.cancel()
            reconciler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            with contextlib.suppress(asyncio.CancelledError):
                await reconciler
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
            request_digest = sha256_digest(payload)
            cached_memory = self._results.get(request_id)
            if cached_memory is not None:
                cached_digest, cached_response = cached_memory
                if cached_digest != request_digest:
                    raise RuntimeFailureError(
                        ErrorCode.INVALID_REQUEST,
                        "request_id replayed with different payload",
                    )
                await write_frame(writer, cached_response)
                return
            cached_durable = self.store.get_request_result(request_id, request_digest)
            if cached_durable is not None:
                cached_response = dict(cached_durable)
                cached_response["daemon_boot_id"] = self.boot_id
                await write_frame(writer, cached_response)
                return
            action = _required_str(payload, "action")
            if action == "register":
                response = self._register(request_id, payload, peer_pid, peer_uid)
            elif action == "verify_started":
                response = await self._verify_started(request_id, payload, peer_pid, peer_uid)
            elif action == "status":
                response = self._ok(request_id, summary=self.store.summary())
            elif action == "apply_binding":
                response = self._apply_binding(request_id, payload)
            elif action == "outbox_pull":
                limit = payload.get("limit", 50)
                if not isinstance(limit, int):
                    raise RuntimeFailureError(
                        ErrorCode.INVALID_REQUEST, "outbox limit must be an integer"
                    )
                response = self._ok(
                    request_id,
                    events=self.store.claim_outbox(limit=limit),
                )
            elif action == "outbox_ack":
                self.store.acknowledge_outbox(
                    _required_str(payload, "event_id"),
                    _required_str(payload, "ack_state"),
                )
                response = self._ok(request_id, acknowledged=True)
            else:
                raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, f"unknown action: {action}")
            self._cache(request_id, request_digest, response)
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
        self.identity_verifier(identity)
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

    def _apply_binding(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_binding = payload.get("binding")
        raw_desired = payload.get("desired")
        if not isinstance(raw_binding, dict) or not isinstance(raw_desired, dict):
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "binding and desired objects are required",
            )
        agent_instance_id = _required_str(raw_binding, "agent_instance_id")
        if agent_instance_id != self.config.agent_instance_id:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "AgentInstance mismatch")
        chat_id = _required_int(raw_binding, "chat_id")
        if chat_id != self.config.forum_chat_id:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "forum chat mismatch")
        thread_id = _required_int(raw_binding, "thread_id")
        if thread_id <= 0:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "thread_id must be positive")
        binding_id = _required_str(raw_binding, "binding_id")
        project_id = _required_str(raw_binding, "project_id")
        requested_cwd = validate_project_path(
            Path(_required_str(raw_binding, "canonical_cwd")),
            allowed_roots=self.config.allowed_project_roots,
        )
        if raw_desired.get("binding_id") != binding_id:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "desired binding_id mismatch")
        spec = _desired_spec(raw_desired)
        if spec.agent_instance_id != agent_instance_id:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "desired AgentInstance mismatch")
        if spec.project_id != project_id or Path(spec.canonical_cwd) != requested_cwd:
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST, "desired project identity mismatch"
            )
        if spec.provider != "codex":
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST, "V1 managed provider must be codex"
            )
        existing_binding = self.store.get_binding(binding_id)
        if existing_binding is not None:
            if (
                existing_binding.agent_instance_id != agent_instance_id
                or existing_binding.chat_id != chat_id
                or existing_binding.thread_id != thread_id
                or existing_binding.project_id != project_id
            ):
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "binding identity is immutable",
                )
            if Path(existing_binding.canonical_cwd) != requested_cwd:
                raise RuntimeFailureError(
                    ErrorCode.PROJECT_RELOCATION_REQUIRED,
                    "project path change requires explicit relocation",
                )
        for known in self.store.list_bindings(agent_instance_id):
            if (
                known.chat_id == chat_id
                and known.thread_id == thread_id
                and known.binding_id != binding_id
            ):
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "Telegram topic is already assigned to another binding",
                )
        try:
            self.store.validate_effective_spec_update(spec)
        except ValueError as exc:
            code = (
                ErrorCode.GENERATION_RECONCILE_REQUIRED
                if "generation" in str(exc)
                else ErrorCode.INVALID_REQUEST
            )
            raise RuntimeFailureError(code, str(exc)) from exc
        profile_name = spec.account_profile
        if (
            not profile_name
            or profile_name in {".", ".."}
            or Path(profile_name).name != profile_name
        ):
            raise RuntimeFailureError(ErrorCode.UNSAFE_PROFILE, "invalid account profile name")
        codex_home = validate_profile_path(
            self.config.profiles_root / profile_name,
            root=self.config.profiles_root,
        )
        fingerprint = profile_fingerprint(codex_home)
        adapter = TelegramUpstreamAdapter(
            self.config.upstream_sha,
            self.config.tmux_session_prefix,
        )
        expected_session = adapter.expected_session_name(
            chat_id=chat_id,
            thread_id=thread_id,
        )
        snapshot = None
        for _ in range(3):
            before = read_topic_config(self.config.topic_config_path)
            try:
                snapshot = reconcile_managed_topic(
                    self.config.topic_config_path,
                    thread_id=thread_id,
                    cwd=str(requested_cwd),
                    expected_digest=before.digest,
                    backup_dir=self.config.state_dir / "topic-config-backups",
                )
                break
            except KeyError as exc:
                raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, str(exc)) from exc
            except RuntimeError:
                continue
        if snapshot is None:
            raise RuntimeFailureError(
                ErrorCode.CONFIG_DRIFT,
                "topic_config changed during all reconciliation attempts",
            )
        binding = RuntimeBinding(
            binding_id=binding_id,
            agent_instance_id=agent_instance_id,
            chat_id=chat_id,
            thread_id=thread_id,
            project_id=project_id,
            canonical_cwd=str(requested_cwd),
            expected_tmux_session=expected_session,
            configured=True,
            config_digest=snapshot.digest,
            management_mode=ManagementMode.MANAGED,
        )
        self.store.upsert_binding(binding)
        self.store.put_effective_spec(
            spec,
            codex_home=codex_home,
            profile_fingerprint=fingerprint,
        )
        return self._ok(
            request_id,
            binding_id=binding_id,
            config_digest=snapshot.digest,
            local_effective={
                "generation_id": spec.generation_id,
                "revision": spec.revision,
                "spec_digest": spec.spec_digest,
                "profile_fingerprint": fingerprint,
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
        self.identity_verifier(identity)
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
            # Phase B proves binding/process correlation, but a new Codex
            # session's rollout identity is not necessarily durable yet.
            # AppliedSpec remains unset until rollout evidence is collected.
            applied_digest=None,
        )
        return self._ok(
            request_id,
            verified=True,
            applied=False,
            session_id=state.session_id,
            expected_spec_digest=spec.spec_digest,
        )

    async def _reconcile_topic_config_loop(self) -> None:
        while True:
            try:
                snapshot = read_topic_config(self.config.topic_config_path)
                for binding in self.store.list_bindings(self.config.agent_instance_id):
                    entry = snapshot.topic(binding.thread_id)
                    if entry is None:
                        continue
                    if entry.engine != "codex":
                        mode = ManagementMode.UNMANAGED
                    elif entry.exec_mode != "tmux":
                        mode = ManagementMode.UNSUPPORTED_CODEX_SUBPROCESS
                    elif (
                        entry.cwd is not None
                        and Path(entry.cwd).resolve() == Path(binding.canonical_cwd).resolve()
                    ):
                        mode = ManagementMode.MANAGED
                    else:
                        continue
                    self.store.update_binding_observation(
                        binding.binding_id,
                        config_digest=snapshot.digest,
                        management_mode=mode,
                    )
            except (OSError, ValueError):
                pass
            await asyncio.sleep(1.0)

    def _cache(
        self,
        request_id: str,
        request_digest: str,
        result: dict[str, Any],
    ) -> None:
        self._results[request_id] = (request_digest, result)
        self._results.move_to_end(request_id)
        self.store.put_request_result(request_id, request_digest, result)
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


def _desired_spec(payload: dict[str, Any]) -> DesiredSpec:
    try:
        spec = DesiredSpec(
            binding_id=_required_str(payload, "binding_id"),
            agent_instance_id=_required_str(payload, "agent_instance_id"),
            project_id=_required_str(payload, "project_id"),
            canonical_cwd=_required_str(payload, "canonical_cwd"),
            provider=_required_str(payload, "provider"),
            account_profile=_required_str(payload, "account_profile"),
            model=_required_str(payload, "model"),
            reasoning_effort=_required_str(payload, "reasoning_effort"),
            generation_id=_required_str(payload, "generation_id"),
            revision=_required_int(payload, "revision"),
            spec_digest=_required_str(payload, "spec_digest"),
        )
    except TypeError as exc:
        raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    if not spec.verify_digest():
        raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "desired spec digest mismatch")
    return spec
