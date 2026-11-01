"""Offline tests for the opt-in browser solver's graceful fallback paths."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import browser_solver


def fake_selenium_modules(chrome_factory):
    webdriver = types.ModuleType("selenium.webdriver")

    class ChromeOptions:
        def __init__(self):
            self.arguments = []
            self.binary_location = None

        def add_argument(self, arg):
            self.arguments.append(arg)

    webdriver.ChromeOptions = ChromeOptions
    webdriver.Chrome = chrome_factory

    service = types.ModuleType("selenium.webdriver.chrome.service")

    class Service:
        def __init__(self, executable_path=None):
            self.executable_path = executable_path

    service.Service = Service
    ui = types.ModuleType("selenium.webdriver.support.ui")

    class WebDriverWait:
        def __init__(self, driver, timeout, poll_frequency=0.5):
            self.driver = driver

        def until(self, condition):
            return condition(self.driver)

    ui.WebDriverWait = WebDriverWait
    root = types.ModuleType("selenium")
    root.webdriver = webdriver
    return {
        "selenium": root,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.chrome": types.ModuleType("selenium.webdriver.chrome"),
        "selenium.webdriver.chrome.service": service,
        "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
        "selenium.webdriver.support.ui": ui,
    }


class BrowserSolverTests(unittest.TestCase):
    def test_missing_binaries_falls_back_without_importing_selenium(self):
        with mock.patch.object(browser_solver, "_find_binary", return_value=None), \
             self.assertLogs("bursa.browser", level="WARNING") as logs:
            self.assertIsNone(browser_solver.solve_challenge())
        self.assertTrue(any("Chromium or chromedriver not found" in m for m in logs.output))

    def test_missing_selenium_falls_back(self):
        with mock.patch.object(browser_solver, "_find_binary", return_value="/fake/bin"), \
             mock.patch.dict(sys.modules, {"selenium": None}), \
             self.assertLogs("bursa.browser", level="WARNING") as logs:
            self.assertIsNone(browser_solver.solve_challenge())
        self.assertTrue(any("selenium not installed" in m for m in logs.output))

    def test_budget_exhausted_skips_browser(self):
        modules = fake_selenium_modules(lambda **kw: self.fail("should not launch"))
        with mock.patch.object(browser_solver, "_find_binary", return_value="/fake/bin"), \
             mock.patch.dict(sys.modules, modules), \
             self.assertLogs("bursa.browser", level="WARNING") as logs:
            self.assertIsNone(browser_solver.solve_challenge(reserve=lambda: False))
        self.assertTrue(any("request budget exhausted" in m for m in logs.output))

    def test_success_returns_cookie_and_user_agent(self):
        class Driver:
            def __init__(self):
                self.quit_called = False

            def set_page_load_timeout(self, timeout):
                pass

            def get(self, url):
                self.url = url

            def get_cookies(self):
                return [{"name": "cf_clearance", "value": "token"}]

            @property
            def title(self):
                return "application/json"

            def execute_script(self, script):
                return "TestChrome/1.0"

            def quit(self):
                self.quit_called = True

        driver = Driver()
        modules = fake_selenium_modules(lambda **kw: driver)
        reserved = []
        with mock.patch.object(browser_solver, "_find_binary", return_value="/fake/bin"), \
             mock.patch.dict(sys.modules, modules):
            result = browser_solver.solve_challenge(reserve=lambda: reserved.append(1) or True)
        self.assertEqual(result["cookies"]["cf_clearance"], "token")
        self.assertEqual(result["user_agent"], "TestChrome/1.0")
        self.assertEqual(reserved, [1])
        self.assertTrue(driver.quit_called)

    def test_driver_exception_falls_back_and_quits(self):
        class Driver:
            def __init__(self):
                self.quit_called = False

            def set_page_load_timeout(self, timeout):
                raise RuntimeError("boom")

            def quit(self):
                self.quit_called = True

        driver = Driver()
        modules = fake_selenium_modules(lambda **kw: driver)
        with mock.patch.object(browser_solver, "_find_binary", return_value="/fake/bin"), \
             mock.patch.dict(sys.modules, modules), \
             self.assertLogs("bursa.browser", level="WARNING") as logs:
            self.assertIsNone(browser_solver.solve_challenge())
        self.assertTrue(driver.quit_called)
        self.assertTrue(any("Browser solver failed" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
