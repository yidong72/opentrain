import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_trajectories import log, resumed

pytestmark = pytest.mark.skipif(
    os.environ.get("OPEN_TRAIN_BROWSER_TESTS") != "1", reason="Opt-in Chromium tests"
)


def test_overlap_views_smoothing_and_share(server):
    store, uid = resumed(server["directory"] / "data")
    with store.connect(write=True) as db:
        db.execute("UPDATE runs SET project='trajectory-ui' WHERE uid=?", (uid,))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server["url"])
        page.get_by_label("Project", exact=True).select_option("local/trajectory-ui")
        page.get_by_label("Compare run", exact=True).check()
        page.get_by_label("Search metrics").fill("train/loss")
        plot = page.locator('.chart[data-metric="train/loss"]')
        expect(plot.locator("svg")).to_be_visible()
        expect(page.get_by_label("Session view")).to_have_value("latest")
        expect(plot).to_have_attribute("data-total", "4")
        expect(plot.locator(".trajectory-note")).to_contain_text(
            "3 earlier-tail records hidden"
        )
        expect(plot).to_have_attribute("data-continuous", "true")
        expect(plot.locator(".metric-line")).to_have_count(1)
        assert (
            plot.locator('.metric-line[data-session="trajectory"]').get_attribute(
                "stroke-dasharray"
            )
            == "none"
        )
        # EMA crosses the retained resume boundary: [1, 41, 550, 700]
        # becomes [1, 21, 285.5, 492.75], not a reset to 550.
        values = plot.evaluate(
            "c => displayCurves(c.liveSeries[0], 'train/global_step', 0.5, false)[0].displayPoints.map(p => p.y)"
        )
        assert values == [1, 21, 285.5, 492.75]
        assert plot.evaluate("c => c.liveSeries[0].points.map(p => p[0])") == [
            0,
            40,
            55,
            70,
        ]
        plot.locator(".plot-session-view").select_option("all")
        expect(page.get_by_label("Session view", exact=True)).to_have_value("all")
        expect(plot).to_have_attribute("data-total", "7")
        assert (
            plot.locator('.metric-line[data-session="sdk:one"]')
            .get_attribute("d")
            .count("L")
            == 4
        )
        assert (
            plot.locator('.metric-line[data-session="sdk:two"]')
            .get_attribute("d")
            .count("L")
            == 1
        )
        assert (
            plot.locator('.metric-line[data-session="sdk:two"]').get_attribute(
                "stroke-dasharray"
            )
            == "none"
        )
        assert plot.locator('.metric-line[data-session="sdk:one"]').get_attribute(
            "stroke"
        ) != plot.locator('.metric-line[data-session="sdk:two"]').get_attribute(
            "stroke"
        )
        plot.locator(".plot-settings > summary").click()
        plot.locator(".plot-smoothing").fill("0.5")
        svg = plot.locator("svg")
        # S2 inherits EMA=21 from x=40 before its checkpoint at x=50,
        # not the abandoned tail at x=100, nor a fresh reset to 550.
        box = svg.bounding_box()
        svg.hover(
            position={
                "x": box["width"] * (47 + 0.55 * 338) / 400,
                "y": box["height"] * 0.5,
            }
        )
        expect(plot.locator(".tooltip")).to_contain_text("S1")
        expect(plot.locator(".tooltip")).to_contain_text("S2")
        expect(plot.locator(".tooltip")).to_contain_text("smoothed: 285.5")
        expect(plot).to_have_attribute("data-resume-smoothing", "true")
        page.mouse.move(5, 5)
        page.locator("#share-view").click()
        link = page.get_by_label("Share link", exact=True).input_value()
        page.get_by_role("button", name="Close", exact=True).click()
        shared = browser.new_page()
        shared.goto(link)
        expect(shared.get_by_label("Session view")).to_have_value("all")
        expect(shared.locator('.chart[data-metric="train/loss"]')).to_have_attribute(
            "data-total", "7"
        )
        shared.close()
        # Switching views must not return the cached data from the other view.
        page.get_by_label("Session view").select_option("latest")
        expect(plot).to_have_attribute("data-total", "4")
        plot.locator(".plot-maximize").click()
        expect(page.locator(".plot-dialog .plot-statistics")).to_contain_text(
            "Latest trajectory"
        )
        page.locator(".plot-dialog").screenshot(
            path="/tmp/opentrain-latest-trajectory.png"
        )
        page.get_by_label("Close expanded plot").click()
        page.get_by_label("Session view").select_option("all")
        expect(plot).to_have_attribute("data-total", "7")
        plot.locator(".plot-maximize").click()
        expect(page.locator(".plot-dialog .plot-statistics")).to_contain_text(
            "All paired records"
        )
        page.locator(".plot-dialog").screenshot(path="/tmp/opentrain-all-sessions.png")
        # The visible selector works inside the expanded plot as well.
        page.locator(".plot-dialog .plot-session-view").select_option("latest")
        expect(page.locator(".plot-dialog .metric-line")).to_have_count(1)
        expect(page.locator(".plot-dialog .chart")).to_have_attribute(
            "data-continuous", "true"
        )
        # Logarithmic grid uses readable values, not arbitrary fractional powers.
        assert page.evaluate("axisTicks(-3, 3, true)") == [
            0.001,
            0.01,
            0.1,
            1,
            10,
            100,
            1000,
        ]
        assert page.evaluate("axisTicks(Math.log10(0.4), Math.log10(0.8), true)") == [
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
        ]
        page.get_by_label("Close expanded plot").click()
        # Unknown/shared provenance and overlapping custom axes must not join.
        expect(page.locator(".plot-dialog")).to_have_count(0)
        assert (
            plot.evaluate(
                "c => displayCurves({...c.liveSeries[0], trajectory:{...c.liveSeries[0].trajectory, stitch_sessions:false}}, 'train/global_step', .5, false).length"
            )
            == 2
        )
        assert (
            plot.evaluate(
                "c => displayCurves(c.liveSeries[0], '_runtime', .5, false).length"
            )
            == 2
        )
        assert not errors
        browser.close()


