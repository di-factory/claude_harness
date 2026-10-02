"""The client's look, from whatever brand material they have.

``read_brand`` takes any mix of files, folders and typed colors (``#1f5f4a``): a logo, a
photo of the shop, a palette swatch, a brand guide (PDF, DOCX, slides exported to PDF), a CSS
or JSON theme, an SVG diagram. It finds the colors in each (pixels of images, hex and
``rgb()`` codes in text) and a logo, and returns the ``branding`` an instance carries:

    {"colors": {"primary": "#1f5f4a", "accent": "#e8a33d"}, "logo": "data:image/png;base64,..."}

Colors typed or written in a document win over colors sampled from pixels; near-white,
near-black and greys are never the primary color. The logo is re-encoded small (a PNG at most
320 px) and travels inside the instance, so it is signed with the rest of the solution.
"""

from __future__ import annotations

import base64
import colorsys
import io
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
OFFICE = re.compile(r'srgbClr val="([0-9A-Fa-f]{6})"')
# Color words people type ("dark blue", "azul marino"): CSS names plus common Spanish ones.
_CSS = (
    "aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff "
    "beige:f5f5dc bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff "
    "blueviolet:8a2be2 brown:a52a2a burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00 "
    "chocolate:d2691e coral:ff7f50 cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c "
    "cyan:00ffff darkblue:00008b darkcyan:008b8b darkgoldenrod:b8860b darkgray:a9a9a9 "
    "darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b darkmagenta:8b008b "
    "darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000 "
    "darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f "
    "darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493 deepskyblue:00bfff "
    "dimgray:696969 dodgerblue:1e90ff firebrick:b22222 floralwhite:fffaf0 "
    "forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700 "
    "goldenrod:daa520 gray:808080 green:008000 greenyellow:adff2f grey:808080 "
    "honeydew:f0fff0 hotpink:ff69b4 indianred:cd5c5c indigo:4b0082 ivory:fffff0 "
    "khaki:f0e68c lavender:e6e6fa lavenderblush:fff0f5 lawngreen:7cfc00 "
    "lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080 lightcyan:e0ffff "
    "lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 lightgrey:d3d3d3 "
    "lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa "
    "lightslategray:778899 lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00 "
    "limegreen:32cd32 linen:faf0e6 magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa "
    "mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371 "
    "mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc "
    "mediumvioletred:c71585 midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 "
    "moccasin:ffe4b5 navajowhite:ffdead navy:000080 navyblue:000080 oldlace:fdf5e6 "
    "olive:808000 olivedrab:6b8e23 orange:ffa500 orangered:ff4500 orchid:da70d6 "
    "palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093 "
    "papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd "
    "powderblue:b0e0e6 purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f "
    "royalblue:4169e1 saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 "
    "seashell:fff5ee sienna:a0522d silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd "
    "slategray:708090 snow:fffafa springgreen:00ff7f steelblue:4682b4 tan:d2b48c "
    "teal:008080 thistle:d8bfd8 tomato:ff6347 turquoise:40e0d0 violet:ee82ee wheat:f5deb3 "
    "white:ffffff whitesmoke:f5f5f5 yellow:ffff00 yellowgreen:9acd32"
)
_ES = {
    "azul": "1e5aa8", "azul marino": "1b2a4a", "azul oscuro": "00008b", "azul claro": "add8e6",
    "azul cielo": "87ceeb", "azul rey": "4169e1", "celeste": "5bc0eb", "turquesa": "1abc9c",
    "rojo": "c62828", "vino": "7b1e3a", "rosa": "e91e63", "rosa palo": "f4c2c2",
    "fucsia": "d81b60", "coral": "ff7f50", "naranja": "ef6c00", "amarillo": "f2c94c",
    "dorado": "d4af37", "plateado": "c0c0c0", "verde": "2e7d32", "verde oscuro": "1b5e20",
    "verde claro": "90ee90", "verde menta": "98d8c8", "morado": "6a1b9a", "lila": "b39ddb",
    "violeta": "8e44ad", "cafe": "6d4c41", "café": "6d4c41", "marron": "6d4c41",
    "marrón": "6d4c41", "beige": "f5f5dc", "crema": "fff8e1", "gris": "808080",
    "gris oscuro": "424242", "negro": "111111", "blanco": "ffffff", "terracota": "c0603c",
}  # fmt: skip
NAMED = {k: "#" + v for k, v in (pair.split(":") for pair in _CSS.split())}
NAMED.update({k: "#" + v for k, v in _ES.items()})
_WORD = re.compile(r"[a-záéíóúñ]+")


