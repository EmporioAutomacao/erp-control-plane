import asyncio
import os
import sys
import unittest

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test.utils import override_settings

try:
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright nao instalado")
class PlaywrightAdminTestCase(StaticLiveServerTestCase):
    browser_viewport = {"width": 1600, "height": 1400}

    @classmethod
    def setUpClass(cls):
        cls._old_allow_async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        cls._old_event_loop_policy = None
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        cls._allowed_hosts_override = override_settings(
            ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"],
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
                },
            },
        )
        cls._allowed_hosts_override.enable()
        super().setUpClass()

        # O loop Selector criado durante a inicializacao do live server nao
        # suporta subprocessos no Windows. Playwright precisa do Proactor para
        # iniciar o driver Node/Chromium.
        if sys.platform == "win32":
            cls._old_event_loop_policy = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        cls._playwright = sync_playwright().start()
        cls.browser = cls._playwright.chromium.launch(headless=True)
        cls.context = cls.browser.new_context(viewport=cls.browser_viewport)
        cls.page = cls.context.new_page()
        cls.page.set_default_timeout(20000)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.context.close()
            cls.browser.close()
            cls._playwright.stop()
        finally:
            if cls._old_event_loop_policy is not None:
                asyncio.set_event_loop_policy(cls._old_event_loop_policy)
            super().tearDownClass()
            cls._allowed_hosts_override.disable()
            if cls._old_allow_async_unsafe is None:
                os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
            else:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = cls._old_allow_async_unsafe

    def autenticar_admin(self, user):
        self.client.force_login(user)
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME]
        self.page.goto(f"{self.live_server_url}/admin/")
        self.context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": session_cookie.value,
                    "url": self.live_server_url,
                }
            ]
        )
