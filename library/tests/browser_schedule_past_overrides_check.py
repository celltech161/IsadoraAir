"""Real-browser check of the Schedule page's Past Overrides list. Not a Django test module (no ``test_``
prefix, no Django import): it runs on a host with Playwright's Chromium, OUTSIDE the validation sandbox
(where Playwright cannot launch a browser), against the page the sandbox rendered with
library.tests.render_schedule_console_artifact:

    run_with_validation_service.sh <python> manage.py test library.tests.render_schedule_console_artifact
    # extract SCHEDULE_PAGE from its stdout into <dir>/schedule_page.html
    <python with playwright> -I library/tests/browser_schedule_past_overrides_check.py <dir>

Every request is intercepted by a scripted schedule API (profiles, date view, Hour Detail, Past
Overrides, DELETE). Responses can be held and released out of order to prove that a late answer for a
previous profile, filter or mode never renders. Exit status 0 only if every check passes.
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://station.test"
results, failures = [], []
P1, P2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    if not ok:
        failures.append(f"{name}: {detail}")
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""))


class Api:
    def __init__(self):
        self.profiles = [
            {"uuid": P1, "name": "Default Schedule", "is_archived": False, "is_active": True, "is_default": True,
             "can_delete": False, "sort_order": 0, "description": "", "block_count": 4, "playlist_log_count": 0},
            {"uuid": P2, "name": "Old Season", "is_archived": True, "is_active": False, "is_default": False,
             "can_delete": False, "sort_order": 1, "description": "", "block_count": 1, "playlist_log_count": 0},
        ]
        self.rows = {
            P1: [
                {"id": 1, "date": "2027-03-09", "time": "06:00", "kind": "rotation", "name": "Jeff Memorial", "cid": 7},
                {"id": 2, "date": "2027-03-09", "time": "06:37", "kind": "rotation", "name": "Rock Block", "cid": 3},
                {"id": 3, "date": "2027-02-01", "time": "14:00", "kind": "playlist", "name": "Special", "cid": 2},
                {"id": 4, "date": "2027-01-15", "time": "09:00", "kind": "rotation", "name": "Jeff Memorial", "cid": 7},
            ],
            P2: [{"id": 9, "date": "2026-12-25", "time": "10:00", "kind": "rotation", "name": "Xmas", "cid": 5}],
        }
        self.calls, self.deletes, self.held, self.hold_past = [], [], [], False
        self.fail_past = 0   # answer the next N list requests like production Django's HTML 500 page

    def past(self, query):
        profile = query.get("profile", [P1])[0]
        q = query.get("q", [""])[0].lower()
        before = query.get("before", ["9999-12-31"])[0]
        limit = int(query.get("limit", ["30"])[0])
        rows = [r for r in self.rows.get(profile, []) if r["date"] < before and q in r["name"].lower()]
        dates = sorted({r["date"] for r in rows}, reverse=True)
        more = len(dates) > limit
        dates = dates[:limit]
        groups = [{"date": d, "overrides": [
            {"id": r["id"], "start_time": r["time"], "minute": int(r["time"][3:]), "specific_date": d,
             "content_kind": r["kind"], "content_id": r["cid"], "content_name": r["name"]}
            for r in sorted((r for r in rows if r["date"] == d), key=lambda r: r["time"])]} for d in dates]
        archived = next(p for p in self.profiles if p["uuid"] == profile)["is_archived"]
        return {"profile_uuid": profile, "is_archived": archived, "today": "2027-03-10", "query": q,
                "dates": groups, "has_more": more, "next_before": dates[-1] if more else None}

    def date_view(self, query):
        cells = [{"hour": h, "origin": "none", "explicit_block_id": None, "inherited_block_id": None,
                  "effective_block": None, "detail_count": 0} for h in range(24)]
        return {"profile_uuid": query.get("profile", [P1])[0], "date": query["date"][0], "weekday": 0, "cells": cells}


class Page:
    def __init__(self, browser, html, api, *, width=1280):
        self.api = api
        self.context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 900})
        self.page = self.context.new_page()
        self.errors, self.confirms = [], []
        self.accept_confirm = True
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", self._dialog)
        self.page.route("**/*", lambda route, request: self._route(route, request, html))
        self.page.goto(f"{BASE}/schedule/")
        self.settle()

    def _dialog(self, dialog):
        self.confirms.append(dialog.message)
        dialog.accept() if self.accept_confirm else dialog.dismiss()

    def _json(self, route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _route(self, route, request, html):
        parsed = urlparse(request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.api.calls.append((request.method, path, parsed.query))
        if path == "/schedule/":
            return route.fulfill(status=200, content_type="text/html", body=html)
        if path == "/api/schedule/profiles/":
            return self._json(route, {"active_profile_uuid": P1, "default_profile_uuid": P1,
                                      "profiles": self.api.profiles})
        if path == "/api/schedule/past-overrides/":
            if self.api.hold_past:
                self.api.held.append((route, query))
                return None
            if self.api.fail_past:
                self.api.fail_past -= 1
                return self._server_error(route)
            return self._json(route, self.api.past(query))
        if path == "/api/schedule/hour-detail/":
            hour = int(query["hour"][0])
            return self._json(route, {"profile_uuid": query["profile"][0], "is_archived": False, "mode": "date",
                                      "day_of_week": None, "date": query["date"][0], "hour": hour, "has_base": False,
                                      "is_partial_hour": False, "takeover_minute": None, "minutes": [], "segments": []})
        if path.startswith("/api/schedule/") and request.method == "DELETE":
            block = int(path.rstrip("/").split("/")[-1])
            self.api.deletes.append((block, query.get("profile", [""])[0], query.get("date", [""])[0]))
            for rows in self.api.rows.values():
                rows[:] = [r for r in rows if r["id"] != block]
            return self._json(route, {"ok": True, "deleted": True})
        if path == "/api/schedule/":
            if "date" in query:
                return self._json(route, self.api.date_view(query))
            return self._json(route, {"blocks": []})
        return route.fulfill(status=404, body="")

    @staticmethod
    def _server_error(route):
        # What DEBUG=False Django sends for an unhandled exception: HTML, no JSON, no traceback.
        route.fulfill(status=500, content_type="text/html", body="\n<!doctype html>\n<html lang=\"en\">\n<head>\n"
                      "  <title>Server Error (500)</title>\n</head>\n<body>\n  <h1>Server Error (500)</h1><p></p>\n"
                      "</body>\n</html>\n")

    def release(self, index=0, *, fail=False):
        route, query = self.api.held.pop(index)
        self._server_error(route) if fail else self._json(route, self.api.past(query))
        self.settle()

    def list_text(self):
        return self.js("document.getElementById('pastOverridesList').textContent")

    def api_error(self):
        return self.js("document.getElementById('apiError').textContent")

    def settle(self, ms=250):
        self.page.wait_for_timeout(ms)

    def js(self, expr):
        return self.page.evaluate(expr)

    def dates_shown(self):
        return self.js("[...document.querySelectorAll('#pastOverridesList .past-override-date')].map(s => s.dataset.date)")

    def visible(self, selector):
        return self.page.is_visible(selector)

    def close(self):
        for route, _query in self.api.held:
            try:
                route.abort()
            except Exception:
                pass
        self.context.close()


def scenario_mode_and_layout(browser, html):
    p = Page(browser, html, Api())
    check("weekly mode hides the Past Overrides control", not p.visible("#pastOverridesButton"))
    p.page.click("#dateModeButton"); p.settle()
    check("date mode shows the control after Today",
          p.js("(() => { const c = [...document.getElementById('dateControls').children].map(e => e.id || e.tagName);"
               " return c.indexOf('pastOverridesButton') === c.indexOf('todayButton') + 1; })()"))
    check("the date navigation arrows, date picker and Today remain",
          all(p.visible(s) for s in ("#previousDateButton", "#overrideDate", "#nextDateButton", "#todayButton")))
    p.page.click("#pastOverridesButton"); p.settle()
    check("the list replaces the 24-hour day view", p.visible("#pastOverrides") and not p.visible("#dateHourList")
          and not p.visible("#dateSchedule .date-schedule-head"))
    check("the control reports its pressed state", p.js("pastOverridesButton.getAttribute('aria-pressed')") == "true")
    check("dates newest first, grouped", p.dates_shown() == ["2027-03-09", "2027-02-01", "2027-01-15"], p.dates_shown())
    rows = p.js("[...document.querySelectorAll('.past-override-date[data-date=\"2027-03-09\"] .past-override-row')]"
                ".map(r => [r.querySelector('.past-override-time').textContent, r.querySelector('.past-override-kind').textContent,"
                " r.querySelector('.past-override-name').textContent])")
    check("every explicit transition with time, type and name (including :37)",
          rows == [["06:00", "Rotation", "Jeff Memorial"], ["06:37", "Rotation", "Rock Block"]], rows)
    check("playlist rows are labeled", "Playlist" in p.js(
        "document.querySelector('.past-override-date[data-date=\"2027-02-01\"] .past-override-kind').textContent"))
    check("the page made the profile-scoped list request",
          any(path == "/api/schedule/past-overrides/" and f"profile={P1}" in q for _m, path, q in p.api.calls))
    p.page.click("#weeklyModeButton"); p.settle()
    check("Weekly Schedule closes the list", not p.visible("#pastOverrides") and not p.visible("#pastOverridesButton"))
    p.page.click("#dateModeButton"); p.settle()
    check("returning to Date Override shows the day view, not stale results",
          p.visible("#dateHourList") and not p.visible("#pastOverrides") and p.dates_shown() == [])
    p.page.click("#pastOverridesButton"); p.settle()
    p.page.click("#todayButton"); p.settle()
    check("choosing a date (Today) returns to the 24-hour view", p.visible("#dateHourList") and not p.visible("#pastOverrides"))
    check("no page errors", not p.errors, p.errors)
    p.close()

    p = Page(browser, html, Api(), width=390)
    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    overflow = p.js("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("phone width: no horizontal page overflow", overflow <= 1, overflow)
    check("phone width: rows still show the Remove action", p.visible(".past-override-row button.danger"))
    p.close()


def scenario_stale_responses(browser, html):
    api = Api()
    p = Page(browser, html, api)
    p.page.click("#dateModeButton"); p.settle()
    api.hold_past = True
    p.page.click("#pastOverridesButton"); p.settle()              # P1 request held
    p.page.select_option("#profileSelect", P2); p.settle()        # P2 request held
    api.hold_past = False
    check("two requests in flight", len(api.held) == 2, len(api.held))
    p.release(1)                                                  # newer (P2) answers first
    p.release(0)                                                  # the old P1 answer arrives late
    check("a late answer for the previous profile never renders", p.dates_shown() == ["2026-12-25"], p.dates_shown())
    check("archived profile: Remove is disabled", p.js("document.querySelector('.past-override-row button.danger').disabled"))

    api.hold_past = True
    p.page.select_option("#profileSelect", P1); p.settle()
    p.page.click("#weeklyModeButton"); p.settle()
    api.hold_past = False
    p.release(0)
    check("a late answer after switching to Weekly never renders", p.dates_shown() == [] and not p.visible("#pastOverrides"))

    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    api.hold_past = True
    p.page.fill("#pastOverridesFilter", "jeff"); p.settle(500)    # debounced filter request held
    p.page.fill("#pastOverridesFilter", "special"); p.settle(500)
    api.hold_past = False
    p.release(1)
    p.release(0)                                                  # the older "jeff" answer is late
    check("the newest filter wins", p.dates_shown() == ["2027-02-01"], p.dates_shown())
    check("the filter is sent to the server", any("q=special" in q for _m, path, q in api.calls
                                                  if path == "/api/schedule/past-overrides/"))
    check("no page errors", not p.errors, p.errors)
    p.close()


def scenario_show_date_and_remove(browser, html):
    api = Api()
    p = Page(browser, html, api)
    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    p.page.click(".past-override-row[data-block-id='2'] button:not(.danger)"); p.settle(500)
    check("inspect returns to the day view for that date", p.js("overrideDate.value") == "2027-03-09"
          and p.visible("#dateHourList") and not p.visible("#pastOverrides"))
    check("the transition's hour is highlighted",
          p.js("[...dateHourList.children].findIndex(r => r.classList.contains('focus-hour'))") == 6)
    check("a minute transition opens Hour Detail for its hour",
          p.visible("#hourDetail") and "2027-03-09 6:00a" in (p.js("document.getElementById('hourDetailTitle').textContent") or ""), p.js("document.getElementById('hourDetailTitle').textContent"))

    p.page.click("#pastOverridesButton"); p.settle()
    p.accept_confirm = False
    p.page.click(".past-override-row[data-block-id='4'] button.danger"); p.settle()
    check("cancelling the confirmation deletes nothing", api.deletes == [] and len(p.confirms) == 1)
    check("the confirmation names the date, time and content",
          "9:00" in p.confirms[0] and "Jeff Memorial" in p.confirms[0] and "Default Schedule" in p.confirms[0], p.confirms)
    p.accept_confirm = True
    p.page.click(".past-override-row[data-block-id='4'] button.danger"); p.settle(500)
    check("removal uses the scoped existing DELETE endpoint", api.deletes == [(4, P1, "2027-01-15")], api.deletes)
    check("the list refreshes and a date with no overrides left disappears",
          p.dates_shown() == ["2027-03-09", "2027-02-01"], p.dates_shown())
    p.page.click(".past-override-row[data-block-id='1'] button.danger"); p.settle(500)
    rows = p.js("[...document.querySelectorAll('.past-override-date[data-date=\"2027-03-09\"] .past-override-row')].map(r => r.dataset.blockId)")
    check("removing one transition leaves its sibling on the same date", rows == ["2"], rows)
    check("no page errors", not p.errors, p.errors)
    p.close()


def scenario_paging(browser, html):
    api = Api()
    api.rows[P1] = [{"id": 100 + n, "date": f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}", "time": "08:00",
                     "kind": "rotation", "name": "Jeff Memorial", "cid": 7} for n in range(35)]
    p = Page(browser, html, api)
    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    first = len(p.dates_shown())
    check("first page holds 30 dates and offers older ones", first == 30 and p.visible("#pastOverridesMore"), first)
    p.page.click("#pastOverridesMore"); p.settle()
    check("Load older dates appends the rest", len(p.dates_shown()) == 35 and not p.visible("#pastOverridesMore"))
    check("the second page used the server cursor", any("before=" in q for _m, path, q in api.calls
                                                        if path == "/api/schedule/past-overrides/"))
    check("no page errors", not p.errors, p.errors)
    p.close()


def scenario_failure_and_empty(browser, html):
    """r0141 production defect: every list request answered HTTP 500. The page must say so truthfully,
    never keep or render a stale list, recover on a later success and tell "nothing found" apart from
    "could not load"."""
    api = Api()
    p = Page(browser, html, api)
    p.page.click("#dateModeButton"); p.settle()
    api.fail_past = 1
    p.page.click("#pastOverridesButton"); p.settle()
    check("a 500 on first load says the list could not be loaded",
          p.list_text() == "Past overrides could not be loaded." and p.dates_shown() == [], p.list_text())
    check("the error banner shows the status only, no server page or details",
          p.api_error() == "Request failed (500)", p.api_error())
    check("a failed load offers no Load older dates", not p.visible("#pastOverridesMore"))
    check("a failed load is not left busy", p.js("pastOverridesList.getAttribute('aria-busy')") == "false")

    p.page.click("#pastOverridesClose"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    check("reopening after the failure renders the list",
          p.dates_shown() == ["2027-03-09", "2027-02-01", "2027-01-15"] and "could not be loaded" not in p.list_text(),
          p.dates_shown())

    api.fail_past = 1
    p.page.fill("#pastOverridesFilter", "jeff"); p.settle(500)
    check("a failed filter request leaves no stale rows from the previous list",
          p.dates_shown() == [] and p.list_text() == "Past overrides could not be loaded.", p.dates_shown())
    check("no Remove control survives the failure", p.js("document.querySelectorAll('#pastOverridesList button').length") == 0)
    p.page.fill("#pastOverridesFilter", "special"); p.settle(500)
    check("the next successful filter request renders its rows", p.dates_shown() == ["2027-02-01"], p.dates_shown())
    p.page.fill("#pastOverridesFilter", "no-such-name"); p.settle(500)
    check("an empty filter result is a stated empty result, not a failure",
          p.list_text() == "No past overrides in this profile match that name.", p.list_text())

    api.hold_past = True
    p.page.fill("#pastOverridesFilter", ""); p.settle(500)            # held: will fail late
    p.page.select_option("#profileSelect", P2); p.settle()            # held: will succeed
    api.hold_past = False
    p.release(1)
    p.release(0, fail=True)
    check("a late 500 for an abandoned request neither clears nor replaces the current list",
          p.dates_shown() == ["2026-12-25"] and "could not be loaded" not in p.list_text(), p.dates_shown())
    check("no page errors", not p.errors, p.errors)
    p.close()

    empty = Api()
    empty.rows[P1] = []
    p = Page(browser, html, empty)
    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    check("a profile with no past overrides says so, with no error",
          p.list_text() == "This profile has no date overrides before today." and p.api_error() == "", p.list_text())
    check("no page errors", not p.errors, p.errors)
    p.close()

    api = Api()
    api.rows[P1] = [{"id": 100 + n, "date": f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}", "time": "08:00",
                     "kind": "rotation", "name": "Jeff Memorial", "cid": 7} for n in range(35)]
    p = Page(browser, html, api)
    p.page.click("#dateModeButton"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    api.fail_past = 1
    p.page.click("#pastOverridesMore"); p.settle()
    check("a failed Load older dates keeps the page already shown and reports the error",
          len(p.dates_shown()) == 30 and p.api_error() == "Request failed (500)", (len(p.dates_shown()), p.api_error()))
    p.page.click("#pastOverridesClose"); p.settle()
    p.page.click("#pastOverridesButton"); p.settle()
    p.page.click("#pastOverridesMore"); p.settle()
    check("after reopening, Load older dates works", len(p.dates_shown()) == 35, len(p.dates_shown()))
    p.page.click(".past-override-date[data-date='2026-01-01'] h3 button"); p.settle(500)
    check("Show date from an older page returns to that date",
          p.js("overrideDate.value") == "2026-01-01" and p.visible("#dateHourList"), p.js("overrideDate.value"))
    check("no page errors", not p.errors, p.errors)
    p.close()


def main():
    render = Path(sys.argv[1])
    html = (render / "schedule_page.html").read_text()
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scenario in (scenario_mode_and_layout, scenario_stale_responses, scenario_show_date_and_remove,
                         scenario_paging, scenario_failure_and_empty):
            if only and only not in scenario.__name__:
                continue
            try:
                scenario(browser, html)
            except Exception as exc:          # a crashed scenario is a failure, never a silent pass
                check(f"{scenario.__name__} completed", False, repr(exc))
        print(f"browser {browser.version}")
        browser.close()
    print(f"{sum(ok for _n, ok in results)}/{len(results)} checks passed in {time.time() - started:.1f}s")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
