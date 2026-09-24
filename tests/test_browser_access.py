"""Cookie write isolation and visitor control-plane boundaries."""
import unittest

from starlette.requests import Request

from backend.browser_access import cross_origin_cookie_write
from backend.config import settings


def request(path="/api/v1/auth/login", method="POST", **headers):
    return Request({
        "type": "http", "method": method, "scheme": "https",
        "path": path, "query_string": b"", "server": ("agent.example", 443),
        "headers": [(b"host", b"agent.example"), *[(k.encode(), v.encode()) for k, v in headers.items()]],
    })


class BrowserAccessTests(unittest.TestCase):
    def test_login_and_guest_bootstrap_reject_cross_origin_even_without_cookie(self):
        for path in ("/api/v1/auth/login", "/api/v1/auth/guest"):
            for origin in ("https://evil.example", "null", "http://agent.example", "https://agent.example:8443"):
                with self.subTest(path=path, origin=origin):
                    self.assertTrue(cross_origin_cookie_write(request(path, origin=origin)))

    def test_same_origin_default_port_and_machine_requests_remain_usable(self):
        for headers in ({}, {"origin": "https://agent.example"}, {"origin": "https://agent.example:443"}, {"referer": "https://agent.example/models"}):
            self.assertFalse(cross_origin_cookie_write(request(**headers)))

    def test_fetch_metadata_and_referer_cannot_bypass_cookie_protection(self):
        self.assertTrue(cross_origin_cookie_write(request(**{"sec-fetch-site": "cross-site"})))
        self.assertTrue(cross_origin_cookie_write(request(referer="https://evil.example/a")))
        self.assertTrue(cross_origin_cookie_write(request(
            origin="https://evil.example", authorization="Bearer explicit",
            cookie=f"{settings.AUTH_COOKIE_NAME}=sess_ambient",
        )))

    def test_explicit_api_credentials_without_ambient_cookie_keep_cors_contract(self):
        self.assertFalse(cross_origin_cookie_write(request(
            origin="https://client.example", authorization="Bearer explicit",
        )))
        self.assertFalse(cross_origin_cookie_write(request(
            origin="https://client.example", **{"x-api-key": "explicit"},
        )))



if __name__ == "__main__":
    unittest.main()
