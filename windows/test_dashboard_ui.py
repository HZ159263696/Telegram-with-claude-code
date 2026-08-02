#!/usr/bin/env python3
"""AI Bridge Dashboard browser smoke test and screenshot helper."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8888/")
    parser.add_argument("--output", default="dashboard-ui.png")
    parser.add_argument("--mobile", action="store_true")
    parser.add_argument("--expect-v3", action="store_true")
    args = parser.parse_args()

    viewport = {"width": 390, "height": 844} if args.mobile else {"width": 1440, "height": 1000}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport=viewport, device_scale_factor=1)
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        # Dashboard keeps an SSE connection open, so networkidle never settles.
        page.goto(args.url, wait_until="domcontentloaded", timeout=20_000)
        page.locator(".log-panel").wait_for(state="visible", timeout=10_000)
        page.wait_for_timeout(1_000)
        page.screenshot(path=str(output), full_page=True)

        report = {
            "title": page.title(),
            "heading": page.locator("h1").first.text_content(),
            "viewport": viewport,
            "log_panel_count": page.locator(".log-panel").count(),
            "bot_tab_count": page.locator(".bot-tab").count(),
            "console_errors": console_errors,
            "screenshot": str(output),
        }
        if args.expect_v3:
            assert report["title"] == "AI Bridge 3.0", report
            assert "AI Bridge" in (report["heading"] or ""), report
            assert not page.locator("#sheet").is_visible()
            page.locator("#modelTrigger").click()
            assert page.locator("#sheet").is_visible()
            page.locator(".sheet-close").click()
            page.wait_for_timeout(400)
            assert not page.locator("#sheet").is_visible()
            first_settings = page.locator(".settings-grid > details").first
            first_settings.locator("summary").click()
            assert first_settings.get_attribute("open") is not None
            first_settings.locator("summary").click()
            assert first_settings.get_attribute("open") is None
            page.evaluate(
                """() => {
                    clearLog();
                    const add = (k, t) => _appendLog(
                        "@@CLAUDE@@" + JSON.stringify({k, t, ts: "00:00:00"}), false
                    );
                    add("think", "分析请求并确定文件范围");
                    add("tool", "Read(/mnt/d/AI/project/dashboard.py)");
                    add("result", "读取完成，共 120 行");
                    add("text", "界面检查完成，未发现阻塞问题。");
                }"""
            )
            assert page.locator(".run-turn").count() == 1
            assert page.locator(".run-trace").count() == 1
            assert page.locator(".trace-step").count() == 2
            assert page.locator(".trace-result").count() == 1
            assert page.locator(".cl-text").count() == 1
            assert not page.locator(".trace-main").is_visible()
            page.locator(".run-trace > summary").click()
            assert page.locator(".trace-main").is_visible()
            assert not console_errors, report
        print(json.dumps(report, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
