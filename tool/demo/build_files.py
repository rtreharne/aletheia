"""Build the demo coursework files from tool/demo/<slug>/source.txt.

    python tool/demo/build_files.py docx      # needs python-docx (the web container has it)
    python tool/demo/build_files.py pdf       # needs Playwright's Chromium (e.g. the Playwright Docker image)
    python tool/demo/build_files.py other     # plain text and the code .zip

Source format: "# " title, "## " / "### " headings, blank-line-separated paragraphs, "1. " numbered
points. PDF sources may use <sub>/<sup>.
"""

import html
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TARGETS = {
    "history": "peel-corn-laws-essay.docx",
    "economics": "minimum-unit-pricing-brief.docx",
    "chemistry": "clock-reaction-lab-report.pdf",
    "psychology": "sleep-working-memory-report.pdf",
    "nursing": "medication-near-miss-reflection.txt",
}


def blocks(text):
    """Yield (kind, text): title, subtitle, h2, h3, item, para."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    para, first = [], True
    for line in lines + [""]:
        if not line:
            if para:
                yield ("para", " ".join(para))
                para = []
            continue
        if line.startswith("# "):
            yield ("title", line[2:])
        elif line.startswith("## "):
            yield ("h2", line[3:])
        elif line.startswith("### "):
            yield ("h3", line[4:])
        elif re.match(r"^\d+\. ", line):
            yield ("item", line)
        elif para == [] and first is False and _is_subtitle(line):
            yield ("subtitle", line)
        else:
            para.append(line)
        first = False


def _is_subtitle(line):
    return " · " in line and len(line) < 120


URL = re.compile(r"https?://[^\s<]+?(?=[.,;)]?(?:\s|$))")


def _add_text_with_links(paragraph, text):
    """Add text to a python-docx paragraph, turning URLs into real hyperlinks."""
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    pos = 0
    for match in URL.finditer(text):
        if match.start() > pos:
            paragraph.add_run(text[pos:match.start()])
        url = match.group(0)
        rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), rel_id)
        run = OxmlElement("w:r")
        props = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "Hyperlink")
        props.append(style)
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "0563C1")
        props.append(color)
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        props.append(underline)
        run.append(props)
        t = OxmlElement("w:t")
        t.text = url
        run.append(t)
        link.append(run)
        paragraph._p.append(link)
        pos = match.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def build_docx(slug, target):
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    for kind, text in blocks((HERE / slug / "source.txt").read_text()):
        text = re.sub(r"</?su[bp]>", "", text)
        if kind == "title":
            doc.add_heading(text, level=0)
        elif kind == "subtitle":
            p = doc.add_paragraph()
            p.add_run(text).italic = True
        elif kind == "h2":
            doc.add_heading(text, level=1)
        elif kind == "h3":
            doc.add_heading(text, level=2)
        elif kind == "item":
            doc.add_paragraph(re.sub(r"^\d+\. ", "", text), style="List Number")
        else:
            _add_text_with_links(doc.add_paragraph(), text)
    doc.save(HERE / slug / target)


def _html_text(text):
    # Escape everything except <sub>/<sup>, and make URLs (e.g. DOIs) clickable.
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"&lt;(/?)(sub|sup)&gt;", r"<\1\2>", escaped)
    return URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)


def build_pdf(slug, target):
    from playwright.sync_api import sync_playwright

    parts = []
    for kind, text in blocks((HERE / slug / "source.txt").read_text()):
        t = _html_text(text)
        parts.append({
            "title": f"<h1>{t}</h1>", "subtitle": f'<p class="sub">{t}</p>', "h2": f"<h2>{t}</h2>",
            "h3": f"<h3>{t}</h3>", "item": f"<p>{t}</p>", "para": f"<p>{t}</p>",
        }[kind])
    page_html = """<!doctype html><html><head><meta charset="utf-8"><style>
      body { font-family: "Liberation Serif", "Times New Roman", serif; font-size: 11.5pt; line-height: 1.45; color: #111; }
      h1 { font-size: 17pt; font-weight: 700; margin: 0 0 4pt; }
      .sub { font-style: italic; color: #444; margin: 0 0 14pt; }
      h2 { font-size: 12.5pt; margin: 14pt 0 4pt; }
      h3 { font-size: 11.5pt; font-style: italic; font-weight: 400; margin: 10pt 0 2pt; }
      p { margin: 0 0 7pt; text-align: justify; }
      sub, sup { font-size: 70%; line-height: 0; }
      a { color: #1a4f8b; text-decoration: none; overflow-wrap: anywhere; }
      p:has(a) { text-align: left; }
      h2 + p, p { orphans: 2; widows: 2; }
    </style></head><body>""" + "\n".join(parts) + "</body></html>"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(page_html)
        page.pdf(path=str(HERE / slug / target), format="A4",
                 margin={"top": "22mm", "bottom": "22mm", "left": "24mm", "right": "24mm"},
                 display_header_footer=True, header_template="<span></span>",
                 footer_template='<div style="font-size:8pt;width:100%;text-align:center;color:#777"><span class="pageNumber"></span></div>')
        browser.close()


def build_other():
    shutil.copyfile(HERE / "nursing" / "source.txt", HERE / "nursing" / TARGETS["nursing"])
    shutil.copyfile(ROOT / "examples" / "code-viva" / "weather-pipeline.zip", HERE / "code" / "weather-pipeline.zip")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    for slug, target in TARGETS.items():
        if what == "docx" and target.endswith(".docx"):
            build_docx(slug, target)
        elif what == "pdf" and target.endswith(".pdf"):
            build_pdf(slug, target)
    if what == "other":
        build_other()
    print("built", what)
