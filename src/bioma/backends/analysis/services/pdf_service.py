from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple


_CSS_TEMPLATE = """
body {{
    font-family: "{font_family}", "Helvetica", sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #222;
    overflow-wrap: anywhere;
    -pdf-word-wrap: {word_wrap};
}}
h1 {{ font-size: 20pt; margin-top: 12pt; }}
h2 {{ font-size: 15pt; margin-top: 10pt; border-bottom: 1px solid #ccc; padding-bottom: 4pt; }}
h3 {{ font-size: 12pt; margin-top: 8pt; }}
h4 {{ font-size: 11pt; margin-top: 6pt; }}
p  {{ margin: 6pt 0; }}
ul, ol {{ margin: 6pt 0 6pt 18pt; }}
li {{ margin: 3pt 0; }}
table {{ border-collapse: collapse; margin: 8pt 0; }}
th, td {{ border: 1px solid #999; padding: 4pt 6pt; }}
code, pre {{ font-family: "{code_font}", monospace; font-size: 10pt; }}
img {{ max-width: 90%; }}
"""


def _register_report_font(path: Path) -> str:
    """Embed font bytes without xhtml2pdf's Windows-locked temporary font file."""
    from hashlib import sha256
    from io import BytesIO
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf.default import DEFAULT_FONT

    raw = path.read_bytes()
    family = 'bioma-' + sha256(raw).hexdigest()
    if family not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(family, BytesIO(raw)))
        pdfmetrics.registerFontFamily(family, normal=family, bold=family,
                                      italic=family, boldItalic=family)
    # Each renderer copies this registry; immutable content-based names avoid
    # collisions between concurrent reports or a font replaced at the same path.
    DEFAULT_FONT[family] = family
    return family


def _report_font(text: str) -> tuple[str, Path | None]:
    """Require actual CJK glyph coverage instead of silently substituting Latin fonts."""
    from hashlib import sha256
    from matplotlib.font_manager import findSystemFonts
    from reportlab.pdfbase.ttfonts import TTFont

    required = {ord(c) for c in text if "\u2e80" <= c <= "\u9fff" or "\uf900" <= c <= "\ufaff" or "\uff00" <= c <= "\uffef"}
    if not required:
        return "Helvetica", None
    configured = os.getenv("BIOMA_CJK_FONT")
    candidates = [configured] if configured else sorted(findSystemFonts())
    for path in candidates:
        name = "bioma-" + sha256(str(path).encode()).hexdigest()[:12]
        try:
            font = TTFont(name, path)
            if not required <= font.face.charToGlyph.keys():
                continue
            return name, Path(path).resolve()
        except Exception as exc:
            # Unsupported font formats should not prevent trying another font.
            if configured:
                raise ValueError(f"Cannot load BIOMA_CJK_FONT: {path}: {exc}") from exc
    raise ValueError("No usable Chinese font. Set BIOMA_CJK_FONT to a TrueType font with the required Chinese glyphs.")


def _normalize_image_paths(md_text: str) -> str:
    """Convert backslashes inside markdown image URLs to forward slashes."""

    def _repl(m: re.Match) -> str:
        alt, url = m.group(1), m.group(2)
        url_fixed = url.replace("\\", "/")
        return f"![{alt}]({url_fixed})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _repl, md_text)


def _render_markdown_to_html(md_text: str) -> str:
    import markdown as md_lib

    md_text = _normalize_image_paths(md_text)
    return md_lib.markdown(
        md_text,
        extensions=["extra", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )


def _link_callback_factory(project_root: Path, md_dir: Path):
    """
    Resolve relative image paths in the markdown against:
      1. the markdown file's directory
      2. the project root
    xhtml2pdf calls this for every <img src=...> / url(...) reference.
    """
    project_root = project_root.resolve()
    md_dir = md_dir.resolve()

    def _cb(uri: str, rel: str) -> str:  # noqa: ARG001
        if not uri:
            return uri
        if uri.startswith(("http://", "https://", "data:")):
            return uri
        if os.path.isabs(uri) and Path(uri).exists():
            return uri
        uri_norm = uri.replace("\\", "/")
        for base in (md_dir, project_root):
            candidate = (base / uri_norm).resolve()
            if candidate.exists():
                return str(candidate)
        return uri

    return _cb


def export_markdown_to_pdf(
    md_path: str,
    pdf_path: Optional[str] = None,
    project_root: Optional[str] = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    Convert a Markdown report file to PDF next to it (or at pdf_path).

    Returns (ok, pdf_or_md_path, error_message).
    On failure, never raises — the caller can decide whether to keep going.
    """
    md_file = Path(md_path)
    if not md_file.exists():
        return False, str(md_file), f"Markdown file not found: {md_file}"

    out_pdf = Path(pdf_path) if pdf_path else md_file.with_suffix(".pdf")
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()

    try:
        md_text = md_file.read_text(encoding="utf-8")
    except Exception as exc:
        return False, str(out_pdf), f"Failed to read markdown: {exc!r}"

    try:
        body_html = _render_markdown_to_html(md_text)
    except Exception as exc:
        return False, str(out_pdf), f"Markdown→HTML failed: {exc!r}"

    try:
        font_family, font_path = _report_font(md_text)
        if font_path:
            font_family = _register_report_font(font_path)
    except Exception as exc:
        return False, str(out_pdf), str(exc)
    css = _CSS_TEMPLATE.format(font_family=font_family, code_font=font_family if font_path else "Courier New", word_wrap="CJK" if font_path else "LTR")

    html_doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{css}</style></head><body>{body_html}</body></html>"
    )

    try:
        from xhtml2pdf import pisa  # type: ignore
    except Exception as exc:
        return False, str(out_pdf), f"xhtml2pdf not installed: {exc!r}"

    try:
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        link_cb = _link_callback_factory(root, md_file.parent)
        with open(out_pdf, "wb") as f:
            result = pisa.CreatePDF(
                src=html_doc,
                path=str(md_file.resolve()),
                dest=f,
                encoding="utf-8",
                link_callback=link_cb,
            )
        if getattr(result, "err", 1):
            return False, str(out_pdf), f"pisa reported {result.err} error(s)"
    except Exception as exc:
        return False, str(out_pdf), f"PDF generation failed: {exc!r}"

    return True, str(out_pdf), None


__all__ = ["export_markdown_to_pdf"]
