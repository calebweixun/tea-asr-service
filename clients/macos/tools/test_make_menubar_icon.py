"""Regression checks for the generated macOS menu-bar template images.

Run from the repository root with:

    uv run --with pillow --with pytest pytest clients/macos/tools/test_make_menubar_icon.py

The @2x files are the acceptance target because they are the assets rendered
on a Retina menu bar.  These checks deliberately validate alpha coverage rather
than RGB colour: AppKit uses the alpha channel of a template image as its mask
and supplies the light/dark appearance tint itself.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image, ImageChops


MACOS_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MACOS_ROOT / "tools" / "make-menubar-icon.py"
SOURCE_PATH = MACOS_ROOT / "tools" / "source-menubar.png"
ASSET_PATH = MACOS_ROOT / "Resources" / "icons"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_menubar_icon", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib failure
        raise AssertionError(f"could not import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _asset(state: str, scale: int) -> Image.Image:
    suffix = "" if scale == 1 else f"@{scale}x"
    return Image.open(ASSET_PATH / f"MenuBar-{state}{suffix}.png").convert("RGBA")


def test_generated_assets_match_source_and_dimensions() -> None:
    generator = _load_generator()
    cells = generator._cells(SOURCE_PATH)

    for scale in (1, 2, 3):
        expected_images = generator._render(cells, generator.SIDE * scale)
        for state, expected in zip(generator.STATES, expected_images):
            actual = _asset(state, scale)
            assert actual.size == (generator.SIDE * scale, generator.SIDE * scale)
            assert actual.mode == "RGBA"
            assert ImageChops.difference(actual, expected).getbbox() is None

            # A template should be a black alpha mask, never a coloured image.
            assert all(
                red == green == blue == 0
                for red, green, blue, _ in actual.get_flattened_data()
            )
            assert actual.getchannel("A").getbbox() is not None


def test_retina_templates_are_solid_enough_for_both_appearances() -> None:
    # The old luminance inversion produced a mostly translucent mask because
    # the source cup is dark grey (roughly 92% opacity after inversion).  A
    # solid template has a substantial fully opaque interior at the @2x target.
    for state in ("idle", "listening", "error"):
        image = _asset(state, 2)
        alpha = list(image.getchannel("A").get_flattened_data())
        ink = [value for value in alpha if value >= 192]
        assert len(ink) >= 250, f"{state}: too little opaque cup coverage"
        assert sum(value >= 240 for value in alpha) / len(alpha) >= 0.18
        assert sum(ink) / len(ink) >= 245, f"{state}: cup mask is too translucent"
        assert max(alpha) == 255


def test_states_remain_distinct_and_share_a_baseline() -> None:
    images = {state: _asset(state, 2).getchannel("A") for state in ("idle", "listening", "error")}
    bottoms = [images[state].getbbox()[3] for state in images]
    assert max(bottoms) - min(bottoms) <= 1

    for first, second in (("idle", "listening"), ("idle", "error"), ("listening", "error")):
        difference = ImageChops.difference(images[first], images[second])
        assert difference.getbbox() is not None
        assert sum(value > 16 for value in difference.get_flattened_data()) >= 40
