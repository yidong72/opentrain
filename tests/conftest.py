import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="session")
def server(tmp_path_factory, request):
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path_factory.mktemp("server")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("WANDB_", "OPEN_TRAIN_"))
    }
    env.update(
        PYTHONPATH=str(root),
        WANDB_BASE_URL=base,
        WANDB_API_KEY="local" + "0" * 35,
        WANDB_ENTITY="local",
        WANDB_DIR=str(directory),
        WANDB_SILENT="true",
        WANDB_CACHE_DIR=str(directory / "cache"),
    )
    if getattr(request, "param", None) == "accounts":
        from open_train.app import create_app

        bootstrap = create_app(directory / "data")
        account = bootstrap.state.accounts.login_identity(
            "github", "sdk-user", "sdk@example.com", "SDK User"
        )
        key = bootstrap.state.accounts.issue(
            account["id"], "api_key", "SDK training", 1
        )["key"]
        env.update(
            OPEN_TRAIN_AUTH_MODE="accounts",
            WANDB_ENTITY=account["username"],
            WANDB_API_KEY=key,
        )
    logfile = directory / "server.log"
    with logfile.open("w+") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "open_train.cli",
                "serve",
                "--port",
                str(port),
                "--data-dir",
                str(directory / "data"),
            ],
            cwd=root,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(150):
                if process.poll() is not None:
                    pytest.fail(logfile.read_text())
                try:
                    if httpx.get(base + "/healthz", timeout=0.2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Server did not become ready")
            yield {
                "url": base,
                "env": env,
                "root": root,
                "directory": directory,
                "log": logfile,
            }
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def sdk(server, tmp_path):
    def execute(code, timeout=100):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=server["env"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        assert result.returncode == 0, (
            result.stdout
            + result.stderr
            + "\nSERVER:\n"
            + server["log"].read_text()[-15000:]
        )
        return result

    return execute
