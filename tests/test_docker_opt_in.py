from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest

from strix_claude_bridge.sandbox import DockerSandboxExecutor

pytestmark = pytest.mark.docker


@pytest.mark.asyncio
async def test_real_disposable_container_exec_and_cleanup() -> None:
    if os.environ.get("STRIX_BRIDGE_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set STRIX_BRIDGE_RUN_DOCKER_TESTS=1 to opt in")

    import docker

    client = docker.from_env()
    before = {
        container.id
        for container in client.containers.list(
            all=True, filters={"label": "org.strix-claude-bridge.disposable=true"}
        )
    }
    executor = DockerSandboxExecutor(
        os.environ.get("STRIX_BRIDGE_TEST_IMAGE", "alpine:3.21"), timeout_s=10
    )
    async with executor:
        result = await executor.exec('printf "docker-test-ok\\n"')
    after = {
        container.id
        for container in client.containers.list(
            all=True, filters={"label": "org.strix-claude-bridge.disposable=true"}
        )
    }
    client.close()

    assert result.exit_code == 0
    assert result.stdout == "docker-test-ok\n"
    assert result.stderr == ""
    assert after == before


@pytest.mark.asyncio
async def test_image_declared_anonymous_volume_is_removed() -> None:
    if os.environ.get("STRIX_BRIDGE_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set STRIX_BRIDGE_RUN_DOCKER_TESTS=1 to opt in")

    import docker

    client = docker.from_env()
    tag = f"strix-bridge-volume-test:{uuid.uuid4().hex}"
    volume_name: str | None = None
    try:
        with tempfile.TemporaryDirectory() as build_dir:
            await asyncio.to_thread(
                Path(build_dir, "Dockerfile").write_text,
                "FROM alpine:3.21\nVOLUME /data\n",
            )
            client.images.build(path=build_dir, tag=tag, rm=True, forcerm=True)

        executor = DockerSandboxExecutor(tag, timeout_s=10)
        async with executor:
            assert executor._container is not None
            executor._container.reload()
            mount = next(
                item
                for item in executor._container.attrs["Mounts"]
                if item["Destination"] == "/data"
            )
            volume_name = mount["Name"]
            assert client.volumes.get(volume_name) is not None

        assert volume_name is not None
        with pytest.raises(docker.errors.NotFound):
            client.volumes.get(volume_name)
    finally:
        if volume_name is not None:
            try:
                client.volumes.get(volume_name).remove(force=True)
            except docker.errors.NotFound:
                pass
        try:
            client.images.remove(tag, force=True)
        except docker.errors.ImageNotFound:
            pass
        client.close()
