"""Opt-in real-browser coverage; CI runs this in its Chromium job."""

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from open_train.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("OPEN_TRAIN_BROWSER_TESTS") != "1",
    reason="Set OPEN_TRAIN_BROWSER_TESTS=1 with Playwright Chromium installed",
)


def test_sdk_session_cards_markers_and_declared_axes(server):
    from tests.test_sessions import send, writer

    app = create_app(server["directory"] / "data")
    store = app.state.store
    uid = writer(store, name="browser-sessions")
    send(store, uid, [0, 1], start=101)
    writer(store, name="browser-sessions", wid="two", start=200)
    send(store, uid, [0, 1], start=201, offset=2)
    store.upsert(
        {
            "name": "browser-sessions",
            "modelName": "sessions",
            "entityName": "local",
            "config": {
                "_wandb": {"value": {"m": [{"1": "train/*", "4": "train/global_step"}]}}
            },
        }
    )
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server["url"])
        page.get_by_label("Project", exact=True).select_option("local/sessions")
        page.get_by_label("Compare browser-sessions", exact=True).check()
        loss = page.locator('.chart[data-metric="train/loss"]')
        expect(loss.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("train/loss by train/global_step")
        )
        page.locator("#show-sessions").check()
        expect(loss.locator(".session-start")).to_have_count(1)
        expect(loss.locator(".session-start title")).to_contain_text("S2")
        page.locator(".session-badge").click()
        expect(page.locator(".session-card")).to_have_count(2)
        expect(page.locator(".session-card").first).to_contain_text("worker-one")
        expect(page.locator(".session-card").nth(1)).to_contain_text(
            "train/global_step: 2–3"
        )
        assert not errors
        browser.close()


def test_train_eval_auto_axes_load_without_full_run_detail(server):
    store = create_app(server["directory"] / "data").state.store
    uid = store.upsert({"name": "train-eval", "modelName": "fast-plots"})[0]["uid"]
    rows = [
        {
            "_step": 2545,
            "_timestamp": 100,
            "eval/global_step": 1180,
            "eval/score": 0.35,
        },
        {
            "_step": 2595,
            "_timestamp": 101,
            "train/global_step": 1195,
            "train/loss": 0.5,
        },
    ]
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [json.dumps(r) for r in rows],
                }
            }
        },
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        full_requests, errors, plot_requests = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: (
                plot_requests.append(r.url)
                if r.url.endswith(("/plots", "/api/series"))
                else None
            ),
        )

        # Loading plots must succeed even if the expensive detail is unavailable.
        def block_detail(route):
            full_requests.append(route.request.url)
            route.abort()

        page.route(server["url"] + f"/api/runs/{uid}", block_detail)
        page.goto(server["url"])
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        page.get_by_label("Project", exact=True).select_option("local/fast-plots")
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        page.reload()
        expect(page.get_by_label("Project", exact=True)).to_have_value(
            "local/fast-plots"
        )
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        page.evaluate("() => refresh()")
        assert not plot_requests
        assert page.evaluate("""() => {
            const element = document.createElement('span');
            return [
                {'_step': 10, 'train/global_step': 0, 'global_step': 9},
                {'_step': 10, 'global_step': 7},
                {'_step': 10, 'train/global_step': null},
            ].map(summary => { updateRunStep(element, {summary}); return element.textContent; });
        }""") == ["Train step 0", "Step 7", "Log step 10"]
        page.get_by_label("Compare train-eval", exact=True).check()
        expect(page.locator(f'[data-uid="{uid}"] .run-step')).to_have_text(
            "Train step 1,195"
        )
        train = page.locator('.chart[data-metric="train/loss"]')
        expect(train.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("train/loss by train/global_step")
        )
        page.locator("#metric-search").fill("eval/score")
        ev = page.locator('.chart[data-metric="eval/score"]')
        expect(ev.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("eval/score by eval/global_step")
        )
        expect(ev.locator("small").first).to_contain_text("Auto: eval/global_step")
        assert page.evaluate(
            "document.querySelector('.chart[data-metric=\"eval/score\"]').liveSeries[0].points"
        ) == [[1180, 0.35]]
        ev.locator(".plot-axis").select_option("_step")
        expect(ev.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("eval/score by _step")
        )
        assert page.evaluate(
            "document.querySelector('.chart[data-metric=\"eval/score\"]').liveSeries[0].points"
        ) == [[2545, 0.35]]
        assert not full_requests and not errors
        # Live sidebar refresh uses the same training counter without remounting.
        page.evaluate("window.stepElement = document.querySelector('.run-step')")
        store.stream(
            uid,
            {
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 2,
                        "content": [
                            json.dumps(
                                {
                                    "_step": 2597,
                                    "train/global_step": 1196,
                                    "train/loss": 0.4,
                                }
                            )
                        ],
                    }
                }
            },
        )
        page.evaluate("() => refresh()")
        expect(page.locator(f'[data-uid="{uid}"] .run-step')).to_have_text(
            "Train step 1,196"
        )
        assert page.evaluate("stepElement === document.querySelector('.run-step')")
        page.unroute(server["url"] + f"/api/runs/{uid}", block_detail)
        page.goto(server["url"] + "/local/fast-plots/runs/train-eval")
        expect(page.locator("#selection-count")).to_have_text("1 runs selected")
        browser.close()


