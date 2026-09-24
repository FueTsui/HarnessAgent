"""品牌资源发现与回退行为的回归测试。"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backend import main
from backend.api import settings


class _FakeDb:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, _model, key):
        value = self.values.get(key)
        return SimpleNamespace(value=value) if value is not None else None


class BrandingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        branding_dir = Path(directory.name)
        (branding_dir / "favicon.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>', encoding="utf-8"
        )
        patcher = patch.object(settings, "BRANDING_DIR", branding_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_frontend_shell_is_rendered_with_current_brand_before_first_paint(self):
        template = (
            "<title>__BRAND_DOCUMENT_TITLE__</title>"
            '<link href="__BRAND_LOGO_URL__">'
            '<h2 id="brand-title">__BRAND_MARKUP__</h2>'
        )
        with TemporaryDirectory() as directory:
            frontend_dir = Path(directory)
            (frontend_dir / "login.html").write_text(template, encoding="utf-8")
            with (
                patch.object(main, "FRONTEND_DIR", frontend_dir),
                patch.object(main, "_docs_branding", return_value=("新品牌<&", "/logo?v=2")),
            ):
                response = main._render_frontend_page("login.html", "登录 - ")

        body = response.body.decode("utf-8")
        self.assertIn("<title>登录 - 新品牌&lt;&amp;</title>", body)
        self.assertIn('href="/logo?v=2"', body)
        self.assertIn('src="/logo?v=2"', body)
        self.assertIn("新品牌&lt;&amp; Logo", body)
        self.assertNotIn("__BRAND_", body)

    def test_directory_branding_is_used_when_uploaded_logo_is_missing(self):
        db = _FakeDb({settings.LOGO_EXT_KEY: ".png"})

        logo = settings._current_logo(db)
        self.assertIsNotNone(logo)
        self.assertIn(logo.name, settings.DEFAULT_LOGO_FILENAMES)
        self.assertFalse(settings._has_custom_logo(db))
        self.assertRegex(settings._logo_url(db), r"^/api/v1/settings/logo\?v=\d+$")

    def test_uploaded_logo_takes_priority_over_directory_branding(self):
        branding_dir = Path("D:/virtual-branding")
        uploaded = branding_dir / "logo.png"
        db = _FakeDb({settings.LOGO_EXT_KEY: ".png"})

        with (
            patch.object(settings, "BRANDING_DIR", branding_dir),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path.name in {"logo.png", "favicon.svg"}),
        ):
                self.assertEqual(settings._current_logo(db), uploaded)
                self.assertTrue(settings._has_custom_logo(db))

    def test_untrusted_extension_cannot_escape_branding_directory(self):
        branding_dir = Path("D:/virtual-branding")
        fallback = branding_dir / "favicon.svg"
        db = _FakeDb({settings.LOGO_EXT_KEY: "/../../outside"})

        with (
            patch.object(settings, "BRANDING_DIR", branding_dir),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path.name == "favicon.svg"),
        ):
                self.assertEqual(settings._current_logo(db), fallback)
                self.assertFalse(settings._has_custom_logo(db))

    def test_versioned_logo_is_immutable_but_unversioned_logo_revalidates(self):
        db = _FakeDb()

        versioned = settings.get_logo(SimpleNamespace(query_params={"v": "1"}), db)
        unversioned = settings.get_logo(SimpleNamespace(query_params={}), db)

        self.assertEqual(
            versioned.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(unversioned.headers["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
