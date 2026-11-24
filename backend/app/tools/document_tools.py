"""LangChain tools that turn structured specs into Word / PDF / Excel / PowerPoint files."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from .docgen.common import GenContext
from .docgen.excel import build_xlsx
from .docgen.pdf import build_pdf
from .docgen.slides import build_pptx
from .docgen.specs import DocumentSpec, PresentationSpec, WorkbookSpec
from .docgen.word import build_docx
from .workspace import files_dir, load_data_ref, resolve_image, safe_filename, thread_id_from, unique_path

log = logging.getLogger(__name__)

MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}

COMMON_HINT = (
    "Large data: first store it in the scratchpad (extract_page_content or scratchpad_write) and reference it "
    "with data_ref instead of pasting rows. Images: 'screenshot:latest' (or -2, -3 ...) embeds auto-captured "
    "page screenshots. Inline markdown (**bold**, *italic*, `code`, [text](url)) works in text fields."
)


def _make_tool(name: str, description: str, schema: type, ext: str, builder: Callable) -> BaseTool:
    async def _run(config: RunnableConfig, **kwargs: Any):
        thread_id = thread_id_from(config)
        spec = schema(**kwargs)
        ctx = GenContext(data_loader=lambda key: load_data_ref(thread_id, key),
                         image_resolver=lambda ref: resolve_image(thread_id, ref))
        out = unique_path(files_dir(thread_id), safe_filename(spec.filename, ext))
        try:
            await asyncio.to_thread(builder, spec, out, ctx)
        except Exception as exc:  # noqa: BLE001 - give the model something actionable
            log.exception("%s failed", name)
            return f"Error: could not create {out.name}: {exc}. Fix the spec and call {name} again.", None
        size = out.stat().st_size
        info = {"name": out.name, "size": size, "kind": ext, "mime": MIME[ext],
                "url": f"/api/files/{thread_id}/{quote(out.name)}"}
        summary = f"Created {out.name} ({size / 1024:.1f} KB). It is shown to the user as a download link."
        summary += _describe(spec)
        if ctx.warnings:
            summary += "\nWarnings: " + "; ".join(ctx.warnings)
        return summary, {"file": info}

    return StructuredTool.from_function(
        coroutine=_run, name=name, description=f"{description}\n{COMMON_HINT}",
        args_schema=schema, response_format="content_and_artifact",
    )


def _describe(spec: Any) -> str:
    if isinstance(spec, WorkbookSpec):
        return " Sheets: " + ", ".join(s.name for s in spec.sheets) + "."
    if isinstance(spec, PresentationSpec):
        return f" Slides: {len(spec.slides)}."
    if isinstance(spec, DocumentSpec):
        return f" Blocks: {len(spec.blocks)}."
    return ""


def document_tools() -> list[BaseTool]:
    return [
        _make_tool(
            "create_word_document",
            "Create a Word (.docx) report. Blocks: heading, paragraph, bullets, numbered, table, chart, image, "
            "callout, quote, code, key_value, page_break, toc, divider. Supports cover page, header/footer, "
            "page numbers, landscape, theme colors.",
            DocumentSpec, "docx", build_docx),
        _make_tool(
            "create_pdf_document",
            "Create a PDF report with the same block model as create_word_document (headings get PDF bookmarks; "
            "a 'toc' block builds a real table of contents).",
            DocumentSpec, "pdf", build_pdf),
        _make_tool(
            "create_excel_workbook",
            "Create an Excel (.xlsx) workbook. Each sheet: columns (+types/number formats), rows or data_ref, "
            "Excel Table with filters, frozen header, live total formulas, computed formula columns "
            "({Header} placeholders), conditional formats, native charts, free-form cells/KPIs. Scraped strings like "
            "'₹1,234.50', '12%', '(45)' are converted to real numbers automatically.",
            WorkbookSpec, "xlsx", build_xlsx),
        _make_tool(
            "create_presentation",
            "Create a 16:9 PowerPoint (.pptx). Layouts: title, section, bullets, two_column, comparison, table "
            "(auto-splits long tables), chart (native, editable), image, stats, timeline, quote, closing. "
            "Use 'Heading: detail' bullets for card-style slides; add speaker notes via 'notes'.",
            PresentationSpec, "pptx", build_pptx),
    ]
