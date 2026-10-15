"""Real-browser QA: seeds a throw-away workspace, starts the app and drives every page with Chrome (Playwright).

    python tools/qa_browser.py [--shots DIR] [--keep]

Requires `playwright` (pip install playwright) and a local Chrome. Exits non-zero on any console error, failed request
or missing element, so it can gate a release.
"""
import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def seed():
    from pyapp import library, versions
    from pyapp.db import db
    import test_knowledge as tk
    atoms = await library.build_atoms(tk.VIDEO, tk.SEGMENTS, [], 120, tk.fake_extractor)
    await db.save_video({**tk.VIDEO, "status": "COMPLETED", "progress": 100, "duration": 120, "trades": 0, "atoms": len(atoms), "atomsVerified": 2, "sourceIds": ["src-demo"], "visualAnalysis": True,
                         "sourceUrl": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "thumbnail": "", "playlistIds": [], "events": 1})
    for n, status in enumerate(("QUEUED", "AWAITING_PERMISSION", "FAILED", "SKIPPED")):
        await db.save_video({"id": f"video-b{n:010d}", "youtubeId": f"b{n:010d}", "title": f"Demo {status.lower()} video", "channel": "Teacher", "status": status, "progress": 0, "duration": 600, "trades": 0,
                             "sourceIds": ["src-demo"], "playlistIds": [], "sourceUrl": f"https://www.youtube.com/watch?v=b{n:010d}", "statusNote": "Needs sign-in" if status == "AWAITING_PERMISSION" else None,
                             "error": "Network timeout" if status == "FAILED" else None})
    await db.save("sources", {"id": "src-demo", "kind": "channel", "url": "https://www.youtube.com/@teacher", "title": "Teacher channel", "channel": "Teacher", "lastSyncedAt": "2026-10-01T00:00:00+00:00",
                              "truncated": False, "discoveredCount": 5})
    await db.save("video_details", {"id": tk.VIDEO["id"], "transcript": tk.SEGMENTS, "events": [{"timestamp": 12, "eventType": "SETUP", "transcriptEvidence": "sweep", "confidence": .9, "detectionSource": "spoken"}],
                                    "observations": [{"timestamp": 12, "summary": {"visualDescription": "EURUSD M5 chart with a marked zone", "symbol": "EURUSD", "timeframe": "M5", "executionVisible": False, "confidence": .8},
                                                      "frames": [], "transcript": tk.SEGMENTS[:2]}]})
    await db.replace_for_video("atoms", tk.VIDEO["id"], atoms)
    await db.save("knowledge", {"id": "kb-1", "videoId": tk.VIDEO["id"], "video": "Sweep entry", "type": "Spoken", "title": "t", "content": tk.SEGMENTS[2]["text"], "time": 40.0,
                                "sourceUrl": "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=40s"})
    from pyapp.config import settings
    settings.GEMINI_API_KEY, settings.MOCK_AI_MODE = "qa-key", False
    await library.rebuild_library(writer=tk.fake_writer, conflicts_fn=tk.no_conflicts, clusters_fn=lambda a: library.make_clusters(a, False))
    settings.GEMINI_API_KEY, settings.MOCK_AI_MODE = None, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", default=str(ROOT / "dist" / "qa"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="tvi-qa-"))
    os.environ.update(STORAGE_PATH=str(tmp), MOCK_AI_MODE="true", ENABLE_WORKER="true", BACKTEST_PROCESSES="true", APP_ENV="development")
    os.environ.pop("ADMIN_PASSWORD", None)
    sys.path.insert(0, str(ROOT))
    asyncio.run(seed())
    sys.path.insert(0, str(ROOT / "tests"))
    from test_lab_api import SPEC, csv_bytes
    csv_path = tmp / "eurusd_m15.csv"
    csv_path.write_bytes(csv_bytes(2500))
    port = free_port()
    problems = []
    log_path = tmp / "server.log"
    log = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env={**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}, stdout=log, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                time.sleep(0.5)
        problems = drive(base, args.shots, csv_path, SPEC)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        out = log_path.read_text(encoding="utf-8", errors="replace")
        # yt-dlp failures from the intentionally unreachable demo videos are expected; anything else is a bug.
        unexpected = [block for block in out.split("Traceback")[1:] if "yt_dlp" not in block and "DownloadError" not in block]
        if unexpected:
            print("SERVER ERRORS:\n" + unexpected[0][-2500:])
            problems = (problems or []) + ["server logged an unexpected traceback"]
    print("\nQA", "FAILED" if problems else "PASSED")
    for p in problems or []:
        print(" -", p)
    sys.exit(1 if problems else 0)