def test_session_colors_run_patterns_and_live_highlight(server):
    store, first = resumed(server["directory"] / "data")
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE runs SET project='colors-ui', name='first', display_name='first' WHERE uid=?",
            (first,),
        )
    store, second = resumed(server["directory"] / "data")
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE runs SET project='colors-ui', name='second', display_name='second' WHERE uid=?",
            (second,),
        )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.add_init_script("window.setInterval = () => 0")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server["url"])
        page.get_by_label("Project", exact=True).select_option("local/colors-ui")
        page.get_by_label("Compare first", exact=True).check()
        page.get_by_label("Compare second", exact=True).check()
        page.get_by_label("Search metrics").fill("train/loss")
        page.get_by_label("Session view").select_option("all")
        plot = page.locator('.chart[data-metric="train/loss"]')
        expect(plot).to_have_attribute("data-total", "14")
        paths = plot.locator(".metric-line")
        expect(paths).to_have_count(4)
        attrs = paths.evaluate_all(
            "nodes => nodes.map(n => ({run:n.dataset.run, session:n.dataset.session, color:n.getAttribute('stroke'), dash:n.getAttribute('stroke-dasharray')}))"
        )
        assert len({a["dash"] for a in attrs}) == 2
        for uid in (first, second):
            assert len({a["color"] for a in attrs if a["run"] == uid}) == 2
            assert len({a["dash"] for a in attrs if a["run"] == uid}) == 1
        for session in ("sdk:one", "sdk:two"):
            assert len({a["color"] for a in attrs if a["session"] == session}) == 1
        plot.locator(".plot-settings > summary").click()
        plot.locator(".plot-smoothing").fill("0.5")
        expect(
            plot.locator('.metric-line[data-inherited-smoothing="true"]')
        ).to_have_count(2)
        plot.locator(".session-legend > summary").click()
        chip = plot.locator(
            f'.session-chip[data-run="{first}"][data-session="sdk:two"]'
        )
        chip.click()
        page.mouse.move(5, 5)
        page.evaluate("document.activeElement.blur()")
        expect(chip).to_have_attribute("aria-pressed", "true")
        expect(plot.locator('.metric-line[opacity="0.12"]')).to_have_count(3)
        chip.evaluate("e => window.pinnedSessionChip = e")
        log(
            store,
            first,
            [
                {
                    "_step": 9,
                    "_timestamp": 205,
                    "train/global_step": 80,
                    "train/loss": 800,
                }
            ],
            offset=9,
        )
        page.evaluate("() => refresh()")
        expect(plot).to_have_attribute("data-total", "15")
        expect(
            plot.locator('.metric-line[data-inherited-smoothing="true"]')
        ).to_have_count(2)
        expect(chip).to_have_attribute("aria-pressed", "true")
        assert chip.evaluate("e => e === window.pinnedSessionChip")
        expect(plot.locator('.metric-line[opacity="0.12"]')).to_have_count(3)
        plot.get_by_role("button", name="Show all", exact=True).click()
        expect(plot.locator('.metric-line[opacity="1"]')).to_have_count(4)
        assert not errors
        browser.close()


