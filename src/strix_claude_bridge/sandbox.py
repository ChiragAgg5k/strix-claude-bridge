"""Disposable, tightly configured Docker command executor."""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}$")
_CLEANUP_ATTEMPTS = 3
_CLEANUP_RETRY_DELAY_S = 0.05


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False


class DockerSandboxExecutor:
    """Own one disposable container and remove it on close or cancellation.

    Images are caller-selected host configuration, never model-controlled input. Commands run via
    an explicit ``/bin/sh -lc`` invocation inside a network-disabled, capability-free container.
    No host paths, environment, credentials, or Docker socket are mounted into the container.
    """

    def __init__(
        self,
        image: str,
        *,
        timeout_s: float = 30.0,
        max_output_bytes: int = 64 * 1024,
    ) -> None:
        if not _IMAGE_RE.fullmatch(image):
            raise ValueError("image must be a non-empty Docker image reference without whitespace")
        if not 0 < timeout_s <= 300:
            raise ValueError("timeout_s must be in (0, 300]")
        if not 1 <= max_output_bytes <= 1024 * 1024:
            raise ValueError("max_output_bytes must be in [1, 1048576]")
        self.image = image
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self._client: Any = None
        self._container: Any = None
        self._close_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._closed = False

    async def __aenter__(self) -> DockerSandboxExecutor:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("sandbox executor is closed")
        if self._container is not None:
            return
        try:
            await asyncio.to_thread(self._start_sync)
        except BaseException:
            await self.close()
            raise

    @staticmethod
    def _remove_sync(container: Any) -> None:
        last_error: Exception | None = None
        for attempt in range(_CLEANUP_ATTEMPTS):
            try:
                container.remove(force=True, v=True)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < _CLEANUP_ATTEMPTS:
                    time.sleep(_CLEANUP_RETRY_DELAY_S)
        assert last_error is not None
        raise last_error

    def _start_sync(self) -> None:
        import docker

        client = docker.from_env()
        try:
            container = client.containers.run(
                self.image,
                entrypoint=["/bin/sh", "-c"],
                command=["while :; do sleep 3600; done"],
                detach=True,
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
                mem_limit="256m",
                nano_cpus=1_000_000_000,
                pids_limit=128,
                labels={"org.strix-claude-bridge.disposable": "true"},
                environment={},
                stdin_open=False,
                tty=False,
            )
        except BaseException:
            client.close()
            raise

        with self._state_lock:
            self._client = client
            self._container = container
            clean_up_immediately = self._closed
        if clean_up_immediately:
            try:
                self._remove_sync(container)
            finally:
                client.close()
            with self._state_lock:
                if self._container is container:
                    self._container = None
                    self._client = None

    async def exec(self, command: str) -> ExecResult:
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ValueError("command must be a non-empty string without NUL bytes")
        if len(command.encode()) > 16 * 1024:
            raise ValueError("command exceeds 16384 bytes")
        await self.start()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._exec_sync, command), timeout=self.timeout_s
            )
        except TimeoutError:
            await self.close()
            raise TimeoutError(f"sandbox command exceeded {self.timeout_s:g}s") from None
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise

    @staticmethod
    def _append_bounded(target: bytearray, chunk: bytes, remaining: int) -> tuple[int, bool]:
        retained = chunk[:remaining]
        target.extend(retained)
        return remaining - len(retained), len(chunk) > len(retained)

    def _exec_sync(self, command: str) -> ExecResult:
        container = self._container
        if container is None:
            raise RuntimeError("sandbox container is unavailable")

        api = container.client.api
        created = api.exec_create(
            container.id,
            ["/bin/sh", "-lc", command],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            privileged=False,
            environment={},
        )
        exec_id = created["Id"]
        chunks = api.exec_start(exec_id, stream=True, demux=True)
        stdout = bytearray()
        stderr = bytearray()
        remaining = self.max_output_bytes
        truncated = False
        for stdout_chunk, stderr_chunk in chunks:
            for target, chunk in ((stdout, stdout_chunk), (stderr, stderr_chunk)):
                if not chunk:
                    continue
                remaining, overflow = self._append_bounded(target, chunk, remaining)
                if overflow:
                    truncated = True
                    break
            if truncated:
                break

        if truncated:
            # Stop an unbounded producer immediately rather than merely discarding its output.
            self._terminate_after_output_limit(container)
            exit_code = 137
        else:
            exit_code = int(api.exec_inspect(exec_id)["ExitCode"])
        return ExecResult(
            exit_code,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            truncated,
        )

    def _terminate_after_output_limit(self, container: Any) -> None:
        self._remove_sync(container)
        with self._state_lock:
            client = self._client
            if self._container is container:
                self._container = None
                self._client = None
                self._closed = True
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    async def close(self) -> None:
        async with self._close_lock:
            with self._state_lock:
                self._closed = True
                container, client = self._container, self._client
            if container is None and client is None:
                return

            if container is not None:
                try:
                    await asyncio.to_thread(self._remove_sync, container)
                except Exception as exc:
                    # Keep both handles so a later close() can retry transient daemon failures.
                    identifier = getattr(container, "short_id", None) or getattr(
                        container, "id", "unknown"
                    )
                    raise RuntimeError(
                        f"failed to remove disposable sandbox container {identifier}"
                    ) from exc

            with self._state_lock:
                if self._container is container:
                    self._container = None
                    self._client = None
            if client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(client.close)
