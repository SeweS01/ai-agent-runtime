"""Local Runtime daemon and fail-closed launch authorization."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import struct
import subprocess
import time
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
    ObservedSpecProposal,
    RolloutObservationCursor,
    RolloutObservationTarget,
    RuntimeBinding,
    UserNotificationCode,
    WrapperIdentity,
    sha256_digest,
)
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.ipc.protocol import read_frame, validate_version, versioned, write_frame
from ai_agent_runtime.process_safety import (
    file_sha256,
    profile_fingerprint,
    retire_tmux_session,
    validate_owned_executable,
    validate_profile_path,
    validate_project_path,
    verify_codex_child,
    verify_wrapper_identity,
)
from ai_agent_runtime.providers.codex.rollout import (
    RolloutContractError,
    RolloutSignalKind,
    scan_rollout,
)
from ai_agent_runtime.state.store import RuntimeStore
from ai_agent_runtime.systemd_notify import notify, watchdog_loop
from ai_agent_runtime.telegram_upstream.adapter import TelegramUpstreamAdapter
from ai_agent_runtime.telegram_upstream.correlation import (
    phase_a_correlate,
    phase_a_identity_candidates,
    phase_b_verify,
)
from ai_agent_runtime.telegram_upstream.tmux_state import read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import (
    read_topic_config,
    reconcile_managed_topic,
    reconcile_managed_topics,
)

_PEERCRED = struct.Struct("3i")


class RuntimeServer:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        phase_b_timeout: float = 45.0,
        identity_verifier: Callable[[WrapperIdentity], None] = verify_wrapper_identity,
        child_verifier: Callable[..., dict[str, object]] = verify_codex_child,
        retirement_executor: Callable[[WrapperIdentity], None] = retire_tmux_session,
        evidence_interval: float = 1.0,
    ) -> None:
        self.config = config
        self.phase_b_timeout = phase_b_timeout
        self.store = RuntimeStore(config.database_path)
        self.boot_id = str(uuid.uuid4())
        self.identity_verifier = identity_verifier
        self.child_verifier = child_verifier
        self.retirement_executor = retirement_executor
        self.evidence_interval = evidence_interval
        self._results: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

    async def serve(self) -> None:
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.socket_path.parent, 0o700)
        self.config.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=self.config.socket_path)
        os.chmod(self.config.socket_path, 0o600)
        watchdog = asyncio.create_task(watchdog_loop())
        reconciler = asyncio.create_task(self._reconcile_topic_config_loop())
        evidence_collector = asyncio.create_task(self._collect_applied_evidence_loop())
        rollout_observer = asyncio.create_task(self._observe_codex_rollouts_loop())
        retirement = (
            asyncio.create_task(self._retire_pending_loop())
            if self.config.retirement_enabled
            else None
        )
        notify("READY=1")
        try:
            async with server:
                await server.serve_forever()
        finally:
            notify("STOPPING=1")
            watchdog.cancel()
            reconciler.cancel()
            evidence_collector.cancel()
            rollout_observer.cancel()
            if retirement is not None:
                retirement.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            with contextlib.suppress(asyncio.CancelledError):
                await reconciler
            with contextlib.suppress(asyncio.CancelledError):
                await evidence_collector
            with contextlib.suppress(asyncio.CancelledError):
                await rollout_observer
            if retirement is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await retirement
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
            elif action == "child_exited":
                response = self._child_exited(request_id, payload, peer_pid, peer_uid)
            elif action == "heartbeat":
                response = self._heartbeat(request_id, payload, peer_pid, peer_uid)
            elif action == "status":
                response = self._ok(
                    request_id,
                    status=self.store.status_report(self.config.agent_instance_id),
                )
            elif action == "apply_binding":
                response = self._apply_binding(request_id, payload)
            elif action == "apply_snapshot":
                response = self._apply_snapshot(request_id, payload)
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
        bindings = self.store.list_bindings(self.config.agent_instance_id)
        try:
            binding = phase_a_correlate(
                identity=identity,
                bindings=bindings,
                topic_config=config_snapshot,
            )
        except RuntimeFailureError as exc:
            candidates = phase_a_identity_candidates(identity=identity, bindings=bindings)
            if exc.code is ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS and len(candidates) == 1:
                self.store.enqueue_user_notification(
                    code=UserNotificationCode.UNSUPPORTED_CODEX_SUBPROCESS,
                    binding=candidates[0],
                    event_ref=config_snapshot.digest,
                )
            raise
        if not binding.configured:
            self.store.enqueue_user_notification(
                code=UserNotificationCode.ONBOARDING_REQUIRED,
                binding=binding,
                event_ref=config_snapshot.digest,
            )
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
                self.store.enqueue_user_notification(
                    code=UserNotificationCode.RESUME_BINDING_MISMATCH,
                    binding=binding,
                    event_ref=resume_session_id,
                )
                raise RuntimeFailureError(
                    ErrorCode.RESUME_BINDING_MISMATCH,
                    "session does not belong to this binding and account",
                )
        real_codex = validate_owned_executable(self.config.real_codex)
        real_digest = file_sha256(real_codex)
        if real_digest != self.config.real_codex_sha256:
            raise RuntimeFailureError(
                ErrorCode.UNSAFE_BINARY,
                "real Codex digest differs from qualified release tuple",
            )
        safe_home = validate_profile_path(codex_home, root=self.config.profiles_root)
        instance_id = str(uuid.uuid4())
        launch_context: dict[str, object] = {
            "desired": {
                "binding_id": spec.binding_id,
                "agent_instance_id": spec.agent_instance_id,
                "project_id": spec.project_id,
                "canonical_cwd": spec.canonical_cwd,
                "provider": spec.provider,
                "account_profile": spec.account_profile,
                "model": spec.model,
                "reasoning_effort": spec.reasoning_effort,
                "generation_id": spec.generation_id,
                "revision": spec.revision,
                "spec_digest": spec.spec_digest,
            },
            "codex_home": str(safe_home),
            "profile_fingerprint": profile_fingerprint,
            "real_codex": str(real_codex),
            "real_codex_version": self.config.real_codex_version,
            "real_codex_sha256": real_digest,
        }
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
            launch_spec=launch_context,
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
                "real_codex_version": self.config.real_codex_version,
                "real_codex_sha256": real_digest,
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

    def _apply_snapshot(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        agent_instance_id = _required_str(payload, "agent_instance_id")
        if agent_instance_id != self.config.agent_instance_id:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "AgentInstance mismatch")
        generation_id = _required_str(payload, "generation_id")
        snapshot_digest = _required_str(payload, "snapshot_digest")
        raw_items = payload.get("bindings")
        unmanaged_present = "unmanaged_bindings" in payload
        raw_unmanaged = payload.get("unmanaged_bindings", [])
        raw_retired = payload.get("retired_binding_ids", [])
        if (
            not isinstance(raw_items, list)
            or not isinstance(raw_unmanaged, list)
            or not isinstance(raw_retired, list)
        ):
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "bindings, unmanaged_bindings and retired_binding_ids must be arrays",
            )
        if len(raw_items) + len(raw_unmanaged) > 1000 or len(raw_retired) > 1000:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "snapshot is too large")
        if any(not isinstance(value, str) or not value for value in raw_retired):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "invalid retired binding id")
        retired = frozenset(raw_retired)
        if len(retired) != len(raw_retired):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "duplicate retired binding id")
        normalized_items: list[dict[str, Any]] = []
        normalized_unmanaged: list[dict[str, Any]] = []
        prepared: list[tuple[RuntimeBinding, DesiredSpec, Path, str]] = []
        managed_topics: dict[int, str] = {}
        binding_ids: set[str] = set()
        tmux_sessions: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "snapshot item must be object")
            raw_binding = raw_item.get("binding")
            raw_desired = raw_item.get("desired")
            if not isinstance(raw_binding, dict) or not isinstance(raw_desired, dict):
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot item requires binding and desired objects",
                )
            binding, spec, codex_home, fingerprint = self._validate_snapshot_item(
                raw_binding,
                raw_desired,
                generation_id=generation_id,
            )
            if binding.thread_id in managed_topics:
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot contains duplicate Telegram topic",
                )
            if binding.binding_id in binding_ids or binding.expected_tmux_session in tmux_sessions:
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot contains duplicate binding or tmux identity",
                )
            binding_ids.add(binding.binding_id)
            tmux_sessions.add(binding.expected_tmux_session)
            managed_topics[binding.thread_id] = binding.canonical_cwd
            prepared.append((binding, spec, codex_home, fingerprint))
            normalized_items.append({"binding": raw_binding, "desired": raw_desired})

        for raw_item in raw_unmanaged:
            if not isinstance(raw_item, dict):
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "unmanaged snapshot item must be object",
                )
            raw_binding = raw_item.get("binding")
            raw_desired = raw_item.get("desired")
            if not isinstance(raw_binding, dict) or not isinstance(raw_desired, dict):
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "unmanaged snapshot item requires binding and desired objects",
                )
            binding, spec, codex_home, fingerprint = self._validate_snapshot_item(
                raw_binding,
                raw_desired,
                generation_id=generation_id,
            )
            if binding.binding_id in binding_ids or binding.expected_tmux_session in tmux_sessions:
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot contains duplicate binding or tmux identity",
                )
            if binding.thread_id in managed_topics:
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot contains duplicate Telegram topic",
                )
            binding_ids.add(binding.binding_id)
            tmux_sessions.add(binding.expected_tmux_session)
            prepared.append(
                (
                    RuntimeBinding(
                        binding_id=binding.binding_id,
                        agent_instance_id=binding.agent_instance_id,
                        chat_id=binding.chat_id,
                        thread_id=binding.thread_id,
                        project_id=binding.project_id,
                        canonical_cwd=binding.canonical_cwd,
                        expected_tmux_session=binding.expected_tmux_session,
                        configured=True,
                        management_mode=ManagementMode.UNMANAGED,
                    ),
                    spec,
                    codex_home,
                    fingerprint,
                )
            )
            normalized_unmanaged.append({"binding": raw_binding, "desired": raw_desired})

        digest_material = {
            "agent_instance_id": agent_instance_id,
            "generation_id": generation_id,
            "bindings": sorted(
                normalized_items,
                key=lambda item: str(item["binding"].get("binding_id", "")),
            ),
            "retired_binding_ids": sorted(retired),
        }
        if unmanaged_present:
            digest_material["unmanaged_bindings"] = sorted(
                normalized_unmanaged,
                key=lambda item: str(item["binding"].get("binding_id", "")),
            )
        if snapshot_digest != sha256_digest(digest_material):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "snapshot digest mismatch")

        known_ids = {
            binding.binding_id
            for binding in self.store.list_bindings(self.config.agent_instance_id)
        }
        active_ids = {binding.binding_id for binding, _spec, _home, _fp in prepared}
        if retired - known_ids:
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "snapshot retires an unknown binding",
            )
        if active_ids & retired:
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "binding cannot be active and retired in the same snapshot",
            )
        if known_ids - active_ids - retired:
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "snapshot omits existing bindings without explicit retirement",
            )
        for known in self.store.list_bindings(self.config.agent_instance_id):
            collision = next(
                (
                    binding
                    for binding, _spec, _home, _fp in prepared
                    if binding.binding_id != known.binding_id
                    and (
                        binding.thread_id == known.thread_id
                        or binding.expected_tmux_session == known.expected_tmux_session
                    )
                ),
                None,
            )
            if collision is not None:
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "snapshot reuses an existing topic or tmux identity",
                )
        if self.store.namespace_generation(agent_instance_id) == generation_id:
            try:
                for _binding, spec, _home, _fingerprint in prepared:
                    self.store.validate_effective_spec_update(spec)
            except ValueError as exc:
                raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

        snapshot = read_topic_config(self.config.topic_config_path)
        for binding, _spec, _home, _fingerprint in prepared:
            if binding.management_mode != ManagementMode.UNMANAGED:
                continue
            entry = snapshot.topic(binding.thread_id)
            if entry is None:
                raise RuntimeFailureError(
                    ErrorCode.CONFIG_DRIFT,
                    "unmanaged binding topic is absent",
                )
            if entry.engine == "codex":
                raise RuntimeFailureError(
                    ErrorCode.INVALID_REQUEST,
                    "codex topic cannot be imported as unmanaged",
                )
            if (
                entry.cwd is None
                or Path(entry.cwd).resolve() != Path(binding.canonical_cwd).resolve()
            ):
                raise RuntimeFailureError(
                    ErrorCode.CONFIG_DRIFT,
                    "unmanaged binding cwd differs from topic config",
                )
        if managed_topics:
            for _ in range(3):
                try:
                    snapshot = reconcile_managed_topics(
                        self.config.topic_config_path,
                        managed_topics=managed_topics,
                        expected_digest=snapshot.digest,
                        backup_dir=self.config.state_dir / "topic-config-backups",
                    )
                    break
                except KeyError as exc:
                    raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, str(exc)) from exc
                except RuntimeError:
                    snapshot = read_topic_config(self.config.topic_config_path)
            else:
                raise RuntimeFailureError(
                    ErrorCode.CONFIG_DRIFT,
                    "topic_config changed during all snapshot reconciliation attempts",
                )
        finalized = tuple(
            (
                RuntimeBinding(
                    binding_id=binding.binding_id,
                    agent_instance_id=binding.agent_instance_id,
                    chat_id=binding.chat_id,
                    thread_id=binding.thread_id,
                    project_id=binding.project_id,
                    canonical_cwd=binding.canonical_cwd,
                    expected_tmux_session=binding.expected_tmux_session,
                    configured=True,
                    config_digest=snapshot.digest,
                    management_mode=binding.management_mode,
                ),
                spec,
                codex_home,
                fingerprint,
            )
            for binding, spec, codex_home, fingerprint in prepared
        )
        try:
            self.store.apply_authoritative_snapshot(
                agent_instance_id=agent_instance_id,
                generation_id=generation_id,
                snapshot_digest=snapshot_digest,
                updates=finalized,
                retired_binding_ids=retired,
            )
        except ValueError as exc:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        return self._ok(
            request_id,
            agent_instance_id=agent_instance_id,
            generation_id=generation_id,
            snapshot_digest=snapshot_digest,
            applied_bindings=sorted(
                binding.binding_id
                for binding, _spec, _home, _fingerprint in finalized
                if binding.management_mode == ManagementMode.MANAGED
            ),
            unmanaged_bindings=sorted(
                binding.binding_id
                for binding, _spec, _home, _fingerprint in finalized
                if binding.management_mode == ManagementMode.UNMANAGED
            ),
            retired_bindings=sorted(retired),
            config_digest=snapshot.digest,
        )

    def _validate_snapshot_item(
        self,
        raw_binding: dict[str, Any],
        raw_desired: dict[str, Any],
        *,
        generation_id: str,
    ) -> tuple[RuntimeBinding, DesiredSpec, Path, str]:
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
        spec = _desired_spec(raw_desired)
        if (
            spec.binding_id != binding_id
            or spec.agent_instance_id != agent_instance_id
            or spec.project_id != project_id
            or Path(spec.canonical_cwd) != requested_cwd
        ):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "desired identity mismatch")
        if spec.generation_id != generation_id:
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "desired generation mismatch")
        if spec.provider != "codex":
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "V1 managed provider must be codex",
            )
        existing = self.store.get_binding(binding_id)
        if existing is not None and (
            existing.agent_instance_id != agent_instance_id
            or existing.chat_id != chat_id
            or existing.thread_id != thread_id
            or existing.project_id != project_id
        ):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "binding identity is immutable")
        if existing is not None and Path(existing.canonical_cwd) != requested_cwd:
            raise RuntimeFailureError(
                ErrorCode.PROJECT_RELOCATION_REQUIRED,
                "project path change requires explicit relocation",
            )
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
        adapter = TelegramUpstreamAdapter(
            self.config.upstream_sha,
            self.config.tmux_session_prefix,
        )
        return (
            RuntimeBinding(
                binding_id=binding_id,
                agent_instance_id=agent_instance_id,
                chat_id=chat_id,
                thread_id=thread_id,
                project_id=project_id,
                canonical_cwd=str(requested_cwd),
                expected_tmux_session=adapter.expected_session_name(
                    chat_id=chat_id,
                    thread_id=thread_id,
                ),
            ),
            spec,
            codex_home,
            profile_fingerprint(codex_home),
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
        registered = self.store.get_registered_instance(instance_id)
        if registered is None:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "instance disappeared")
        if (
            registered.binding_id != binding_id
            or registered.wrapper_pid != identity.pid
            or registered.wrapper_starttime != identity.starttime
            or registered.tmux_session != identity.tmux_session
            or registered.tmux_pane != identity.tmux_pane
        ):
            raise RuntimeFailureError(
                ErrorCode.CORRELATION_MISMATCH,
                "started child does not match registered provisional instance",
            )
        launch = registered.launch_spec
        raw_desired = launch.get("desired")
        if not isinstance(raw_desired, dict):
            raise RuntimeFailureError(ErrorCode.INVALID_REQUEST, "instance desired spec is invalid")
        spec = _desired_spec(raw_desired)
        codex_home = Path(_required_str(launch, "codex_home"))
        real_codex = Path(_required_str(launch, "real_codex"))
        self.child_verifier(
            child_pid=child_pid,
            child_starttime=child_starttime,
            wrapper_pid=identity.pid,
            codex_home=codex_home,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            real_codex=real_codex,
        )
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
            heartbeat_at=time.time(),
        )
        return self._ok(
            request_id,
            verified=True,
            applied=False,
            session_id=state.session_id,
            expected_spec_digest=spec.spec_digest,
        )

    def _heartbeat(
        self,
        request_id: str,
        payload: dict[str, Any],
        peer_pid: int,
        peer_uid: int,
    ) -> dict[str, Any]:
        if payload.get("agent_instance_id") != self.config.agent_instance_id:
            raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "AgentInstance namespace mismatch")
        connection_generation = _required_int(payload, "connection_generation")
        if connection_generation < 1:
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "connection_generation must be positive",
            )
        previous_boot_id = payload.get("previous_daemon_boot_id")
        if previous_boot_id is not None and (
            not isinstance(previous_boot_id, str) or not previous_boot_id
        ):
            raise RuntimeFailureError(
                ErrorCode.INVALID_REQUEST,
                "previous_daemon_boot_id must be null or non-empty string",
            )
        identity = _identity(payload)
        _verify_claimed_peer(identity, peer_pid=peer_pid, peer_uid=peer_uid)
        self.identity_verifier(identity)
        try:
            lifecycle, heartbeat_at = self.store.heartbeat_instance(
                instance_id=_required_str(payload, "instance_id"),
                binding_id=_required_str(payload, "binding_id"),
                wrapper_pid=identity.pid,
                wrapper_starttime=identity.starttime,
                child_pid=_required_int(payload, "child_pid"),
                child_starttime=_required_int(payload, "child_starttime"),
                tmux_session=identity.tmux_session,
                tmux_pane=identity.tmux_pane,
            )
        except KeyError as exc:
            raise RuntimeFailureError(
                ErrorCode.CORRELATION_MISMATCH,
                "heartbeat does not match a registered live instance",
            ) from exc
        return self._ok(
            request_id,
            heartbeat_at=heartbeat_at,
            lifecycle=lifecycle,
            connection_generation=connection_generation,
            resynchronized=(previous_boot_id is not None and previous_boot_id != self.boot_id),
        )

    def _child_exited(
        self,
        request_id: str,
        payload: dict[str, Any],
        peer_pid: int,
        peer_uid: int,
    ) -> dict[str, Any]:
        identity = _identity(payload)
        _verify_claimed_peer(identity, peer_pid=peer_pid, peer_uid=peer_uid)
        self.identity_verifier(identity)
        instance_id = _required_str(payload, "instance_id")
        binding_id = _required_str(payload, "binding_id")
        child_pid = _required_int(payload, "child_pid")
        child_starttime = _required_int(payload, "child_starttime")
        exit_code = _required_int(payload, "exit_code")
        registered = self.store.get_registered_instance(instance_id)
        if registered is None or (
            registered.binding_id != binding_id
            or registered.wrapper_pid != identity.pid
            or registered.wrapper_starttime != identity.starttime
            or registered.tmux_session != identity.tmux_session
            or registered.tmux_pane != identity.tmux_pane
        ):
            raise RuntimeFailureError(
                ErrorCode.CORRELATION_MISMATCH,
                "exited child does not match registered wrapper instance",
            )
        self.store.mark_instance_exited(
            instance_id=instance_id,
            binding_id=binding_id,
            wrapper_pid=identity.pid,
            wrapper_starttime=identity.starttime,
            child_pid=child_pid,
            child_starttime=child_starttime,
        )
        return self._ok(request_id, instance_id=instance_id, exit_code=exit_code)

    async def _reconcile_topic_config_loop(self) -> None:
        while True:
            with contextlib.suppress(OSError, ValueError):
                self._reconcile_topic_config_once()
            await asyncio.sleep(1.0)

    def _reconcile_topic_config_once(self) -> None:
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
            if mode == ManagementMode.UNSUPPORTED_CODEX_SUBPROCESS:
                self.store.enqueue_user_notification(
                    code=UserNotificationCode.UNSUPPORTED_CODEX_SUBPROCESS,
                    binding=binding,
                    event_ref=snapshot.digest,
                )

    async def _collect_applied_evidence_loop(self) -> None:
        while True:
            try:
                states = read_tmux_states(self.config.tmux_state_path)
                for instance in self.store.pending_evidence_instances():
                    binding = self.store.get_binding(instance.binding_id)
                    if binding is None:
                        continue
                    matches = tuple(
                        state
                        for state in states
                        if state.chat_id == binding.chat_id
                        and state.thread_id == binding.thread_id
                        and state.session_name == instance.tmux_session
                        and state.provider == "codex"
                        and Path(state.cwd).resolve() == Path(binding.canonical_cwd).resolve()
                    )
                    if len(matches) != 1:
                        continue
                    state = matches[0]
                    if not state.session_id or not state.transcript_path:
                        continue
                    launch = instance.launch_spec
                    raw_desired = launch.get("desired")
                    if not isinstance(raw_desired, dict):
                        continue
                    spec = _desired_spec(raw_desired)
                    codex_home = Path(_required_str(launch, "codex_home"))
                    expected_fingerprint = _required_str(launch, "profile_fingerprint")
                    if profile_fingerprint(codex_home) != expected_fingerprint:
                        continue
                    real_codex = Path(_required_str(launch, "real_codex"))
                    expected_binary_digest = _required_str(launch, "real_codex_sha256")
                    if file_sha256(real_codex) != expected_binary_digest:
                        continue
                    rollout_path = Path(state.transcript_path).resolve(strict=True)
                    rollout_stat = rollout_path.stat()
                    if not stat.S_ISREG(rollout_stat.st_mode):
                        continue
                    if rollout_stat.st_uid != os.getuid():
                        continue
                    process_evidence = self.child_verifier(
                        child_pid=instance.child_pid,
                        child_starttime=instance.child_starttime,
                        wrapper_pid=instance.wrapper_pid,
                        codex_home=codex_home,
                        model=spec.model,
                        reasoning_effort=spec.reasoning_effort,
                        real_codex=real_codex,
                    )
                    evidence: dict[str, object] = {
                        **process_evidence,
                        "spec_digest": spec.spec_digest,
                        "profile_fingerprint": expected_fingerprint,
                        "real_codex_version": _required_str(launch, "real_codex_version"),
                        "real_codex_sha256": expected_binary_digest,
                        "session_id": state.session_id,
                        "rollout_path": str(rollout_path),
                        "rollout_device": rollout_stat.st_dev,
                        "rollout_inode": rollout_stat.st_ino,
                        "wrapper_pid": instance.wrapper_pid,
                        "wrapper_starttime": instance.wrapper_starttime,
                        "tmux_session": instance.tmux_session,
                        "tmux_pane": instance.tmux_pane,
                    }
                    self.store.record_applied_spec(
                        instance_id=instance.instance_id,
                        binding_id=instance.binding_id,
                        spec=spec,
                        evidence=evidence,
                        session_id=state.session_id,
                        account_profile=spec.account_profile,
                        rollout_path=str(rollout_path),
                        rollout_device=rollout_stat.st_dev,
                        rollout_inode=rollout_stat.st_ino,
                    )
            except (OSError, ValueError, RuntimeFailureError):
                pass
            await asyncio.sleep(self.evidence_interval)

    async def _observe_codex_rollouts_loop(self) -> None:
        while True:
            for target in self.store.rollout_observation_targets():
                with contextlib.suppress(OSError, ValueError, RolloutContractError):
                    self._observe_codex_rollout(target)
            await asyncio.sleep(self.evidence_interval)

    async def _retire_pending_loop(self) -> None:
        while True:
            await self._retire_pending_once()
            await asyncio.sleep(self.evidence_interval)

    async def _retire_pending_once(self) -> None:
        for candidate in self.store.retirement_candidates():
            binding = self.store.get_binding(candidate.binding_id)
            if binding is None:
                continue
            try:
                snapshot = read_topic_config(self.config.topic_config_path)
                entry = snapshot.topic(binding.thread_id)
                config_is_current = (
                    entry is not None
                    and entry.engine == "codex"
                    and entry.exec_mode == "tmux"
                    and entry.cwd is not None
                    and Path(entry.cwd).resolve() == Path(binding.canonical_cwd).resolve()
                    and binding.management_mode == ManagementMode.MANAGED
                    and binding.config_digest == snapshot.digest
                )
            except (OSError, ValueError):
                config_is_current = False
            if not config_is_current:
                continue
            identity = WrapperIdentity(
                pid=candidate.wrapper_pid,
                starttime=candidate.wrapper_starttime,
                uid=os.getuid(),
                tmux_session=candidate.tmux_session,
                tmux_pane=candidate.tmux_pane,
                cwd=candidate.canonical_cwd,
            )
            try:
                await asyncio.to_thread(self.retirement_executor, identity)
                self.store.mark_instance_retired(candidate)
            except (OSError, ValueError, RuntimeFailureError, subprocess.SubprocessError):
                continue

    def _observe_codex_rollout(self, target: RolloutObservationTarget) -> None:
        cursor = self.store.rollout_observation_cursor(
            instance_id=target.instance_id,
            rollout_device=target.rollout_device,
            rollout_inode=target.rollout_inode,
        )
        scanned = scan_rollout(
            Path(target.rollout_path),
            codex_version=target.codex_version,
            start_offset=cursor.offset,
            expected_device=target.rollout_device,
            expected_inode=target.rollout_inode,
        )
        lifecycle = target.lifecycle
        active_turn_id = cursor.active_turn_id
        pending_model = cursor.pending_model
        pending_effort = cursor.pending_reasoning_effort
        pending_evidence = cursor.pending_evidence_digest
        pending_turn_id = cursor.pending_turn_id
        for signal in scanned.signals:
            if signal.kind == RolloutSignalKind.TURN_STARTED and signal.turn_id is not None:
                lifecycle = LifecycleState.BUSY
                active_turn_id = signal.turn_id
            elif (
                signal.kind == RolloutSignalKind.TURN_CONTEXT
                and lifecycle == LifecycleState.BUSY
                and signal.turn_id == active_turn_id
            ):
                pending_model = signal.model
                pending_effort = signal.reasoning_effort
                pending_evidence = signal.evidence_digest
                pending_turn_id = signal.turn_id
            elif (
                signal.kind == RolloutSignalKind.TURN_COMPLETED
                and signal.turn_id is not None
                and signal.turn_id == active_turn_id
            ):
                lifecycle = LifecycleState.IDLE
                active_turn_id = None

        sequence = cursor.observation_sequence
        last_model = cursor.last_model
        last_effort = cursor.last_reasoning_effort
        if (
            scanned.at_eof
            and pending_model is not None
            and pending_effort is not None
            and pending_evidence is not None
            and pending_turn_id is not None
        ):
            effective = self.store.get_effective_spec(target.binding_id)
            if effective is None:
                raise ValueError("observed binding has no local effective spec")
            spec, _codex_home, _profile_fingerprint = effective
            observed = (pending_model, pending_effort)
            if observed != (last_model, last_effort) and observed != (
                spec.model,
                spec.reasoning_effort,
            ):
                sequence += 1
                evidence_digest = sha256_digest(
                    {
                        "instance_id": target.instance_id,
                        "rollout_device": target.rollout_device,
                        "rollout_inode": target.rollout_inode,
                        "session_id": target.session_id,
                        "turn_id": pending_turn_id,
                        "provider_evidence_digest": pending_evidence,
                    }
                )
                self.store.enqueue_observed_proposal(
                    ObservedSpecProposal(
                        binding_id=target.binding_id,
                        base_generation_id=spec.generation_id,
                        base_revision=spec.revision,
                        base_spec_digest=spec.spec_digest,
                        model=pending_model,
                        reasoning_effort=pending_effort,
                        instance_id=target.instance_id,
                        observation_sequence=sequence,
                        evidence_digest=evidence_digest,
                    )
                )
            last_model = pending_model
            last_effort = pending_effort
            pending_model = None
            pending_effort = None
            pending_evidence = None
            pending_turn_id = None
        self.store.save_rollout_observation_cursor(
            instance_id=target.instance_id,
            rollout_device=target.rollout_device,
            rollout_inode=target.rollout_inode,
            cursor=RolloutObservationCursor(
                offset=scanned.next_offset,
                observation_sequence=sequence,
                last_model=last_model,
                last_reasoning_effort=last_effort,
                active_turn_id=active_turn_id,
                pending_model=pending_model,
                pending_reasoning_effort=pending_effort,
                pending_evidence_digest=pending_evidence,
                pending_turn_id=pending_turn_id,
            ),
            lifecycle=str(lifecycle),
        )

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