def test_checkpoint_seed_ignores_abandoned_branches_and_preserves_gaps(server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(server["url"])
        page.wait_for_function("typeof displayCurves === 'function'")
        result = page.evaluate("""() => {
            const make = (groups, steps=[0,15,5]) => ({
                run: {sessions: ['a','b','c'].map(id=>({id}))},
                points: groups.flatMap(g=>g),
                point_sessions: groups.flatMap((g,i)=>g.map(()=>['a','b','c'][i])),
                trajectory: {view:'all', resume_smoothing:true, coordinate_axis:'train/global_step',
                    boundaries: steps.map((step,i)=>({id:['a','b','c'][i], step}))}
            });
            const s = make([[[0,10],[10,20],[20,1000]], [[15,30],[25,40]], [[5,50]]]);
            const before = JSON.stringify(s);
            const read = (s, alpha=.5, log=false, axis='train/global_step') => displayCurves(s,axis,alpha,log).map(c=>({
                ys:c.displayPoints.map(p=>p.y), anchor:c.smoothingAnchor?.x ?? null,
                count:c.displayPoints.length}));
            return {
                branches:read(s), unchanged:before===JSON.stringify(s),
                raw:read(s,0), unsafe:read({...s, trajectory:{...s.trajectory,resume_smoothing:false}}),
                otherAxis:read(s,.5,false,'_runtime'),
                missingBranch:read(make([[[0,10],[10,20],[17,1000]], [], [[18,40]]],[0,15,18])),
                gap:read(make([[[0,10],[10,null]], [[15,30]], []])),
                logGap:read(make([[[0,10],[10,-1]], [[15,30]], []]),.5,true),
                sameCheckpoint:read(make([[[0,10],[10,20]], [[15,30]], [[15,50]]],[0,15,15]))
            };
        }""")
        assert result["unchanged"]
        assert result["branches"] == [
            {"ys": [10, 15, 507.5], "anchor": None, "count": 3},
            {"ys": [22.5, 31.25], "anchor": 10, "count": 2},
            {"ys": [30], "anchor": 0, "count": 1},
        ]
        assert result["raw"][1]["ys"] == [30, 40]
        assert result["raw"][1]["anchor"] is None
        for key in ("unsafe", "otherAxis"):
            assert result[key][1]["ys"] == [30, 35]
            assert result[key][1]["anchor"] is None
        assert result["missingBranch"][1]["ys"] == [27.5]
        for key in ("gap", "logGap"):
            assert result[key][1]["ys"] == [30]
            assert result[key][1]["anchor"] is None
        assert result["sameCheckpoint"][2]["ys"] == [32.5]
        browser.close()
