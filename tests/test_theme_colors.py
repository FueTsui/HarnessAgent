"""Theme colors stay readable and server-confirmed choices survive navigation."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ThemeColorTests(unittest.TestCase):
    def run_js(self, scenario):
        source = (ROOT / "frontend/static/common.js").read_text(encoding="utf-8")
        source = source[source.index("const Theme ="):source.index("\nTheme.apply();")]
        prelude = r'''
const assert = require("node:assert/strict");
const storage = new Map();
const properties = {};
let systemDark = false;
global.localStorage = {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)};
global.document = {documentElement: {dataset: {}, style: {setProperty: (key, value) => properties[key] = value}}, querySelectorAll: () => []};
global.matchMedia = () => ({matches: systemDark});
global.showToast = () => {};
function ratio(a, b) {
  const luminance = color => {
    const rgb = color.slice(1).match(/../g).map(c => parseInt(c, 16) / 255)
      .map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  };
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
'''
        result = subprocess.run(["node", "-"], input=prelude + source + scenario,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_all_presets_and_extreme_custom_colors_meet_contrast(self):
        self.run_js(r'''
for (const dark of [false, true]) {
  for (const id of Theme.palette.map(p => p.id)) {
    for (const custom of ["#ffffff", "#000000", "#ffff00", "#00ff00", "#0000ff", "#ff00ff", "#123abc"]) {
      const colors = Theme.colors(id, custom, dark);
      const surfaces = dark ? ["#212121", "#2b2b2b", "#303030", "#393939", "#404040"]
        : ["#ffffff", "#f5f5f5", "#ebebeb", "#e8e8e8"];
      surfaces.push(colors["--surface-active"]);
      for (const surface of surfaces) assert.ok(ratio(colors["--primary"], surface) >= 4.5, `${id} ${custom} ${dark} ${surface}`);
      assert.ok(ratio(colors["--primary"], colors["--accent-contrast"]) >= 4.5);
      assert.ok(ratio(colors["--primary-hover"], colors["--accent-contrast"]) >= 4.5);
      if (id === "custom") assert.equal(colors["--theme-swatch"], custom);
    }
  }
}
''')

    def test_invalid_values_cannot_become_css_or_persisted_state(self):
        self.run_js(r'''
Theme.setAccent("blue", "#123456");
const before = {...properties};
for (const color of ["red", "#123", "#12345678", "#12345g", "#123456;display:none", null, 123456]) {
  assert.throws(() => Theme.setAccent("custom", color));
  assert.deepEqual(properties, before);
  assert.equal(storage.get("gca_theme_color"), "blue");
}
assert.throws(() => Theme.colors("url(javascript:alert(1))", "#123456"));
''')

    def test_saved_color_survives_appearance_changes_and_resets_for_another_account(self):
        self.run_js(r'''
Theme.usePreferences({theme: "light", theme_color: "custom", custom_color: "#AB12EF"});
assert.equal(storage.get("gca_custom_color"), "#ab12ef");
assert.equal(properties["--theme-swatch"], "#ab12ef");
const light = properties["--primary"];
Theme.toggle();
assert.equal(document.documentElement.dataset.theme, "dark");
assert.notEqual(properties["--primary"], light);
assert.equal(properties["--theme-swatch"], "#ab12ef");
Theme.usePreferences({theme: "system"});
assert.equal(storage.get("gca_theme_color"), "default");
assert.equal(properties["--theme-swatch"], "#3b82f6");
assert.equal(properties["--primary"], Theme.colors("blue")["--primary"]);
systemDark = true;
Theme.apply();
assert.equal(document.documentElement.dataset.theme, "dark");
assert.equal(properties["--primary"], Theme.colors("blue", "#8b5cf6", true)["--primary"]);
''')

    def test_new_and_legacy_accounts_default_to_blue(self):
        self.run_js(r'''
Theme.apply();
assert.equal(properties["--theme-swatch"], "#3b82f6");
for (const dark of [false, true]) {
  assert.deepEqual(Theme.colors("default", "#123456", dark), Theme.colors("blue", "#123456", dark));
}
Theme.usePreferences({theme: "light", theme_color: "purple", custom_color: "#123456"});
assert.equal(properties["--theme-swatch"], "#8b5cf6");
''')

    def test_preview_is_pure_and_does_not_overwrite_saved_choice(self):
        self.run_js(r'''
Theme.setAccent("green", "#123456");
const before = {...properties};
const cached = [...storage.entries()];
const preview = Theme.colors("purple", "#123456", false);
assert.notEqual(preview["--primary"], properties["--primary"]);
assert.deepEqual(properties, before);
assert.deepEqual([...storage.entries()], cached);
''')


if __name__ == "__main__":
    unittest.main()
