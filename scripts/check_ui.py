"""Browser acceptance checks against a running demo-seeded server."""

import argparse
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8080")
parser.add_argument("--output", default="test-results")
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1440, "height": 1060}, device_scale_factor=1
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if key := os.environ.get("WANDB_API_KEY"):
        import json

        page.add_init_script(
            f"sessionStorage.setItem('open-train-key', {json.dumps(key)})"
        )
    page.goto(args.base_url)
    expect(page.locator("#connection")).to_contain_text("Live")
    expect(page.locator(".chart svg").first).to_be_visible()
    assert page.locator("#runs-body .run-row").count() >= 3
    page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
    page.locator("#search").fill("transformer-resumed")
    expect(page.locator("#runs-body .run-row")).to_have_count(1)
    page.locator(".run-name").click()
    expect(page.locator("#detail")).to_be_visible()
    page.locator('[data-detail="config"]').click()
    expect(page.locator("#detail-content")).to_contain_text("learning_rate")
    page.locator("#close-detail").click()
    page.locator("#search").fill("")
    page.locator("#x-axis").select_option("global_step")
    metric = page.locator('.chart[data-metric="eval/accuracy"]')
    metric.scroll_into_view_if_needed()
    expect(metric.locator("svg")).to_have_attribute(
        "aria-label",
        "eval/accuracy by global_step. Drag a rectangle to zoom. Use plus, minus, or zero keys to zoom and reset.",
    )
    page.locator('[data-view="connect"]').click()
    expect(page.locator("#connect-code")).to_contain_text("WANDB_BASE_URL")
    page.locator('[data-view="experiments"]').click()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".chart svg").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        "Horizontal overflow on mobile"
    )
    page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1060})
    page.locator("#search").fill("media-example")
    if page.locator("#runs-body .run-row").count():
        page.locator(".run-name").first.click()
        page.locator('[data-detail="files"]').click()
        page.get_by_role("button", name="Preview table").first.click()
        expect(page.locator(".data-table")).to_contain_text("rectangle")
        page.wait_for_function(
            "document.querySelector('.file-item img')?.naturalWidth > 0"
        )
        page.screenshot(path=str(output / "media-preview.png"))
    browser.close()
    assert not errors, errors
    print(f"Browser checks passed. Screenshots: {output.resolve()}")