def named_colors(text: str) -> tuple[list[str], list[str]]:
    """Color words in plain text, in order ("dark blue", "lightblue", "azul marino"), and
    the words that are not colors."""
    words = _WORD.findall(text.lower())
    found, rest, i = [], [], 0
    while i < len(words):
        two = " ".join(words[i : i + 2]) if i + 1 < len(words) else ""
        if two and (two in NAMED or two.replace(" ", "") in NAMED):
            found.append(NAMED.get(two) or NAMED[two.replace(" ", "")])
            i += 2
        elif words[i] in NAMED:
            found.append(NAMED[words[i]])
            i += 1
        else:
            rest.append(words[i])
            i += 1
    return list(dict.fromkeys(found)), rest


RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
RASTER = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOCUMENTS = {".pdf", ".docx", ".xlsx", ".pptx"}
LOGO_PX = 320
MAX_LOGO_BYTES = 300_000
MAX_FILES = 60


Describe = Callable[[str], list[str] | None]  # a description -> "#rrggbb" colors, or None


@dataclass
class Brand:
    colors: list[str] = field(default_factory=list)  # best first
    logo: str | None = None  # a data: URI
    notes: list[str] = field(default_factory=list)  # what was read, what was skipped

    def branding(self) -> dict[str, Any] | None:
        primary, accent = pick_palette(self.colors)
        out: dict[str, Any] = {}
        if primary:
            out["colors"] = {"primary": primary, **({"accent": accent} if accent else {})}
        if self.logo:
            out["logo"] = self.logo
        return out or None


def _norm(code: str) -> str:
    code = code.lower()
    if len(code) == 4:
        code = "#" + "".join(c * 2 for c in code[1:])
    return code


