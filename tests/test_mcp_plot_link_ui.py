"""Verify MCP-generated links against the real dashboard contract."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from open_train.store import Store

MCP = Path(
    os.environ.get(
        "OPENTRAIN_MCP_DIR", Path(__file__).resolve().parents[2] / "opentrain-mcp"
    )
)
pytestmark = pytest.mark.skipif(
    os.environ.get("OPEN_TRAIN_BROWSER_TESTS") != "1"
    or not (MCP / "src/links.js").is_file(),
    reason="Opt-in Chromium test requiring the sibling opentrain-mcp checkout",
)


def test_mcp_wall_time_link_opens_exact_comparison(server):
    store = Store(server["directory"] / "data")
    key = "train/reward [a+b]? 雪#%"
    uids = []
    for index in range(2):
        uid = store.upsert({"name": f"link-run-{index}", "modelName": "mcp-links"})[0][
            "uid"
        ]
        uids.append(uid)
        store.stream(
            uid,
            {
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 0,
                        "content": [
                            json.dumps(
                                {
                                    "_step": i,
                                    "_timestamp": 1710000000 + i * 60 + index * 10,
                                    key: i + index + 1,
                                    "train/reward-other": i,
                                }
                            )
                            for i in range(5)
                        ],
                    }
                }
            },
        )
    code = f"""
        import {{OpenTrainClient}} from {json.dumps((MCP / "src/client.js").as_uri())};
        import {{getPlotLink}} from {json.dumps((MCP / "src/links.js").as_uri())};
        const client = new OpenTrainClient({{baseUrl:{json.dumps(server["url"])},key:'local-fixture-only'}});
        console.log(JSON.stringify(await getPlotLink(client,{{run_uids:{json.dumps(uids)},key:{json.dumps(key)},axis:'_timestamp',smoothing:.35,scale:'log',session_view:'all'}})));
    """
    result = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "--eval", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    )
    assert "local-fixture-only" not in result["plot_url"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(result["plot_url"])
        expect(page.locator("#shared-view-notice")).to_contain_text(
            "Shared view restored"
        )
        expect(page.locator("#selection-count")).to_have_text("2 runs selected")
        expect(page.locator("#project-filter")).to_have_value("local/mcp-links")
        expect(page.locator("#x-axis")).to_have_value("_timestamp")
        expect(page.locator("#session-view")).to_have_value("all")
        plot = page.locator("#chart-grid .chart")
        expect(plot).to_have_count(1)
        expect(plot).to_have_attribute("data-metric", key)
        expect(plot.locator("svg")).to_be_visible()
        expect(plot.locator(".plot-value-row")).to_have_count(2)
        expect(plot.locator(".plot-smoothing")).to_have_value("0.35")
        expect(plot.locator(".plot-scale")).to_have_value("log")
        assert plot.evaluate(
            "c => c.liveSeries.every(s => s.axis === '_timestamp' && s.points[0][0] >= 1710000000)"
        )
        plot.locator(".plot-maximize").click()
        expect(page.locator(".plot-dialog svg")).to_be_visible()
        page.locator(".plot-dialog").screenshot(
            path="/tmp/opentrain-mcp-wall-time-link.png"
        )
        assert not errors
        browser.close()