@pytest.mark.parametrize("server", ["accounts"], indirect=True)
def test_projects_plot_axes_sharing_and_png(server, tmp_path):
    app = create_app(server["directory"] / "data")
    entity = server["env"]["WANDB_ENTITY"]
    uids = []
    for index, project in enumerate(["project-alpha", "project-alpha", "project-beta"]):
        uid = app.state.store.upsert(
            {"entityName": entity, "modelName": project, "name": f"ui-{index}"}
        )[0]["uid"]
        uids.append(uid)
        app.state.store.stream(
            uid,
            {
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 0,
                        "content": [
                            json.dumps(
                                {
                                    "_step": s,
                                    "_timestamp": 100 + s,
                                    "train/global_step": s * 10,
                                    "train/loss": 1 / (s + 1) + index,
                                    "train/reward": s / 10,
                                }
                            )
                            for s in range(20)
                        ],
                    }
                }
            },
        )
    outsider = app.state.accounts.login_identity(
        "github", "ui-outsider", "outsider@example.com", "Outsider"
    )
    outsider_key = app.state.accounts.issue(outsider["id"], "api_key", "UI test", 1)[
        "key"
    ]
    reader = app.state.accounts.login_identity(
        "github", "ui-reader", "reader@example.com", "Reader"
    )
    reader_key = app.state.accounts.issue(reader["id"], "api_key", "UI test", 1)["key"]
    with app.state.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO memberships VALUES (?,?,'reader')", (entity, reader["id"])
        )
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()

        def new_page(key):
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            page.add_init_script(
                f"sessionStorage.setItem('open-train-key', {json.dumps(key)})"
            )
            page.on("pageerror", lambda e: errors.append(str(e)))
            return page

        page = new_page(server["env"]["WANDB_API_KEY"])
        page.goto(server["url"])
        selector = page.get_by_label("Project", exact=True)
        expect(selector).to_be_visible()
        selector.select_option(f"{entity}/project-alpha")
        expect(page.locator("#runs-body .run-row")).to_have_count(2)
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        selector.select_option(f"{entity}/project-beta")
        expect(page.locator("#runs-body .run-row")).to_have_count(1)
        assert page.evaluate("[...state.selected]") == []
        page.reload()
        expect(selector).to_have_value(f"{entity}/project-beta")
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        selector.select_option(f"{entity}/project-alpha")
        page.get_by_label("Compare ui-0", exact=True).check()
        page.get_by_label("Compare ui-1", exact=True).check()
        loss = page.locator('.chart[data-metric="train/loss"]')
        reward = page.locator('.chart[data-metric="train/reward"]')
        expect(loss.locator("svg")).to_be_visible()
        loss.get_by_label("X axis for train/loss", exact=True).select_option(
            "train/global_step"
        )
        expect(loss.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("train/loss by train/global_step")
        )
        expect(reward.locator(".plot-axis")).to_have_value("")
        expect(reward.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("train/reward by train/global_step")
        )
        loss.locator(".plot-smoothing").fill("0.6")
        loss.locator(".plot-zoom-in").click()
        zoom = loss.get_attribute("data-domain")
        page.locator("#share-view").click()
        link = page.get_by_label("Share link", exact=True).input_value()
        assert server["env"]["WANDB_API_KEY"] not in link
        shared = json.loads(unquote(urlsplit(link).fragment[5:]))
        assert set(shared["runs"]) == set(uids[:2])
        assert shared["colors"] == page.evaluate(
            "[...state.selected].map(id => [id, color(id)])"
        )
        page.get_by_role("button", name="Close", exact=True).click()
        teammate = new_page(reader_key)
        teammate.goto(link)
        teammate_loss = teammate.locator('.chart[data-metric="train/loss"]')
        expect(teammate_loss.locator("svg")).to_be_visible()
        expect(teammate_loss.locator(".plot-axis")).to_have_value("train/global_step")
        expect(teammate_loss.locator(".plot-smoothing")).to_have_value("0.6")
        expect(teammate_loss).to_have_attribute("data-domain", zoom)
        assert (
            teammate.evaluate("[...state.selected].map(id => [id, color(id)])")
            == shared["colors"]
        )
        expect(teammate.get_by_label("Project", exact=True)).to_have_value(
            f"{entity}/project-alpha"
        )
        # A receiving browser's old local preferences cannot alter shared state.
        teammate.evaluate(
            "localStorage.setItem('open-train-axis:train/reward','_runtime')"
        )
        teammate.reload()
        expect(
            teammate.locator('.chart[data-metric="train/reward"] svg')
        ).to_have_attribute(
            "aria-label", pytest_regex("train/reward by train/global_step")
        )
        # Per-plot axes also work inside the maximized view.
        loss.locator(".plot-maximize").click()
        expanded = page.locator(".plot-dialog")
        expanded.locator(".plot-axis").select_option("_timestamp")
        expect(expanded.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("train/loss by _timestamp")
        )
        expanded.locator(".plot-axis").select_option("train/global_step")
        expect(expanded.locator(".chart")).to_have_attribute("data-domain", zoom)
        with page.expect_download() as download:
            expanded.locator(".plot-export").click()
        png = tmp_path / "shared-plot.png"
        download.value.save_as(png)
        assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        from PIL import Image

        with Image.open(png) as image:
            assert image.width == 1280 and image.height > 500
        # Save visual acceptance artifacts in the normal ignored output directory.
        Path("test-results").mkdir(exist_ok=True)
        with Image.open(png) as image:
            image.save("test-results/shared-plot.png")
        expanded.locator(".plot-share").click()
        plot_link = page.get_by_label("Share link", exact=True).input_value()
        teammate.goto(plot_link)
        expect(teammate.locator(".chart svg")).to_have_count(1)
        expect(teammate_loss.locator("svg")).to_be_visible()
        teammate.screenshot(path="test-results/project-sharing.png", full_page=True)
        teammate.set_viewport_size({"width": 390, "height": 844})
        assert teammate.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # No data is exposed by knowing the link; endpoint access still applies.
        denied = new_page(outsider_key)
        denied.goto(link)
        expect(denied.locator("#shared-view-notice")).to_contain_text(
            "2 selected run(s) are unavailable"
        )
        expect(denied.locator(".chart svg")).to_have_count(0)
        assert (
            httpx.get(
                server["url"] + f"/api/runs/{uids[0]}",
                headers={"Authorization": "Bearer " + outsider_key},
            ).status_code
            == 403
        )
        assert httpx.get(server["url"] + f"/api/runs/{uids[0]}").status_code == 401
        # Simulate OAuth's redirect to /; same-tab pending state survives login.
        login = new_page(reader_key)
        login.add_init_script(
            f"sessionStorage.setItem('open-train-pending-view', {json.dumps(urlsplit(link).fragment[5:])})"
        )
        login.goto(server["url"])
        expect(
            login.locator('.chart[data-metric="train/loss"] .plot-axis')
        ).to_have_value("train/global_step")
        malformed = new_page(reader_key)
        malformed.goto(server["url"] + "/#view=" + quote(json.dumps({"version": 99})))
        expect(malformed.locator("#shared-view-notice")).to_contain_text("Invalid")
        expect(malformed.locator("#selection-count")).to_have_text("0 runs selected")
        malformed.get_by_label("Compare ui-0", exact=True).check()
        expect(malformed.locator(".chart svg").first).to_be_visible()
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize("server", ["accounts"], indirect=True)
def test_workspace_member_list_survives_reopening_and_refreshes_after_save(server):
    app = create_app(server["directory"] / "data")
    owner = app.state.accounts.authenticate(server["env"]["WANDB_API_KEY"], "api_key")
    teammate = app.state.accounts.login_identity(
        "github", "membership-reader", "reader@example.com", "Teammate"
    )
    session = app.state.accounts.issue(owner["id"], "session", "browser test", 1)["key"]
    with app.state.store.connect() as db:
        existing_members = db.execute(
            "SELECT COUNT(*) FROM memberships WHERE entity=?", (owner["username"],)
        ).fetchone()[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        context.add_cookies(
            [{"name": "open_train_session", "value": session, "url": server["url"]}]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server["url"])
        page.get_by_label("Account & access", exact=True).click()
        dialog = page.locator("dialog[open]")
        roster = dialog.locator(
            f'.workspace-membership[data-workspace="{owner["username"]}"]'
        )
        expect(roster.locator("li")).to_have_count(existing_members)
        expect(dialog.get_by_label("Workspace", exact=True)).to_have_value(
            owner["username"]
        )
        dialog.get_by_label("User entity name", exact=True).fill(teammate["username"])
        dialog.get_by_label("Role", exact=True).select_option("reader")
        dialog.get_by_role("button", name="Save membership", exact=True).click()
        expect(roster.locator("li")).to_have_count(existing_members + 1)
        expect(roster).to_contain_text(f"{teammate['username']} · reader")
        dialog.get_by_label("Role", exact=True).select_option("writer")
        dialog.get_by_role("button", name="Save membership", exact=True).click()
        expect(roster).to_contain_text(f"{teammate['username']} · writer")
        dialog.get_by_label("Close", exact=True).click()
        page.get_by_label("Account & access", exact=True).click()
        expect(roster).to_contain_text(f"{teammate['username']} · writer")
        dialog.get_by_role("button", name="Refresh members", exact=True).click()
        expect(roster.locator("li")).to_have_count(existing_members + 1)
        page.reload()
        page.get_by_label("Account & access", exact=True).click()
        expect(roster).to_contain_text(f"{teammate['username']} · writer")
        assert not errors
        browser.close()


def pytest_regex(prefix):
    import re

    return re.compile("^" + re.escape(prefix))
