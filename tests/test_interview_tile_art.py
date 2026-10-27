"""Interview prep's two round tiles carry one quiet line drawing each (camera / two people),
decorative and hidden from screen readers, very low contrast (Kofi chose it, 2026-10-27)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_each_round_tile_has_one_decorative_drawing():
    html = (ROOT / "ui/templates/index.html").read_text()
    for tid in ("tileRound1", "tileRound2"):
        tile = html[html.index(f'id="{tid}"'):]
        tile = tile[:tile.index("</button>")]
        assert tile.count('class="prep-tile-art" aria-hidden="true"') == 1
        assert "<svg" in tile
    css = (ROOT / "ui/static/styles.css").read_text()
    block = css[css.index(".prep-tile-art {"):]
    assert "opacity: .08" in block[:block.index("}")]
