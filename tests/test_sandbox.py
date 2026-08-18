from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from strix_claude_bridge.sandbox import DockerSandboxExecutor


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.api: Any = self

    def close(self) -> None:
        self.closed = True


class BlockingContainer:
    id = "container-id"
    short_id = "container"

    def __init__(self) -> None:
        self.released = threading.Event()
        self.removed = False
        self.remove_volumes = False
        self.client = FakeClient()
        self.client.api = self

    def exec_create(self, *_args: object, **_kwargs: object) -> dict[str, str]:
        return {"Id": "exec-id"}

    def exec_start(self, *_args: object, **_kwargs: object) -> Iterator[tuple[bytes, bytes]]:
        self.released.wait(timeout=2)
        yield b"", b"removed"

    def exec_inspect(self, _exec_id: str) -> dict[str, int]:
        return {"ExitCode": 137}

    def remove(self, *, force: bool, v: bool) -> None:
        assert force is True
        self.removed = True
        self.remove_volumes = v
        self.released.set()


class StreamingContainer(BlockingContainer):
    def __init__(self, chunks: list[tuple[bytes, bytes]]) -> None:
        super().__init__()
        self.chunks = chunks
        self.yielded = 0

    def exec_start(self, *_args: object, **_kwargs: object) -> Iterator[tuple[bytes, bytes]]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def exec_inspect(self, _exec_id: str) -> dict[str, int]:
        return {"ExitCode": 0}


class StartClient(FakeClient):
    def __init__(self, container: BlockingContainer) -> None:
        super().__init__()
        self.container = container
        self.containers = self

    def run(self, *_args: object, **_kwargs: object) -> BlockingContainer:
        return self.container


class FlakyRemovalContainer(BlockingContainer):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.remove_calls = 0

    def remove(self, *, force: bool, v: bool) -> None:
        assert force is True
        assert v is True
        self.remove_calls += 1
        if self.remove_calls <= self.failures:
            raise RuntimeError("transient daemon error")
        self.removed = True


def attach(executor: DockerSandboxExecutor, container: BlockingContainer) -> FakeClient:
    client = container.client
    executor._container = container
    executor._client = client
    return client


@pytest.mark.asyncio
async def test_programmatic_task_cancellation_removes_container_and_volumes() -> None:
    executor = DockerSandboxExecutor("alpine:3.21", timeout_s=10)
    container = BlockingContainer()
    client = attach(executor, container)

    task = asyncio.create_task(executor.exec("sleep 100"))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert container.removed
    assert container.remove_volumes
    assert client.closed


@pytest.mark.asyncio
async def test_timeout_removes_container_and_volumes() -> None:
    executor = DockerSandboxExecutor("alpine:3.21", timeout_s=0.01)
    container = BlockingContainer()
    client = attach(executor, container)

    with pytest.raises(TimeoutError, match="exceeded"):
        await executor.exec("sleep 100")

    assert container.removed
    assert container.remove_volumes
    assert client.closed


def test_streaming_output_stops_at_limit_and_removes_container() -> None:
    executor = DockerSandboxExecutor("alpine:3.21", max_output_bytes=10)
    container = StreamingContainer([(b"12345678", b""), (b"abcdefgh", b""), (b"unused", b"")])
    client = attach(executor, container)

    result = executor._exec_sync("produce output")

    assert result.stdout == "12345678ab"
    assert result.truncated is True
    assert result.exit_code == 137
    assert container.yielded == 2
    assert container.removed
    assert container.remove_volumes
    assert client.closed


def test_late_container_creation_after_close_is_cleaned_up(monkeypatch) -> None:
    import docker

    executor = DockerSandboxExecutor("alpine:3.21")
    executor._closed = True
    container = BlockingContainer()
    client = StartClient(container)
    monkeypatch.setattr(docker, "from_env", lambda: client)

    executor._start_sync()

    assert container.removed
    assert container.remove_volumes
    assert client.closed
    assert executor._container is None
    assert executor._client is None


@pytest.mark.asyncio
async def test_failed_cleanup_retains_handle_for_later_retry() -> None:
    executor = DockerSandboxExecutor("alpine:3.21")
    container = FlakyRemovalContainer(failures=3)
    client = attach(executor, container)

    with pytest.raises(RuntimeError, match="container container"):
        await executor.close()
    assert executor._container is container
    assert not client.closed

    await executor.close()
    assert container.removed
    assert container.remove_calls == 4
    assert executor._container is None
    assert client.closed


def test_image_and_command_controls_reject_unsafe_values() -> None:
    with pytest.raises(ValueError, match="image"):
        DockerSandboxExecutor("alpine:latest --privileged")

    executor = DockerSandboxExecutor("alpine:3.21")
    with pytest.raises(ValueError, match="command"):
        asyncio.run(executor.exec("contains\x00nul"))
