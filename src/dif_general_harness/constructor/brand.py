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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
OFFICE = re.compile(r'srgbClr val="([0-9A-Fa-f]{6})"')
RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
RASTER = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOCUMENTS = {".pdf", ".docx", ".xlsx", ".pptx"}
LOGO_PX = 320
MAX_LOGO_BYTES = 300_000
MAX_FILES = 60


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


def pick_palette(colors: list[str]) -> tuple[str | None, str | None]:
    usable = [c for c in colors if _usable(c)]
    if not usable:
        return None, None
    primary = usable[0]
    accent = next((c for c in usable[1:] if _hue_far(primary, c)), None)
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
        else:
            typed.append(f"?{item}")
    return files[:MAX_FILES], typed


def read_brand(items: list[str]) -> Brand:
    """Colors and a logo from files, folders and typed colors."""
    files, typed = _files(items)
    brand = Brand(notes=[f"not found: {t[1:]}" for t in typed if t.startswith("?")])
    written: list[str] = [t for t in typed if not t.startswith("?")]  # typed or in documents
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
