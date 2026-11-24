"""Complex dry run for the document generators (no LLM / browser needed).

    cd backend
    python -m tests.dry_run_docgen            # writes to tests/out/
    python -m tests.dry_run_docgen --render   # + LibreOffice -> PDF -> PNG previews (if soffice installed)

Exercises: data_ref from scratchpad, scraped-string number parsing, unicode (₹ € ü 日本),
inline markdown, nested lists, restartable numbering, tables with totals/zebra/captions, charts of every
type, screenshots, callouts, quotes, code, TOC, cover page, headers/footers/page numbers, landscape,
Excel formulas/tables/conditional formats/native charts/multiple sheets, and every slide layout.
Each output is re-opened and asserted.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.docgen.common import GenContext, to_columns_rows  # noqa: E402
from app.tools.docgen.excel import build_xlsx  # noqa: E402
from app.tools.docgen.pdf import build_pdf  # noqa: E402
from app.tools.docgen.slides import build_pptx  # noqa: E402
from app.tools.docgen.specs import DocumentSpec, PresentationSpec, WorkbookSpec  # noqa: E402
from app.tools.docgen.word import build_docx  # noqa: E402

OUT = Path(__file__).parent / "out"

# ---------------------------------------------------------------- fixtures (simulate scraped data)
random.seed(7)
REGIONS = ["North", "South", "East", "West", "Central"]
SALES = [  # list[dict] with *string* numbers as a browser would scrape them
    {"Month": f"2026-{m:02d}", "Region": r, "Units": f"{random.randint(800, 4200):,}",
     "Revenue": f"₹{random.randint(90_000, 900_000):,}.{random.randint(0, 99):02d}",
     "Margin": f"{random.uniform(8, 32):.1f}%", "Owner": random.choice(["Asha", "Jürgen", "李雷", "María"]),
     "Link": f"https://example.com/report/{m}{r[0]}", "SKU": f"00{random.randint(100, 999)}"}
    for m in range(1, 7) for r in REGIONS
]
MONTHLY = {"columns": ["Month", "Online", "Retail", "Partner"],
           "rows": [[f"2026-{m:02d}", 120 + m * 14, 200 - m * 6, 60 + m * 9] for m in range(1, 13)]}
TABLES_FROM_PAGE = [["Rank", "Company", "Score", "Change"],
                    *[[i, f"Company {chr(64 + i)} — très bien", round(random.uniform(50, 99), 1),
                       f"{random.choice(['+', '-'])}{random.uniform(0, 5):.1f}%"] for i in range(1, 31)]]
SCRATCH = {"sales": SALES, "monthly": MONTHLY, "ranking": TABLES_FROM_PAGE}


def make_screenshots() -> dict[str, Path]:
    d = OUT / "screens"
    d.mkdir(parents=True, exist_ok=True)
    shots = {}
    for i, (name, color) in enumerate([("dashboard", (31, 58, 95)), ("report", (224, 122, 95))]):
        img = Image.new("RGB", (1366, 768), (245, 247, 250))
        dr = ImageDraw.Draw(img)
        dr.rectangle([0, 0, 1366, 64], fill=color)
        dr.rectangle([0, 64, 220, 768], fill=(230, 234, 240))
        for k in range(6):
            dr.rectangle([260 + (k % 3) * 360, 110 + (k // 3) * 300, 580 + (k % 3) * 360, 370 + (k // 3) * 300],
                         fill="white", outline=(210, 215, 222))
            dr.rectangle([290 + (k % 3) * 360, 300 + (k // 3) * 300 - k * 20, 330 + (k % 3) * 360,
                          350 + (k // 3) * 300], fill=color)
        dr.text((24, 22), f"Fake {name} page", fill="white")
        p = d / f"testthread0-20261019-1000{i:02d}-00{i + 1}-navigate.png"
        img.save(p)
        shots[name] = p
    return shots


def context(shots: dict[str, Path]) -> GenContext:
    ordered = sorted(shots.values())

    def image_resolver(ref: str):
        ref = ref.replace("screenshot:", "")
        if ref in ("latest", "last"):
            return ordered[-1]
        if ref.lstrip("-").isdigit():
            return ordered[int(ref)]
        return shots.get(Path(ref).stem) or next((p for p in ordered if p.name == ref), None)

    def data_loader(key: str):
        if key not in SCRATCH:
            raise ValueError(f"data_ref '{key}' not found")
        return json.loads(json.dumps(SCRATCH[key]))  # simulate JSON round trip

    return GenContext(data_loader=data_loader, image_resolver=image_resolver)


# ---------------------------------------------------------------- specs
THEME = {"primary": "0B3C5D", "secondary": "328CC1", "accent": "D9B310", "font_heading": "Georgia",
         "font_body": "Calibri"}

DOC_BLOCKS = [
    {"type": "toc"},
    {"type": "heading", "text": "Executive summary", "level": 1},
    {"type": "paragraph", "text": "Revenue grew **18.4%** half-on-half, led by the *North* region. "
                                  "Data was scraped from [the sales portal](https://example.com/sales) and "
                                  "normalised with `pandas`-style parsing. Unicode check: ₹ € ü 日本 ✓.\n\n"
                                  "Second paragraph after a blank line.\nAnd a manual line break.",
     "align": "justify"},
    {"type": "callout", "style": "success", "title": "Key finding",
     "text": "North and West together delivered **52%** of revenue."},
    {"type": "key_value", "pairs": [["Source", "Sales portal (scraped)"], ["Period", "Jan–Jun 2026"],
                                    ["Rows", len(SALES)], ["Prepared by", "Web Agent"]]},
    {"type": "heading", "text": "Highlights", "level": 2},
    {"type": "bullets", "items": ["Top region: **North**", {"text": "Online up 40%", "level": 1},
                                  {"text": "Partner channel steady", "level": 2}, "Margins stable at ~20%"]},
    {"type": "numbered", "items": ["Extract", "Clean", "Publish"]},
    {"type": "paragraph", "text": "A second numbered list must restart at 1:"},
    {"type": "numbered", "items": ["Plan", "Act", {"text": "sub-step", "level": 1}, "Verify"]},
    {"type": "heading", "text": "Data", "level": 1},
    {"type": "table", "table": {"data_ref": "sales", "select_columns": ["Month", "Region", "Units", "Revenue", "Margin"],
                                "max_rows": 12, "caption": "Table 1 — first 12 rows (data_ref: sales)",
                                "total_row": False}},
    {"type": "table", "table": {"columns": ["Item", "Qty", "Price"], "rows": [["Widget", 3, 9.5], ["Gadget", 12, 120.25],
                                                                             ["Gizmo", 1, 1999]],
                                "total_row": True, "caption": "Table 2 — inline rows with total"}},
    {"type": "heading", "text": "Charts", "level": 1},
    {"type": "chart", "chart": {"chart_type": "line", "title": "Monthly channel mix", "data_ref": "monthly",
                                "category_column": "Month", "y_title": "Orders"}, "caption": "Figure 1"},
    {"type": "chart", "chart": {"chart_type": "column", "title": "Stacked quarterly", "categories": ["Q1", "Q2", "Q3"],
                                "series": [{"name": "A", "values": [3, 4, 5]}, {"name": "B", "values": [2, 2, 3]}],
                                "stacked": True, "show_values": True}, "width_in": 5},
    {"type": "chart", "chart": {"chart_type": "doughnut", "title": "Share", "categories": ["N", "S", "E", "W"],
                                "series": [{"name": "Share", "values": [40, 25, 20, 15]}]}, "width_in": 4.5},
    {"type": "chart", "chart": {"chart_type": "bar", "title": "Horizontal bars", "categories": ["Alpha", "Beta", "Gamma"],
                                "series": [{"name": "Score", "values": [88, 72, 95]}], "show_values": True},
     "width_in": 5},
    {"type": "page_break"},
    {"type": "heading", "text": "Evidence", "level": 1},
    {"type": "image", "image": "screenshot:latest", "caption": "Screenshot of the report page", "width_in": 6},
    {"type": "image", "image": "does-not-exist.png"},  # must degrade gracefully
    {"type": "quote", "text": "Without data you're just another person with an opinion.", "caption": "W. E. Deming"},
    {"type": "code", "text": "SELECT region, SUM(revenue)\nFROM sales\nGROUP BY region;"},
    {"type": "divider"},
    {"type": "callout", "style": "warning", "text": "Figures are unaudited."},
    {"type": "heading", "text": "Appendix", "level": 2},
    {"type": "heading", "text": "Method detail", "level": 3},
    {"type": "paragraph", "text": "Lorem-free filler text. " * 30},
]

DOC_SPEC = {"filename": "Sales Report 2026 H1", "title": "Sales Report — H1 2026",
            "subtitle": "Scraped from the sales portal", "author": "Web Agent", "cover_page": True,
            "header_text": "Confidential · Internal", "footer_text": "Generated by Web Agent",
            "theme": THEME, "blocks": DOC_BLOCKS}

XLSX_SPEC = {
    "filename": "sales_data.xlsx",
    "theme": THEME,
    "sheets": [
        {"name": "Sales [raw]/H1", "title": "Sales H1 2026 (scraped)", "data_ref": "sales",
         "columns": ["Month", "Region", {"header": "Units", "type": "integer"},
                     {"header": "Revenue (₹)", "source": "Revenue", "type": "currency"}, "Margin",
                     {"header": "Owner"}, {"header": "Link", "type": "url"}, "SKU"],
         "computed_columns": {"Rev per Unit": "={Revenue (₹)}/{Units}"},
         "totals": {"Units": "sum", "Revenue (₹)": "sum", "Margin": "average", "Rev per Unit": "average"},
         "conditional_formats": [{"column": "Margin", "rule": "color_scale"},
                                 {"column": "Units", "rule": "data_bar"},
                                 {"column": "Region", "rule": "contains", "value": "North", "color": "FFF2CC"},
                                 {"column": "Revenue (₹)", "rule": "top_n", "value": 5, "color": "C6EFCE"}],
         "charts": [{"chart_type": "column", "title": "Units by row", "category_column": "Month",
                     "value_columns": ["Units"]}],
         "notes": "Source: example.com sales portal, scraped 2026-10-19"},
        {"name": "Monthly", "data_ref": "monthly", "as_table": False,
         "computed_columns": {"Total": "={Online}+{Retail}+{Partner}", "Online share": "={Online}/{Total}"},
         "totals": {"Online": "sum", "Retail": "sum", "Partner": "sum", "Total": "sum"},
         "charts": [{"chart_type": "line", "title": "Channels", "category_column": "Month",
                     "value_columns": ["Online", "Retail", "Partner"], "y_title": "Orders"},
                    {"chart_type": "pie", "title": "Jan split", "category_column": "Month", "value_columns": ["Online"],
                     "anchor": "J40"}]},
        {"name": "Ranking", "data_ref": "ranking",
         "conditional_formats": [{"column": "Score", "rule": "greater_than", "value": 90, "color": "C6EFCE"}],
         "charts": [{"chart_type": "bar", "category_column": "Company", "value_columns": ["Score"], "title": "Scores"}]},
        {"name": "Summary", "rows": [], "cells": [
            {"cell": "A1", "value": "KPI", "bold": True, "fill": "DDEBF7"},
            {"cell": "B1", "value": "Value", "bold": True, "fill": "DDEBF7"},
            {"cell": "A2", "value": "Total units"}, {"cell": "B2", "value": "=SUM(Table1[Units])", "number_format": "#,##0"},
            {"cell": "A3", "value": "Avg score"}, {"cell": "B3", "value": "=AVERAGE(Table2[Score])", "number_format": "0.0",
                                                    "comment": "Average of the Ranking sheet"},
            {"cell": "A4", "value": "Rows scraped"}, {"cell": "B4", "value": "=COUNTA(Table1[Region])"}]},
        {"name": "Inline", "columns": ["Name", "Joined", "Score"],
         "rows": [["A", "2026-01-05", "91.5"], ["B", "2026-02-11", "(12)"], ["C", None, ""]]},
    ],
}

PPTX_SPEC = {
    "filename": "sales_deck",
    "title": "Sales H1 2026",
    "theme": THEME,
    "slides": [
        {"layout": "title", "title": "Sales Performance — H1 2026", "subtitle": "What the portal data tells us",
         "text": "Prepared by Web Agent · Oct 2026", "notes": "Opening slide"},
        {"layout": "stats", "title": "H1 at a glance", "stats": [
            {"value": "₹1.2Cr", "label": "Revenue", "detail": "+18% vs H2 2025"},
            {"value": "62k", "label": "Units", "detail": "Across 5 regions"},
            {"value": "21%", "label": "Avg margin"},
            {"value": "30", "label": "Partners", "detail": "Ranked by score"}], "source": "Source: sales portal"},
        {"layout": "section", "title": "Where growth came from", "subtitle": "Regional and channel view"},
        {"layout": "bullets", "title": "Three drivers", "bullets": [
            "North region: Biggest absolute gain driven by new partner onboarding",
            "Online channel: Up 40% after checkout redesign",
            "Margins: Held at ~20% despite discounting"]},
        {"layout": "bullets", "title": "Dense bullet slide", "bullets": [
            "Point one", {"text": "Sub point", "level": 1}, "Point two", "Point three", "Point four", "Point five",
            "Point six with a much longer sentence to test wrapping behaviour across the slide width"]},
        {"layout": "chart", "title": "Channel trend", "chart": {"chart_type": "line", "data_ref": "monthly",
                                                                 "category_column": "Month"},
         "bullets": ["Online overtakes Retail in **Jul**", "Partner grows steadily"]},
        {"layout": "chart", "title": "Revenue share", "chart": {"chart_type": "doughnut", "categories": REGIONS,
                                                                 "series": [{"name": "Share", "values": [30, 20, 18, 22, 10]}]}},
        {"layout": "chart", "title": "Stacked columns", "chart": {"chart_type": "column", "stacked": True,
                                                                   "categories": ["Q1", "Q2"], "show_values": True,
                                                                   "series": [{"name": "A", "values": [1, 2]},
                                                                              {"name": "B", "values": [3, 1]}]}},
        {"layout": "table", "title": "Partner ranking", "table": {"data_ref": "ranking", "max_rows": 26}},
        {"layout": "two_column", "title": "Wins and risks",
         "left": {"heading": "Wins", "bullets": ["North +32%", "Online +40%"]},
         "right": {"heading": "Risks", "text": "Retail decline continues; partner concentration is high."}},
        {"layout": "image", "title": "Portal dashboard", "image": "screenshot:latest",
         "bullets": ["Captured automatically", "After each action"]},
        {"layout": "image", "title": "Full-width screenshot", "image": "dashboard"},
        {"layout": "timeline", "title": "Next steps", "steps": [
            {"title": "Validate", "text": "Confirm numbers with finance"}, {"title": "Share", "text": "Send deck"},
            {"title": "Automate", "text": "Schedule weekly scrape"}, {"title": "Scale", "text": "Add more portals"}]},
        {"layout": "quote", "text": "The goal is to turn data into information, and information into insight.",
         "quote_author": "Carly Fiorina"},
        {"layout": "chart", "title": "Broken chart falls back", "chart": {"data_ref": "missing"}},
        {"layout": "closing", "title": "Thank you", "subtitle": "Questions?", "bullets": ["web-agent", "Oct 2026"]},
    ],
}


# ---------------------------------------------------------------- checks
def check_docx(p: Path):
    from docx import Document
    d = Document(p)
    text = "\n".join(x.text for x in d.paragraphs)
    assert "Executive summary" in text and len(d.tables) >= 4 and len(d.inline_shapes) >= 4, "docx content"
    return f"{len(d.paragraphs)} paragraphs, {len(d.tables)} tables, {len(d.inline_shapes)} images"


def check_pdf(p: Path):
    from pypdf import PdfReader
    r = PdfReader(p)
    txt = "".join(pg.extract_text() for pg in r.pages)
    assert "Executive summary" in txt and "Page 2 of" in txt, "pdf text"
    assert "₹" in txt or "Helvetica" in str(r.pages[1].get("/Resources")), "unicode"
    return f"{len(r.pages)} pages, outline entries: {len(r.outline)}"


def check_xlsx(p: Path):
    from openpyxl import load_workbook
    wb = load_workbook(p)
    ws = wb.worksheets[0]
    assert isinstance(ws["C4"].value, int), f"Units parsed as number, got {ws['C4'].value!r}"
    assert isinstance(ws["H4"].value, str) and ws["H4"].value.startswith("00"), "SKU leading zeros kept"
    assert str(ws["I4"].value).startswith("="), "computed column formula"
    assert any(str(c.value).startswith("=SUBTOTAL") for c in ws[34]), "totals formulas"
    return f"sheets={wb.sheetnames}, tables={[list(w.tables) for w in wb.worksheets]}, charts={[len(w._charts) for w in wb.worksheets]}"


def check_pptx(p: Path):
    from pptx import Presentation
    prs = Presentation(p)
    n = len(prs.slides)
    assert n >= len(PPTX_SPEC["slides"]) + 2, f"table split expected, got {n} slides"
    return f"{n} slides"


def recalc(p: Path) -> str:
    script = Path("/mnt/skills/public/xlsx/scripts/recalc.py")
    if not script.exists() or not shutil.which("soffice"):
        return "skipped (LibreOffice recalc not available)"
    res = subprocess.run([sys.executable, str(script), str(p), "90"], capture_output=True, text=True, timeout=180)
    out = res.stdout.strip()
    data = json.loads(out[out.find("{"):]) if "{" in out else {}
    assert data.get("total_errors", 1) == 0, f"formula errors: {out}"
    return f"{data.get('total_formulas')} formulas, 0 errors"


def render(p: Path):
    if not shutil.which("soffice"):
        return
    prev = OUT / "preview"
    prev.mkdir(exist_ok=True)
    src = p
    if p.suffix != ".pdf":
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(prev), str(p)],
                       capture_output=True, timeout=180)
        src = prev / (p.stem + ".pdf")
    if src.exists() and shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-png", "-r", "50", str(src), str(prev / f"{p.stem}-{p.suffix[1:]}")],
                       capture_output=True, timeout=180)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shots = make_screenshots()
    assert to_columns_rows(SALES)[0][0] == "Month"

    jobs = [
        ("docx", lambda c: build_docx(DocumentSpec(**DOC_SPEC), OUT / "report.docx", c), check_docx),
        ("docx-landscape", lambda c: build_docx(DocumentSpec(**{**DOC_SPEC, "orientation": "landscape",
                                                                 "page_size": "Letter", "cover_page": False}),
                                                OUT / "report-landscape.docx", c), check_docx),
        ("pdf", lambda c: build_pdf(DocumentSpec(**DOC_SPEC), OUT / "report.pdf", c), check_pdf),
        ("pdf-landscape", lambda c: build_pdf(DocumentSpec(**{**DOC_SPEC, "orientation": "landscape",
                                                               "cover_page": False}),
                                              OUT / "report-landscape.pdf", c), check_pdf),
        ("xlsx", lambda c: build_xlsx(WorkbookSpec(**XLSX_SPEC), OUT / "sales_data.xlsx", c), check_xlsx),
        ("pptx", lambda c: build_pptx(PresentationSpec(**PPTX_SPEC), OUT / "sales_deck.pptx", c), check_pptx),
    ]
    failed = 0
    for name, build, check in jobs:
        ctx = context(shots)
        t0 = time.perf_counter()
        try:
            path = build(ctx)
            info = check(path)
            print(f"PASS {name:15s} {path.name:26s} {path.stat().st_size / 1024:7.1f} KB "
                  f"{time.perf_counter() - t0:5.2f}s  {info}")
            if ctx.warnings:
                print(f"     warnings: {ctx.warnings}")
            if name == "xlsx":
                print(f"     recalc: {recalc(path)}")
            if args.render:
                render(path)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(jobs) - failed}/{len(jobs)} passed. Output: {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