def drive(base, shots, csv_path, spec):
    import json
    from playwright.sync_api import sync_playwright
    Path(shots).mkdir(parents=True, exist_ok=True)
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        # External hosts (fonts, YouTube embeds) are aborted so the run is deterministic and offline-safe.
        page.route(lambda url: not url.startswith(base), lambda route: route.abort())
        page.on("console", lambda m: problems.append(f"console {m.type}: {m.text[:200]} @ {page.url}") if m.type == "error" and "net::ERR" not in m.text and "Failed to load resource" not in m.text else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {str(e)[:200]} @ {page.url}"))
        page.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "favicon" not in r.url and r.url.startswith(base) else None)

        def shot(name):
            page.screenshot(path=str(Path(shots) / f"{name}.png"), full_page=True)

        def visit(path, name, wait="domcontentloaded"):
            page.goto(base + path, wait_until=wait)
            page.wait_for_timeout(900)
            shot(name)

        # --- data, strategy, approval via the API (the same calls the UI makes)
        api = page.request
        r = api.post(base + "/api/market-data/csv", multipart={"file": {"name": "eurusd.csv", "mimeType": "text/csv", "buffer": csv_path.read_bytes()}, "symbol": "EURUSD", "timeframe": "M15", "provider": "QA vendor", "tz": "UTC"})
        assert r.status == 201, r.text()
        dataset = r.json()
        strategy = api.post(base + "/api/library/custom", data={"name": "QA EMA cross"}).json()
        version = api.post(base + "/api/versions", data={"strategyId": strategy["id"], "spec": spec}).json()

        for path, name in (("/", "dashboard"), ("/sources", "sources"), ("/videos", "videos"), ("/videos/video-aaaaaaaaaaa", "video_detail"), ("/library", "library"), ("/qa", "qa"),
                           ("/editor?strategy=" + strategy["id"] + "&version=" + version["id"], "editor_draft"), ("/backtest", "backtest_empty"), ("/integrations", "integrations"), ("/compare", "compare"), ("/charts", "charts")):
            visit(path, name)

        # --- sources: expand the demo channel
        page.goto(base + "/sources", wait_until="domcontentloaded")
        page.click("[data-toggle]")
        page.wait_for_selector("text=Demo awaiting_permission video")
        shot("sources_expanded")
        assert "awaiting permission" in page.inner_text("body").lower()

        # --- library -> strategy -> evidence drawer
        page.goto(base + "/library", wait_until="domcontentloaded")
        page.wait_for_selector("text=Liquidity Sweep Entry")
        page.click("text=Liquidity Sweep Entry")
        page.wait_for_selector(".manual-section")
        shot("strategy_manual")
        assert "Not stated in the analysed sources" in page.inner_text("body")
        page.click(".cite >> nth=0")
        page.wait_for_selector(".drawer.open iframe")
        page.wait_for_timeout(300)
        shot("evidence_drawer")
        page.keyboard.press("Escape")

        # --- Q&A (extractive without AI)
        page.goto(base + "/qa", wait_until="domcontentloaded")
        page.fill("#question", "stop loss kahan rakhna hai?")
        page.click("#askBtn")
        page.wait_for_selector(".bubble.ai:not(#pending)")
        page.wait_for_timeout(500)
        shot("qa_answer")
        assert "swing low" in page.inner_text("#thread").lower()

        # --- editor: validate, approve
        page.goto(base + f"/editor?strategy={strategy['id']}&version={version['id']}", wait_until="domcontentloaded")
        page.wait_for_selector("#approve")      # rendered once the version has loaded into the editor
        page.click("#validate")
        page.wait_for_selector("#vresult .banner.ok")
        page.once("dialog", lambda d: d.accept())
        page.click("#approve")
        page.wait_for_selector("text=approved by")
        shot("editor_approved")

        # --- backtest through the real form
        page.goto(base + "/backtest?version=" + version["id"], wait_until="domcontentloaded")
        shot("backtest_form")
        page.click("#quick [data-m='all']")
        page.fill("#audit", "3")
        page.click("#runBtn")
        page.wait_for_url("**/runs/*", timeout=240000)
        page.wait_for_selector("#cEquity svg path", timeout=15000)
        page.wait_for_timeout(600)
        shot("run_overview")
        run_url = page.url
        assert page.locator("#kpis .kpi").count() >= 8
        page.click("[data-tab=journal]")
        page.wait_for_selector("#jTable tbody tr")
        shot("run_journal")
        page.click("#jTable tbody tr >> nth=0")
        page.wait_for_selector(".drawer.open svg")
        page.wait_for_timeout(300)
        shot("trade_drawer")
        page.keyboard.press("Escape")
        for tab in ("segments", "validation", "pine", "export"):
            page.click(f"[data-tab={tab}]")
            page.wait_for_timeout(500)
            shot("run_" + tab)
        assert "identical" in page.inner_text("#valBody") or "identical results" in page.inner_text("#valBody")
        with page.expect_download() as dl:
            page.click("text=Excel (.xlsx)")
        assert dl.value.suggested_filename.endswith(".xlsx")
        page.click("[data-tab=pine]")
        page.click("#genPine")
        page.wait_for_selector(".drawer.open pre.code-block")
        shot("pine_drawer")
        page.keyboard.press("Escape")

        visit("/journal", "journal_redirect")
        visit("/analytics", "analytics_redirect")
        visit("/runs", "runs")
        # --- comparison
        page.goto(base + "/compare", wait_until="domcontentloaded")
        # a second run with wider costs so there is something to compare
        r2 = api.post(base + "/api/runs", data={"versionId": version["id"], "config": {"dataset_id": dataset["id"], "symbol": "EURUSD", "costs": {"spread": 0.0004}, "causality_samples": 0}, "label": "wide spread"})
        job = r2.json()
        for _ in range(240):
            state = api.get(base + f"/api/jobs/{job['id']}").json()
            if state["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(1)
        assert state["status"] == "COMPLETED", state
        page.goto(base + "/compare", wait_until="domcontentloaded")
        page.locator("#picker input[type=checkbox]").nth(0).check()
        page.locator("#picker input[type=checkbox]").nth(1).check()
        page.click("#go")
        page.wait_for_selector("#cmpChart svg path")
        shot("compare_result")

        # --- CRUD through the UI: rename run, notes, rename/delete own strategy, delete a video
        page.goto(run_url, wait_until="domcontentloaded")
        page.wait_for_selector("#kpis .kpi")
        page.on("dialog", lambda d: d.accept("QA renamed run"))
        page.click("#renameRun")
        page.wait_for_selector("text=QA renamed run")
        page.goto(base + "/library/" + strategy["id"], wait_until="domcontentloaded")
        page.fill("#notes", "my private notes")
        page.click("#saveNotes")
        page.wait_for_selector("text=Notes saved")
        page.click("#rename")
        page.wait_for_selector("text=QA renamed run")
        page.goto(base + "/videos", wait_until="domcontentloaded")
        page.fill("input[name=q]", "queued")
        page.press("input[name=q]", "Enter")
        page.wait_for_selector("text=Demo queued video")
        page.click("[data-del] >> nth=0")
        page.wait_for_selector("text=No videos match")
        shot("videos_after_delete")

        # --- responsive: phone width
        page.set_viewport_size({"width": 390, "height": 844})
        for path, name in (("/", "m_dashboard"), (run_url.replace(base, ""), "m_run"), ("/library", "m_library"), ("/qa", "m_qa")):
            visit(path, name)
            overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if overflow > 4:
                problems.append(f"horizontal overflow {overflow}px on {name} at 390px")
        # --- dark mode
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(base + run_url.replace(base, ""), wait_until="domcontentloaded")
        page.click("#themeToggle")
        page.wait_for_timeout(700)
        shot("run_dark")
        browser.close()
    return problems


if __name__ == "__main__":
    main()
