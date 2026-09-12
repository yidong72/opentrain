"""Disposable browser regression for lazy metric groups, hover, caching and sessions."""

import json
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


def main():
    with tempfile.TemporaryDirectory(prefix="opentrain-metrics-ui-") as directory:
        app = create_app(directory)
        keys = (
            ["train/loss", "train/timing/step_seconds"]
            + [f"train/metric_{i:02}" for i in range(25)]
            + [f"eval/task_{i:02}/accuracy" for i in range(20)]
        )
        for index in range(2):
            sessions = [
                {
                    "directory": f"audit/tb/rl_{i}",
                    "first_step": i * 20,
                    "last_step": i * 20 + 19,
                    "events": 20 * len(keys),
                    "first_wall_time": 100 + i * 20,
                    "last_wall_time": 119 + i * 20,
                }
                for i in range(2)
            ]
            uid = app.state.store.upsert(
                {
                    "name": f"audit-{index}",
                    "displayName": f"Audit {index}",
                    "config": json.dumps(
                        {"tensorboard_import": {"value": {"sessions": sessions}}}
                    ),
                }
            )[0]["uid"]
            app.state.store.import_events(
                uid,
                [
                    {
                        "step": step,
                        "wall_time": 100 + step,
                        "restart": False,
                        "values": {
                            k: step + index + j / 100 for j, k in enumerate(keys)
                        },
                    }
                    for step in range(40)
                ],
            )
        for index in range(25):
            app.state.store.upsert(
                {"name": f"empty-{index}", "displayName": f"Empty {index}"}
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPEN_TRAIN_")}
        with (Path(directory) / "server.log").open("w") as logs:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "open_train.cli",
                    "serve",
                    "--port",
                    str(port),
                    "--data-dir",
                    directory,
                ],
                env=env,
                stdout=logs,
                stderr=logs,
            )
            try:
                for _ in range(100):
                    try:
                        if httpx.get(base + "/healthz").is_success:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page(viewport={"width": 1440, "height": 1060})
                    failures, requests = [], []
                    page.on("pageerror", lambda e: failures.append(str(e)))
                    page.on(
                        "request",
                        lambda r: (
                            requests.append(r.url) if "/series" in r.url else None
                        ),
                    )
                    page.goto(base)
                    expect(page.locator(".chart svg").first).to_be_visible()
                    expect(page.locator("#metric-count")).to_contain_text("47 of 47")
                    assert len(requests) < 5
                    assert all(url.endswith("/api/series") for url in requests)
                    expect(page.locator("#runs-body .run-row")).to_have_count(20)
                    expect(page.locator("#runs-prev")).to_be_disabled()
                    page.locator("#runs-next").click()
                    expect(page.locator("#runs-body .run-row")).to_have_count(7)
                    expect(page.locator("#runs-next")).to_be_disabled()
                    assert page.evaluate("state.selected.size") == 2
                    candidate = page.locator(
                        "#runs-body input:not(:checked)"
                    ).first.get_attribute("aria-label")
                    page.get_by_role("checkbox", name=candidate, exact=True).check()
                    assert page.evaluate("state.selected.size") == 3
                    page.locator("#runs-prev").click()
                    assert page.evaluate("state.selected.size") == 3
                    page.locator("#search").fill("Audit")
                    expect(page.locator("#runs-body .run-row")).to_have_count(2)
                    expect(page.locator("#selection-count")).to_have_text(
                        "3 runs selected"
                    )
                    page.locator("#clear-selection").click()
                    page.locator("#select-all").check()
                    expect(page.locator("#selection-count")).to_have_text(
                        "2 runs selected"
                    )
                    page.locator(".run-filters summary").click()
                    page.locator("#state-filter").select_option("failed")
                    expect(page.locator("#runs-body .run-row")).to_have_count(0)
                    expect(page.locator("#selection-count")).to_have_text(
                        "2 runs selected"
                    )
                    page.locator("#clear-filters").click()
                    page.locator("#run-page-size").select_option("50")
                    expect(page.locator("#runs-body .run-row")).to_have_count(27)
                    page.locator("#search").fill("Audit")
                    page.locator(".run-filters summary").click()
                    page.locator("#metric-search").fill("train/metric_24")
                    expect(page.locator("#metric-count")).to_contain_text("1 of 47")
                    svg = page.locator('.chart[data-metric="train/metric_24"] svg')
                    expect(svg).to_be_visible()
                    svg.hover(position={"x": 250, "y": 100})
                    expect(
                        page.locator('.hover-point[visibility="visible"]')
                    ).to_have_count(2)
                    expect(
                        page.locator('.hover-guide[visibility="visible"]')
                    ).to_have_count(1)
                    expect(page.locator(".tooltip")).to_contain_text("value:")
                    assert page.locator(".session-start").count() == 2
                    before = len(requests)
                    page.evaluate(
                        "window.previousSVG = document.querySelector('.chart svg'); document.querySelector('.plot-smoothing').value = 0.6; document.querySelector('.plot-smoothing').dispatchEvent(new Event('input'))"
                    )
                    page.wait_for_function(
                        "document.querySelector('.chart svg') && document.querySelector('.chart svg') !== window.previousSVG"
                    )
                    assert len(requests) == before, "Smoothing must reuse cached series"
                    svg.hover(position={"x": 330, "y": 100})
                    expect(page.locator(".tooltip")).to_contain_text("smoothed:")
                    expect(page.locator(".tooltip")).to_contain_text("S2")
                    values = page.locator(".hover-point").evaluate_all(
                        "(points) => points.map(p => ({x: +p.dataset.x, y: +p.dataset.value}))"
                    )
                    assert all(
                        0 <= p["x"] <= 39 and p["y"] < p["x"] + 1 for p in values
                    )
                    page.mouse.move(0, 0)
                    expect(
                        page.locator('.hover-point[visibility="visible"]')
                    ).to_have_count(0)
                    card = page.locator('.chart[data-metric="train/metric_24"]')
                    full_domain = json.loads(card.get_attribute("data-domain"))
                    card.locator(".plot-zoom-in").click()
                    zoomed = json.loads(card.get_attribute("data-domain"))
                    assert (
                        zoomed[1] - zoomed[0] == (full_domain[1] - full_domain[0]) / 2
                    )
                    card.locator(".plot-zoom-out").click()
                    assert json.loads(card.get_attribute("data-domain")) == full_domain
                    svg.focus()
                    page.keyboard.press("+")
                    page.keyboard.press("+")
                    keyboard_domain = json.loads(card.get_attribute("data-domain"))
                    assert (
                        keyboard_domain[1] - keyboard_domain[0]
                        == (full_domain[1] - full_domain[0]) / 4
                    )
                    page.keyboard.press("0")
                    assert json.loads(card.get_attribute("data-domain")) == full_domain
                    box = svg.bounding_box()
                    page.mouse.move(
                        box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.2
                    )
                    page.mouse.down()
                    page.mouse.move(
                        box["x"] + box["width"] * 0.7,
                        box["y"] + box["height"] * 0.7,
                        steps=5,
                    )
                    expect(card.locator(".zoom-brush")).to_have_attribute(
                        "visibility", "visible"
                    )
                    page.mouse.up()
                    zoomed = json.loads(card.get_attribute("data-domain"))
                    assert zoomed[1] - zoomed[0] < full_domain[1] - full_domain[0]
                    card.locator(".plot-maximize").click()
                    expanded = page.locator(".plot-dialog")
                    expect(expanded).to_be_visible()
                    assert (
                        expanded.locator("svg").bounding_box()["width"] > box["width"]
                    )
                    assert (
                        json.loads(
                            expanded.locator(".chart").get_attribute("data-domain")
                        )
                        == zoomed
                    )
                    expect(expanded.locator(".plot-smoothing")).to_have_value("0.6")
                    expanded.locator(".plot-smoothing").fill("0.3")
                    expanded.locator(".plot-reset").click()
                    Path("test-results").mkdir(exist_ok=True)
                    page.screenshot(path="test-results/expanded-plot.png")
                    page.keyboard.press("Escape")
                    expect(page.locator(".plot-dialog")).to_have_count(0)
                    expect(card.locator(".plot-smoothing")).to_have_value("0.3")
                    assert json.loads(card.get_attribute("data-domain")) == full_domain
                    assert len(requests) == before, (
                        "Plot controls must not refetch series"
                    )
                    before = len(requests)
                    page.wait_for_timeout(5500)
                    assert len(requests) == before, (
                        "Unchanged polling must not refetch plots"
                    )
                    page.locator("#metric-search").fill("eval/task_19/accuracy")
                    expect(
                        page.locator('.chart[data-metric="eval/task_19/accuracy"] svg')
                    ).to_be_visible()
                    expect(
                        page.locator('details[data-group="eval/task_19"]')
                    ).to_have_attribute("open", "")
                    page.locator(".session-badge").first.click()
                    expect(page.locator("#detail-content .session-card")).to_have_count(
                        2
                    )
                    expect(page.locator("#detail-content")).to_contain_text(
                        "Steps 20–39"
                    )
                    page.locator("#close-detail").click()
                    page.locator("#metric-search").fill("")
                    expect(page.locator("#metric-count")).to_contain_text("47 of 47")
                    page.locator("#collapse-metrics").click()
                    expect(page.locator(".metric-group[open]")).to_have_count(0)
                    page.locator("#expand-metrics").click()
                    expect(page.locator(".chart svg").first).to_be_visible()
                    # Other plots retain their own smoothing, not the edited value.
                    other = page.locator('.chart[data-metric="train/loss"]')
                    other.scroll_into_view_if_needed()
                    expect(other.locator(".plot-smoothing")).to_have_value("0")
                    page.reload()
                    page.locator("#metric-search").fill("train/metric_24")
                    expect(
                        page.locator(
                            '.chart[data-metric="train/metric_24"] .plot-smoothing'
                        )
                    ).to_have_value("0.3")
                    page.locator("#metric-search").fill("train/")
                    expect(page.locator("#metric-count")).to_contain_text("27 of 47")
                    page.locator("#search").fill("Audit")
                    page.evaluate("window.scrollTo(0, 0)")
                    Path("test-results").mkdir(exist_ok=True)
                    page.screenshot(path="test-results/sidebar-plots.png")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    ), "Mobile overflow"
                    page.screenshot(path="test-results/sidebar-mobile.png")
                    assert not failures, failures
                    browser.close()
                    print(
                        "Sidebar filters/pagination/selection, per-plot smoothing, drag/button/keyboard zoom, maximize, lazy loading, cache, sessions and mobile checks passed."
                    )
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
