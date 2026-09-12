"""Exercise account/key UI using a synthetic session, without OAuth credentials."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from open_train.app import create_app

with tempfile.TemporaryDirectory(prefix="open-train-account-ui-") as directory:
    app = create_app(directory)
    account = app.state.accounts.login_identity(
        "github", "ui-fixture", "ui@example.com", "UI Test User"
    )
    session = app.state.accounts.issue(account["id"], "session", "Browser test", 1)[
        "key"
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPEN_TRAIN_")}
    env.update(OPEN_TRAIN_AUTH_MODE="accounts", OPEN_TRAIN_PUBLIC_URL=base)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_train.cli",
            "serve",
            "--data-dir",
            directory,
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/healthz", timeout=0.2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.context.add_cookies(
                [
                    {
                        "name": "open_train_session",
                        "value": session,
                        "url": base,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page.goto(base)
            page.get_by_role("button", name="Account & access").click()
            expect(page.get_by_role("heading", name="Account & access")).to_be_visible()
            page.get_by_role("textbox", name="Key name", exact=True).fill(
                "browser-training"
            )
            page.get_by_role("button", name="Create key").click()
            expect(page.locator("dialog[open] pre")).to_contain_text("Copy now")
            page.get_by_role("button", name="Revoke", exact=True).click()
            expect(
                page.get_by_role("button", name="Revoked", exact=True)
            ).to_be_disabled()
            # The screenshot must not contain the ephemeral credential.
            page.locator("dialog[open] pre").evaluate(
                "e => e.textContent = 'Key revoked successfully.'"
            )
            Path("test-results").mkdir(exist_ok=True)
            page.screenshot(path="test-results/account-access.png")
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#auth")).to_be_visible()
            assert not errors, errors
            browser.close()
        print("Account/key browser checks passed (synthetic session; no live OAuth).")
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
