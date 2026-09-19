"""Metric navigation, compact plots and raw statistics in a real browser."""

import json
import os
from urllib.parse import unquote, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from open_train.app import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("OPEN_TRAIN_BROWSER_TESTS") != "1", reason="Opt-in Chromium tests"
)


def test_metric_studio(server):
    store = create_app(server["directory"] / "data").state.store
    uid = store.upsert({"name": "studio-run", "modelName": "studio"})[0]["uid"]
    rows = [
        {
            "_step": i,
            "_timestamp": 100 + i,
            **{f"train/loss{n:02}": i - 1 for n in range(50)},
            "eval/quality/score": i,
        }
        for i in range(5)
    ]
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [json.dumps(row) for row in rows],
                }
            }
        },
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1536, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server["url"])
        expect(page.locator("#selection-count")).to_have_text("0 runs selected")
        page.get_by_label("Project", exact=True).select_option("local/studio")
        page.get_by_label("Compare studio-run", exact=True).check()
        plot = page.locator('.chart[data-metric="train/loss00"]')
        expect(plot.locator("svg")).to_be_visible()
        expect(page.locator("#chart-grid .chart")).to_have_count(24)
        assert (
            len(
                page.locator(".chart-grid").first.evaluate(
                    "e => getComputedStyle(e).gridTemplateColumns.split(' ')"
                )
            )
            == 3
        )
        page.screenshot(path="/tmp/opentrain-studio-grid.png", full_page=False)
        expect(plot.locator(".plot-settings")).not_to_have_attribute("open", "")
        expect(plot.locator(".plot-values")).to_contain_text("3")
        expect(plot.locator(".latest-point")).to_have_count(1)
        page.locator("#more-metrics").click()
        expect(page.locator("#chart-grid .chart")).to_have_count(48)
        page.locator("#metrics-tab").click()
        page.get_by_role("button", name="Show eval metrics", exact=True).click()
        expect(page.locator("#chart-grid .chart")).to_have_count(1)
        expect(page.locator('[data-metric="eval/quality/score"] svg')).to_be_visible()
        expect(page.locator("#metric-breadcrumbs")).to_contain_text("eval")
        expect(page.locator("#runs-panel")).to_be_hidden()
        page.locator("#share-view").click()
        category_link = page.get_by_label("Share link", exact=True).input_value()
        assert (
            json.loads(unquote(urlsplit(category_link).fragment[5:]))["category"]
            == "eval"
        )
        page.get_by_role("button", name="Close", exact=True).click()
        page.get_by_role("button", name="Show all metrics", exact=True).click()
        page.get_by_label("Search metrics").fill("/loss0[01]$/")
        expect(page.locator("#chart-grid .chart")).to_have_count(2)
        expect(plot.locator("svg")).to_be_visible()
        plot.locator(".plot-settings > summary").click()
        plot.locator(".plot-scale").select_option("log")
        expect(plot.locator(".plot-warning")).to_contain_text("2 nonpositive")
        assert "NaN" not in plot.locator(".metric-line").first.get_attribute("d")
        assert "Infinity" not in plot.locator(".metric-line").first.get_attribute("d")
        expect(page.locator('[data-metric="train/loss01"] .plot-scale')).to_have_value(
            "linear"
        )
        plot.locator(".plot-zoom-in").click()
        zoom = plot.get_attribute("data-domain")
        plot.locator(".plot-share").click()
        link = page.get_by_label("Share link", exact=True).input_value()
        page.get_by_role("button", name="Close", exact=True).click()
        shared = browser.new_page()
        shared.goto(link)
        shared_plot = shared.locator('.chart[data-metric="train/loss00"]')
        expect(shared_plot.locator("svg")).to_be_visible()
        expect(shared_plot.locator(".plot-scale")).to_have_value("log")
        expect(shared_plot).to_have_attribute("data-domain", zoom)
        shared.close()
        plot.locator(".plot-maximize").click()
        modal = page.locator(".plot-dialog")
        expect(modal.locator(".plot-statistics")).to_contain_text("All paired records")
        expect(modal.locator("td")).to_have_count(7)
        assert modal.locator("td").all_text_contents() == [
            "studio-run",
            "3",
            "+1",
            "-1",
            "3",
            "1",
            "5",
        ]
        modal.screenshot(path="/tmp/opentrain-studio-modal.png")
        modal.get_by_label("Close expanded plot").click()
        page.get_by_label("Search metrics").fill("/[invalid/")
        expect(page.locator("#metric-search-error")).to_be_visible()
        page.get_by_label("Search metrics").fill("quality")
        expect(page.locator("#chart-grid .chart")).to_have_count(1)
        expect(page.locator('[data-metric="eval/quality/score"] svg')).to_be_visible()
        page.screenshot(path="/tmp/opentrain-studio-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#x-axis").evaluate(
            "e => e.add(new Option('eval/a_very_long_metric_namespace_with_a_long_global_step_name', 'long-test'))"
        )
        page.screenshot(path="/tmp/opentrain-studio-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()
