"""Guards on the phone experience.

The app is used almost entirely on a phone, and the things that break that are
silent: a manifest icon whose path no longer exists still parses as valid JSON,
and the browser simply declines to offer installation with no error anywhere.
"""

from __future__ import annotations

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def on_disk(url: str) -> pathlib.Path:
    """Resolve a /static URL the way main.py mounts it: /static -> app/static."""
    assert url.startswith("/static/"), url
    return STATIC / url[len("/static/"):]

MANIFEST = json.loads((STATIC / "manifest.json").read_text())
BASE_HTML = (ROOT / "app" / "templates" / "base.html").read_text()


def test_every_manifest_icon_exists():
    """A dangling icon path costs installability and reports nothing."""
    missing = [
        i["src"] for i in MANIFEST["icons"]
        if not on_disk(i["src"]).exists()
    ]
    assert missing == [], f"manifest references icons that are not on disk: {missing}"


def test_installability_requirements():
    """Chrome will not offer 'Install' without these, and says nothing when it declines."""
    sizes = {i["sizes"] for i in MANIFEST["icons"]}
    assert "192x192" in sizes and "512x512" in sizes
    assert MANIFEST["display"] in {"standalone", "fullscreen", "minimal-ui"}
    assert MANIFEST["start_url"] and MANIFEST["name"] and MANIFEST["short_name"]


def test_a_maskable_icon_is_offered():
    """Without one, Android launchers letterbox the icon inside a white blob."""
    assert any(i.get("purpose") == "maskable" for i in MANIFEST["icons"])


def test_ios_home_screen_icon_is_declared_and_present():
    """iOS ignores the manifest entirely and reads apple-touch-icon."""
    assert 'rel="apple-touch-icon"' in BASE_HTML
    assert (STATIC / "icons" / "apple-touch-icon.png").exists()


def test_apple_touch_icon_has_no_alpha():
    """iOS discards the alpha channel and composites the icon on black."""
    png = (STATIC / "icons" / "apple-touch-icon.png").read_bytes()
    # IHDR colour type is byte 25; 6 is RGBA, 4 is grey+alpha.
    assert png[25] not in (4, 6), "apple-touch-icon must be flattened, not RGBA"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_colour_is_declared_per_scheme(scheme):
    """One fixed colour leaves the status bar clashing in the other scheme."""
    assert f'prefers-color-scheme: {scheme}' in BASE_HTML


def test_theme_colour_matches_the_stylesheet_background():
    """The status bar sits directly above the header, which is painted --bg."""
    css = (STATIC / "app.css").read_text()
    light = css.split("--bg:")[1].split(";")[0].strip()
    dark = css.split("--bg:")[2].split(";")[0].strip()
    assert f'content="{light}" media="(prefers-color-scheme: light)"' in BASE_HTML
    assert f'content="{dark}" media="(prefers-color-scheme: dark)"' in BASE_HTML
    assert MANIFEST["theme_color"] == light


def test_touch_targets_meet_the_platform_minimum():
    """44px is Apple's floor. The juz picker is the densest control in the app."""
    css = (STATIC / "app.css").read_text()
    block = css.split(".juzpick-opts span {")[1].split("}")[0]
    height = int(block.split("min-height:")[1].split("px")[0].strip())
    assert height >= 44, f"juz picker tap target is {height}px, below the 44px minimum"


def test_pillow_is_not_a_runtime_dependency():
    """Icons are generated offline and committed; the function stays lean."""
    assert "pillow" not in (ROOT / "requirements.txt").read_text().lower()