def _rgb(code: str) -> tuple[float, float, float]:
    return tuple(int(code[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _usable(code: str) -> bool:
    """Not near-white, not near-black, not a grey: a color a brand can be recognized by."""
    _h, lightness, s = colorsys.rgb_to_hls(*_rgb(code))
    return 0.12 < lightness < 0.85 and s > 0.22


def _hue_far(a: str, b: str) -> bool:
    ha = colorsys.rgb_to_hls(*_rgb(a))[0]
    hb = colorsys.rgb_to_hls(*_rgb(b))[0]
    d = abs(ha - hb)
    return min(d, 1 - d) > 0.08


def _light_far(a: str, b: str) -> bool:
    """Two shades of one color still pair well when their lightness differs clearly."""
    return abs(colorsys.rgb_to_hls(*_rgb(a))[1] - colorsys.rgb_to_hls(*_rgb(b))[1]) > 0.2


def pick_palette(colors: list[str]) -> tuple[str | None, str | None]:
    usable = [c for c in colors if _usable(c)]
    if not usable:
        return None, None
    primary = usable[0]
    accent = next((c for c in usable[1:] if _hue_far(primary, c) or _light_far(primary, c)), None)
    return primary, accent


def _text_colors(text: str) -> list[str]:
    found = [_norm(m) for m in HEX.findall(text)]
    for r, g, b in RGB.findall(text):
        found.append("#" + "".join(f"{min(255, int(v)):02x}" for v in (r, g, b)))
    return [c for c, _ in Counter(found).most_common()]


def _document_colors(data: bytes, suffix: str, name: str) -> list[str]:
    """Office files carry their palette in a theme (``srgbClr``); PDFs and documents may
    also write color codes in their text."""
    found: list[str] = []
    if suffix != ".pdf":
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if "theme" in member and member.endswith(".xml"):
                    xml = zf.read(member).decode("utf-8", "replace")
                    found += [_norm("#" + v) for v in OFFICE.findall(xml)]
    if suffix != ".pptx":
        from ..documents.extract import extract

        found += _text_colors(extract(data, name).text)
    return found


def _image_colors(data: bytes) -> list[str]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        img = source.convert("RGBA")
    img.thumbnail((96, 96))
    counts: Counter[str] = Counter()
    raw = img.tobytes()
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 128:
            continue
        counts[f"#{_bucket(r):02x}{_bucket(g):02x}{_bucket(b):02x}"] += 1
    return [c for c, _ in counts.most_common(12)]


def _bucket(value: int) -> int:
    """Similar pixels count as one color."""
    return min(255, (value // 24) * 24 + 12)


def _logo(data: bytes, suffix: str) -> str | None:
    if suffix == ".svg":
        if len(data) > MAX_LOGO_BYTES:
            return None
        return "data:image/svg+xml;base64," + base64.b64encode(data).decode()
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        img = source.convert("RGBA")
    img.thumbnail((LOGO_PX, LOGO_PX))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    raw = out.getvalue()
    if len(raw) > MAX_LOGO_BYTES:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _looks_like_path(item: str) -> bool:
    return "/" in item or item.startswith("~") or bool(re.search(r"\.[A-Za-z0-9]{2,5}$", item))


def _files(items: list[str]) -> tuple[list[Path], list[str]]:
    files, typed = [], []
    for item in items:
        item = item.strip().strip("'\"")
        if not item:
            continue
        path = Path(item).expanduser()
        if HEX.fullmatch(item):
            typed.append(_norm(item))
        elif path.is_dir():
            files += sorted(
                p for p in path.rglob("*") if p.is_file() and not p.name.startswith(".")
            )
        elif path.is_file():
            files.append(path)
        elif _looks_like_path(item):
            typed.append(f"?{item}")  # a file that is not there
        # anything else is words: read as color names (read_brand)
    return files[:MAX_FILES], typed


_FILLER = {"and", "y", "also", "use", "with", "con", "the", "el", "la", "de", "of", "a",
           "or", "o", "plus", "colors", "colores", "color", "tambien", "también"}  # fmt: skip


def read_brand(
    items: list[str], *, text: str | None = None, describe: Describe | None = None
) -> Brand:
    """Colors and a logo from files, folders, typed codes and color words. ``text`` is
    what the person typed; when it says more than color names ("and any other blue to
    complete"), ``describe`` (a model) turns the description into a palette."""
    files, typed = _files(items)
    brand = Brand(notes=[f"not found: {t[1:]}" for t in typed if t.startswith("?")])
    written: list[str] = [t for t in typed if not t.startswith("?")]  # typed or in documents
    words = " ".join(i for i in items if not HEX.fullmatch(i.strip()) and not _looks_like_path(i))
    if text is not None:
        words = text
        for item in items:  # paths and folders are not words
            if _looks_like_path(item) or Path(item).expanduser().exists():
                words = words.replace(item, " ")
    named, rest = named_colors(HEX.sub(" ", words))
    rest = [w for w in rest if w not in _FILLER]
    palette = describe(words) if describe is not None and rest else None
    if palette:
        written += palette
        brand.notes.append("colors from your description: " + ", ".join(palette))
    else:
        written += named
        if named:
            brand.notes.append("colors from your words: " + ", ".join(named))
        if rest and describe is None:
            brand.notes.append("not read as colors: " + " ".join(rest[:12]))
    sampled: list[str] = []  # from pixels
    logos: list[tuple[int, str]] = []
    for path in files:
        suffix = path.suffix.lower()
        try:
            data = path.read_bytes()
            if suffix in RASTER:
                sampled += _image_colors(data)
                logo = _logo(data, suffix)
                if logo:
                    logos.append((0 if "logo" in path.stem.lower() else 1, logo))
                brand.notes.append(f"image: {path.name}")
            elif suffix == ".svg":
                written += _text_colors(data.decode("utf-8", "replace"))
                logo = _logo(data, suffix)
                if logo:
                    logos.append((0 if "logo" in path.stem.lower() else 2, logo))
                brand.notes.append(f"vector: {path.name}")
            elif suffix in DOCUMENTS:
                written += _document_colors(data, suffix, path.name)
                brand.notes.append(f"document: {path.name}")
            else:
                written += _text_colors(data.decode("utf-8", "replace"))
                brand.notes.append(f"text: {path.name}")
        except Exception as exc:  # one unreadable file never stops the others
            brand.notes.append(f"skipped {path.name}: {type(exc).__name__}")
    seen: dict[str, None] = {}
    for code in written + sampled:
        seen.setdefault(code, None)
    brand.colors = list(seen)
    if logos:
        brand.logo = sorted(logos, key=lambda x: x[0])[0][1]
    return brand
