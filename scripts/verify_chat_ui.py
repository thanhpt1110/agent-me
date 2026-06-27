#!/usr/bin/env python
"""Headless browser behavior test for the dashboard /chat UI.

Drives a real browser through the full flow — Alpine init, the passcode gate,
unlock, New chat, type, Send, and an assistant reply — so a "the API returns
200" check can never again hide a dead UI (e.g. a broken x-data attribute).

Run:  uv run python scripts/verify_chat_ui.py [base_url]
Needs the chromium browser + its system libs:
  uv run python -m playwright install chromium
  sudo uv run python -m playwright install-deps chromium   # one-time, needs root
Reads the passcode from DASHBOARD_OPERATOR_ACTION_CODE (skips the gate if unset).
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
    passcode = os.environ.get("DASHBOARD_OPERATOR_ACTION_CODE", "")
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/chat", wait_until="load", timeout=30000)
        page.wait_for_timeout(2000)

        check("Alpine.js initialised", page.evaluate("() => typeof window.Alpine !== 'undefined'"))
        check("no page JS errors", not errors)

        if passcode:
            gate = page.locator("#chat-passcode")
            check("passcode gate visible", gate.is_visible())
            gate.fill(passcode)
            page.get_by_role("button", name="Unlock").click()
            page.wait_for_timeout(1500)
            check("gate dismissed after unlock", not page.locator("#chat-passcode").is_visible())

        page.get_by_role("button", name="+ New").click()
        page.wait_for_timeout(1500)
        composer = page.locator("textarea")
        check("composer enabled after New", composer.is_enabled())
        composer.fill("Reply with exactly the single word PONG. Do not use tools.")
        page.get_by_role("button", name="Send").click()
        page.wait_for_selector("text=PONG", timeout=90000)
        check("assistant replied (PONG)", page.locator("text=PONG").count() > 0)

        browser.close()

    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
