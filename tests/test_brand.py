"""The client's look from any brand material, and the pages that use it."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from dif_general_harness.channels.landing import render_landing
from dif_general_harness.channels.web_page import render_chat
from dif_general_harness.constructor import build
from dif_general_harness.constructor.brand import pick_palette, read_brand
from dif_general_harness.constructor.interview import load_answers
from dif_general_harness.spec import PackCatalog, load_instance

PINK, PLUM = (214, 51, 132), "#4a1450"


def _logo(path: Path) -> Path:
    img = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((20, 20, 580, 580), fill=(*PINK, 255))
    draw.rectangle((250, 150, 330, 450), fill=(255, 255, 255, 255))
    img.save(path)
    return path


def _pptx(path: Path) -> Path:
    theme = (
        '<a:theme xmlns:a="x"><a:clrScheme><a:dk1><a:srgbClr val="111111"/></a:dk1>'
        '<a:accent1><a:srgbClr val="0B6E4F"/></a:accent1>'
        '<a:accent2><a:srgbClr val="F2A541"/></a:accent2></a:clrScheme></a:theme>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ppt/theme/theme1.xml", theme)
        zf.writestr("ppt/slides/slide1.xml", "<p:sld/>")
    path.write_bytes(buf.getvalue())
    return path


def test_colors_and_a_logo_from_any_material(tmp_path: Path) -> None:
    logo = _logo(tmp_path / "logo.png")
    brand = read_brand([str(logo)])
    branding = brand.branding()
    assert branding is not None and branding["logo"].startswith("data:image/png;base64,")
    primary = branding["colors"]["primary"]
    assert abs(int(primary[1:3], 16) - PINK[0]) < 30  # the logo's pink, not white or clear
    shrunk = Image.open(io.BytesIO(base64.b64decode(branding["logo"].split(",", 1)[1])))
    assert shrunk.width <= 320  # re-encoded small: it travels inside the signed instance

    theme = read_brand([str(_pptx(tmp_path / "brand.pptx"))]).branding()
    assert theme == {"colors": {"primary": "#0b6e4f", "accent": "#f2a541"}}  # dark text skipped

    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "theme.css").write_text(":root{--x:#fff;--brand: rgb(11, 110, 79)}")
    (tmp_path / "kit" / "mark.svg").write_text('<svg><path fill="#F2A541"/></svg>')
    kit = read_brand([str(tmp_path / "kit")])
    assert kit.branding() and kit.branding()["colors"]["primary"] in {"#0b6e4f", "#f2a541"}
    assert kit.branding()["logo"].startswith("data:image/svg+xml;base64,")

    typed = read_brand([PLUM, str(logo), "nope.pdf"])  # a typed color wins over pixels
    assert typed.branding()["colors"]["primary"] == PLUM
    assert "not found: nope.pdf" in typed.notes
    assert read_brand(["#ffffff", "#000000", "#808080"]).branding() is None  # no brand there
    assert pick_palette(["#0b6e4f", "#0c704f", "#f2a541"]) == ("#0b6e4f", "#f2a541")


def test_the_look_is_kept_across_rebuilds_and_drawn(examples: Path, tmp_path: Path) -> None:
    from tests.test_constructor import CLINIC, _flat

    catalog = PackCatalog(roots=[examples])
    branding = read_brand([str(_logo(tmp_path / "logo.png")), PLUM]).branding()
    answers = {**_flat(CLINIC), "branding": branding}
    first = build(catalog, ["pyme-appointment-agent"], tmp_path / "out", answers=answers)
    assert first.ok and json.loads(first.spec_path.read_text())["branding"] == branding
    saved = load_answers(first.answers_path)
    assert saved["branding"] == branding  # in the answers file: a rebuild keeps it
    again = build(catalog, ["pyme-appointment-agent"], tmp_path / "out", answers=saved)
    assert again.ok and json.loads(again.spec_path.read_text())["branding"] == branding

    assert again.resolved is not None
    page = render_landing(again.resolved.spec, again.resolved.data, "web")
    assert f"--brand:{PLUM};--on-brand:#ffffff" in page  # white text on the dark plum
    assert '<img class="logo" src="data:image/png;base64,' in page
    assert "Ver en el mapa" in page and 'class="icon"' in page
    chat = render_chat("web", "Clínica", locale="es", public=True, color=PLUM,
                       logo=branding["logo"])  # fmt: skip
    assert f"--me:{PLUM}" in chat and '<img class="mark" src="data:image/png' in chat


def test_without_branding_the_page_has_a_monogram(examples: Path) -> None:
    resolved = load_instance(
        examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])
    )
    page = render_landing(resolved.spec, resolved.data, "web")
    assert '<span class="monogram" aria-hidden="true">C</span>' in page
    assert "--brand:#1f5f4a" in page


@pytest.mark.parametrize(
    "branding",
    [
        {"colors": {"primary": "green"}},
        {"logo": "https://example.com/logo.png"},  # never a remote file: it is not signed
        {"logo": "data:text/html;base64,PHNjcmlwdD4="},
    ],
)
def test_invalid_branding_is_caught(examples: Path, branding: dict[str, object]) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    data = json.loads(path.read_text())
    data["branding"] = branding
    path.write_text(json.dumps(data))
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    assert "invalid_branding" in {i.code for i in resolved.issues if i.severity == "error"}


def test_colors_in_words_and_descriptions(tmp_path: Path) -> None:
    from dif_general_harness.constructor.brand import named_colors

    said = "darkblue, lightblue, and white.. also use any other kind of blue to complete"
    alone = read_brand(said.split(), text=said)  # no model at hand: the names still count
    assert alone.branding() == {"colors": {"primary": "#00008b", "accent": "#add8e6"}}
    assert not any(n.startswith("not found") for n in alone.notes)  # words are not files
    assert "not read as colors: any other kind to complete" in alone.notes

    asked: list[str] = []

    def model(text: str) -> list[str]:
        asked.append(text)
        return ["#0b2545", "#8da9c4", "#13315c", "#ffffff"]

    described = read_brand(said.split(), text=said, describe=model)
    assert asked == [said]  # the whole sentence goes to the model
    assert described.branding() == {"colors": {"primary": "#0b2545", "accent": "#8da9c4"}}
    plain = read_brand(["navy", "gold"], text="navy gold", describe=model)
    assert len(asked) == 1 and plain.branding() == {"colors": {"primary": "#000080",
                                                               "accent": "#ffd700"}}  # fmt: skip

    assert named_colors("Azul marino y dorado, con dark blue")[0] == [
        "#1b2a4a", "#d4af37", "#00008b"]  # fmt: skip
    logo = _logo(tmp_path / "logo.png")
    mixed = read_brand([str(logo), "navy"], text=f"{logo} navy", describe=model)
    assert mixed.branding()["colors"]["primary"] == "#000080" and len(asked) == 1


def test_the_setup_asks_the_model_for_a_described_palette(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dif_general_harness.constructor.setup import Setup
    from dif_general_harness.core.messages import Message
    from dif_general_harness.providers import FakeProvider

    advisor = FakeProvider([Message.assistant('{"colors": ["#0b2545", "#8da9c4"]}')])
    said = "darkblue, lightblue, and white.. also use any other kind of blue to complete"
    setup = Setup([examples], tmp_path, ask=lambda _: said, advisor=lambda _: advisor)
    setup.key = "sk-ant-api03-" + "k" * 90
    assert setup.ask_brand() == {"colors": {"primary": "#0b2545", "accent": "#8da9c4"}}
    assert advisor.requests[0].messages[0].text() == said
    out = capsys.readouterr().out
    assert "colors from your description" in out and "not found" not in out
