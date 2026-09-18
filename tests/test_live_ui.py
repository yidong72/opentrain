"""Live telemetry must not replace the reader's dashboard or interactions."""

import json
import os

import pytest
from playwright.sync_api import expect, sync_playwright

from open_train.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("OPEN_TRAIN_BROWSER_TESTS") != "1",
    reason="Set OPEN_TRAIN_BROWSER_TESTS=1 with Playwright Chromium installed",
)


def test_live_updates_preserve_plots_and_only_fetch_open_visible_metrics(server):
    store = create_app(server["directory"] / "data").state.store
    uids = [
        store.upsert({"name": f"live-{i}", "modelName": "live-ui"})[0]["uid"]
        for i in range(2)
    ]

    def append(index, start, stop):
        store.stream(
            uids[index],
            {
                "files": {
                    "wandb-history.jsonl": {
                        "offset": start,
                        "content": [
                            json.dumps(
                                {
                                    "_step": step,
                                    "_timestamp": 100 + step,
                                    "train/loss": 1 / (step + 1) + index,
                                    "train/reward": step,
                                    "hidden/loss": step,
                                    **{f"train/z{i:02}": step for i in range(12)},
                                }
                            )
                            for step in range(start, stop)
                        ],
                    }
                }
            },
        )

    for i in range(2):
        append(i, 0, 20)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        # Drive the same poll function deterministically rather than racing a timer.
        page.add_init_script("window.setInterval = () => 0")
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda req: (
                requests.append(req.post_data_json)
                if req.url.endswith("/api/series")
                else None
            ),
        )
        page.goto(server["url"])
        page.get_by_label("Project", exact=True).select_option("local/live-ui")
        page.get_by_label("Compare live-0", exact=True).check()
        page.get_by_label("Compare live-1", exact=True).check()
        plot = page.locator('.chart[data-metric="train/loss"]')
        expect(plot.locator("svg")).to_be_visible()
        page.locator('[data-group="hidden"]').evaluate("e => e.open = false")
        plot.locator(".plot-smoothing").fill("0.6")
        plot.locator(".plot-zoom-in").click()
        page.mouse.move(10, 10)
        page.evaluate("document.activeElement.blur()")
        before = page.evaluate("""() => {
            window.liveCard = document.querySelector('.chart[data-metric="train/loss"]');
            window.liveSvg = liveCard.querySelector('svg');
            window.liveGroup = liveCard.closest('details');
            window.liveRow = document.querySelector('#runs-body .run-row');
            window.liveSelector = document.querySelector('#project-filter').firstChild;
            return {domain: liveCard.dataset.domain, scroll: window.scrollY,
                    keys: [...document.querySelectorAll('#chart-grid .chart')].filter(openedPlot).map(c=>c.dataset.metric)};
        }""")
        requests.clear()
        append(0, 20, 21)
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "41")
        assert page.evaluate(
            "liveCard === document.querySelector('.chart[data-metric=\"train/loss\"]') && liveSvg === liveCard.querySelector('svg') && liveGroup === liveCard.closest('details') && liveRow === document.querySelector('#runs-body .run-row') && liveSelector === document.querySelector('#project-filter').firstChild"
        )
        assert plot.get_attribute("data-domain") == before["domain"]
        expect(plot.locator(".plot-smoothing")).to_have_value("0.6")
        assert page.evaluate("window.scrollY") == before["scroll"]
        assert requests and all(set(r["keys"]) <= set(before["keys"]) for r in requests)
        assert all(r["runs"] == [uids[0]] for r in requests)

        # A hovered value, highlight and SVG stay stable until the cursor leaves.
        svg = plot.locator("svg")
        svg.hover(position={"x": 120, "y": 70})
        tooltip = plot.locator(".tooltip")
        expect(tooltip).to_be_visible()
        tip = tooltip.text_content()
        path = svg.locator(".metric-line").first.get_attribute("d")
        append(0, 21, 22)
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-pending-update", "true")
        assert tooltip.text_content() == tip
        assert svg.locator(".metric-line").first.get_attribute("d") == path
        page.mouse.move(10, 10)
        expect(plot).to_have_attribute("data-total", "42")

        # Failures retain the last good plot, and a retry replaces snapshots once.
        page.route(
            "**/api/series",
            lambda route: route.fulfill(status=503, body="temporarily unavailable"),
        )
        append(0, 22, 23)
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "42")
        assert page.evaluate("liveCard.isConnected && liveSvg.isConnected")
        page.unroute("**/api/series")
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "43")
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "43")

        # Maximized plots update too, without replacing the modal or background.
        plot.locator(".plot-maximize").click()
        dialog = page.locator(".plot-dialog")
        expanded = dialog.locator(".chart")
        page.mouse.move(10, 10)
        page.evaluate(
            "window.liveDialog = document.querySelector('.plot-dialog'); window.expandedSvg = liveDialog.querySelector('svg')"
        )
        requests.clear()
        append(0, 23, 24)
        page.evaluate("() => refresh()")
        expect(expanded).to_have_attribute("data-total", "44")
        assert requests and all(r["keys"] == ["train/loss"] for r in requests)
        assert page.evaluate(
            "liveDialog === document.querySelector('.plot-dialog') && expandedSvg === liveDialog.querySelector('svg') && liveCard.isConnected"
        )
        dialog.get_by_role("button", name="Close expanded plot").click()
        page.mouse.move(10, 10)
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "44")

        # A collapsed/offscreen group catches up when opened, without refreshing
        # the charts the user has now scrolled away from.
        append(0, 24, 25)
        hidden_group = page.locator('[data-group="hidden"]')
        hidden_group.locator("summary").click()
        hidden_plot = page.locator('.chart[data-metric="hidden/loss"]')
        expect(hidden_plot).to_have_attribute("data-total", "45")
        hidden_plot.scroll_into_view_if_needed()
        page.mouse.move(10, 10)
        requests.clear()
        append(0, 25, 26)
        page.evaluate("() => refresh()")
        expect(hidden_plot).to_have_attribute("data-total", "46")
        assert requests and all("train/loss" not in r["keys"] for r in requests)

        # A delayed old-axis response must not repaint a newer user selection.
        held = []

        def delay_first(route):
            if not held:
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/series", delay_first)
        append(0, 26, 27)
        page.evaluate("() => { void refresh(); }")
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(20)
        assert held
        hidden_plot.locator(".plot-axis").select_option("_timestamp")
        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "series": {
                        uids[0]: {
                            "hidden/loss": {
                                "axis": "_step",
                                "points": [[999, 999]],
                                "total": 999,
                            }
                        }
                    }
                }
            ),
        )
        hidden_plot.scroll_into_view_if_needed()
        expect(hidden_plot).to_have_attribute("data-total", "47")
        expect(hidden_plot.locator("svg")).to_have_attribute(
            "aria-label", pytest_regex("hidden/loss by _timestamp")
        )
        page.unroute("**/api/series")
        assert not errors
        browser.close()


def pytest_regex(value):
    import re

    return re.compile(re.escape(value))
