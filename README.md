# Web Agent — LangChain `create_agent` + Playwright MCP + SAP AI Core

You give the agent a task in a one-page chat UI. It drives a real browser through the Playwright MCP server (via `langchain-mcp-adapters`) and reasons step by step with a plan, a think tool and a scratchpad. It can search the web when something is unclear. It can also produce **Excel, Word, PDF and PowerPoint** files.

Everything streams over plain HTTP; WebSockets are not used.

```
React + Vite + Tailwind ──POST /api/chat──▶ FastAPI ──▶ create_agent (ReAct, gpt-4.1 on SAP AI Core)
        ▲   (fetch + ReadableStream)            │          ├─ middleware: context budget, working memory,
        └──── text/event-stream ◀───────────────┘          │  loop guard, timeouts, auto-save, screenshots
                                                           ├─ Playwright MCP tools (long-lived session)
                                                           ├─ web_search / extract_page_content (composite, via MCP)
                                                           ├─ think / update_plan / scratchpad_* / data_query
                                                           └─ create_excel_workbook / create_word_document /
                                                              create_pdf_document / create_presentation
```

## Project layout

```
backend/
  run.py                      entry point (Windows-safe event loop)
  mcp_servers.json            your MCP config (npx @playwright/mcp@latest)
  app/
    config.py                 all settings (env / .env)
    llm.py                    SAP AI Core LLM via generative AI hub SDK
    mcp_session.py            long-lived MCP sessions, shared/per-thread switch
    middleware.py             context budget, working memory, loop guard, auto-save, screenshots
    prompts.py                reasoning + research + deliverables system prompt
    agent.py                  create_agent + tool wiring
    runner.py                 LangGraph stream → UI events (token, tool, plan, file, …)
    main.py                   FastAPI routes, file downloads, static UI
    tools/
      workspace.py            per-thread files dir + scratchpad store
      reasoning_tools.py      think, update_plan
      scratchpad_tools.py     scratchpad_write/read/list/delete, data_query
      research_tools.py       web_search, extract_page_content (built on MCP tools)
      document_tools.py       the 4 create_* LangChain tools
      docgen/                 pure-Python generators (python-docx, python-pptx, openpyxl, reportlab)
  tests/
    dry_run_docgen.py         complex dry run of all generators (+ optional LibreOffice render)
    e2e_fake_agent.py         offline end-to-end: SSE → agent → middleware → tools → MCP → downloads
    fake_playwright_mcp.py    stdio MCP server mimicking Playwright MCP (used by the e2e test)
frontend/                     React (JSX) + Vite + Tailwind v4 — chat, tool cards, plan panel, file cards
deploy/btp | kyma | azure     deployment descriptors
Dockerfile                    single image for BTP & ACA
```

## Run locally (no Docker)

```powershell
# Backend
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env           # fill AICORE_* values
python run.py                         # http://localhost:8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                           # http://localhost:5173
```

### Tests (no AI Core or browser needed)

```powershell
cd backend
python -m tests.dry_run_docgen          # builds 6 complex documents and asserts their contents
python -m tests.dry_run_docgen --render # also renders PNG previews (needs LibreOffice + poppler)
python -m tests.e2e_fake_agent          # full stack with a scripted LLM + fake Playwright MCP
```

Output is written to `backend/tests/out/`.

## New capabilities

### 1. Document tools

| Tool | Highlights |
|---|---|
| `create_excel_workbook` | Multiple sheets; typed columns. Scraped strings like `₹1,234.50`, `12%` or `(45)` become real numbers, while IDs with leading zeros stay text. Also: Excel Tables with filters, frozen headers, **totals as live `SUBTOTAL` formulas**, computed formula columns (`{Qty}*{Price}`), conditional formats (color scale, data bar, rules, top-N), native charts, KPI/free-form cells with comments, and a source notes line |
| `create_word_document` | Cover page, real TOC field, headers/footers, `Page X of Y`, headings, inline markdown with hyperlinks, nested bullets, numbered lists that restart, themed tables (repeat header, zebra, totals), charts, screenshots, callouts, quotes, code, key-value tables, page breaks, landscape |
| `create_pdf_document` | Same block model as Word, plus clickable TOC, PDF bookmarks and Unicode fonts (Arial/DejaVu auto-detected) |
| `create_presentation` | 16:9 themed deck. Layouts: title, section, bullets (card grid for 2–4 "Heading: detail" points), two_column/comparison, table (auto-split across slides), **native editable charts**, image, stats, timeline, quote, closing. Speaker notes and source lines |

- All four tools accept **`data_ref`**, a scratchpad key, so large data never travels through the LLM.
- Images use `screenshot:latest` (or `-2`, `-3`, …) to embed the auto-captured page screenshots.
- Files are saved to `artifacts/files/<thread_id>/` and shown in the UI as download cards (`GET /api/files/{thread}/{name}`).
- If one block, sheet or slide fails, it degrades gracefully: you get a warning instead of losing the whole file.

### 2. Scratchpad and working memory for large data

| Piece | What it does |
|---|---|
| `extract_page_content` | Collects **all** HTML tables and ARIA grids (e.g. SAP UI5), the page text, or the links in one call. Stores the full data and returns a preview plus a key. `append=true` handles paginated tables |
| `scratchpad_write` / `read` / `list` / `delete` | Notes and JSON data. `read` supports paging (offset/limit) and grep |
| `data_query` | Filter, select, group/aggregate, sort, dedupe and limit without reading the data; `save_as` stores the result for `data_ref` |
| Auto-save | Large raw outputs (`browser_evaluate`, network, console) are saved under `auto/...` keys |
| Working memory | Before **every** model call, the system message gets the current plan, the scratchpad index and the list of created files. Trimmed page snapshots therefore never lose the agent's state |

### 3. Reasoning and research

| Piece | What it does |
|---|---|
| `update_plan` | Checklist for multi-step tasks, shown live in the UI's plan panel |
| `think` | Explicit reasoning step: before complex decisions, after failures, and before producing deliverables |
| `web_search` | Google in a **separate tab**, falling back to Bing and then DuckDuckGo on captcha or no results. Returns titles, URLs and snippets, then restores the task tab. The prompt tells the agent to search whenever something is unclear instead of guessing or asking |
| Loop guard | After N identical consecutive calls, the agent is told to think and change approach |
| Verify step | Before answering, the prompt has the agent check every requirement, and the deliverable, against the original request |

## Key configuration (`backend/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODEL` / `LLM_MAX_TOKENS` | `gpt-4.1` / `8192` | Model; keep tokens high, because document specs are large |
| `AGENT_MAX_STEPS` | `60` | Loop guard for one user turn |
| `AUTO_SAVE_CHARS` | `12000` | Raw outputs above this size are fully saved to the scratchpad |
| `LOOP_GUARD_REPEATS` | `3` | Number of identical consecutive tool calls before the guard steps in |
| `WORKING_MEMORY_IN_PROMPT` | `true` | Inject plan, scratchpad index and files into each model call |
| `ENABLE_DOCUMENT_TOOLS` / `ENABLE_WEB_SEARCH` | `true` | Feature switches. The 4 document tool schemas add about 6k tokens per call |
| `BROWSER_ISOLATION` | `shared` | `shared` or `per_thread` |
| `PW_HEADLESS` / `PW_BROWSER` | `true` / MCP default | Browser mode and engine |
| `ALLOWED_ORIGINS` | empty (= all) | If you set this, add search engine hosts for `web_search` |
| `SCREENSHOT_MODE` | `after_action` | Screenshots go to disk only; they can be embedded in documents |
| `DISABLE_SSL_VERIFY` | `true` | Corporate SSL inspection |

## Example prompts

- *"Go to `<report URL>`, review the report, and give me the data as an Excel file with totals and a chart by region."*
- *"Compare the pricing pages of X and Y and produce a 6-slide deck with a comparison table."*
- *"Open our status page, summarise last month's incidents and create a PDF brief with a screenshot."*

## Streaming API

`POST /api/chat` returns `text/event-stream` with these events:

- `meta`
- `status`
- `token`
- `tool_start`
- `tool_end`
- `plan`
- `file`
- `error`
- `done`

A heartbeat is sent every 15 s.

Other endpoints:

- `POST /api/chat/{thread}/cancel`
- `DELETE /api/threads/{thread}` clears the history, scratchpad and browser (per_thread mode)
- `GET /api/threads/{thread}/files`
- `GET /api/files/{thread}/{name}`
- `GET /api/info`
- `GET /api/health`

## Deployment

The process is unchanged from the previous version: use the root `Dockerfile`, then `deploy/btp/manifest.yml`, `deploy/kyma/web-agent.yaml` or `deploy/azure/deploy-aca.sh`.

What's new in the image:

- `fonts-dejavu-core`, so PDFs render Unicode
- matplotlib's headless backend
- 3 GB of memory on CF

Generated files live in `artifacts/`. Mount a volume (PVC or Azure Files) if they must survive restarts. Keep **1 instance** while memory and the scratchpad are in-process.

## Roadmap hooks

- **Redis:** use a Redis checkpointer in `agent.get_checkpointer()`. `ScratchpadStore` in `tools/workspace.py` has a small interface (`write`/`get`/`delete`/`keys`/`index`/`clear`) that can be re-implemented on Redis.
- **Multi-user:** add XSUAA or Entra ID, prefix `thread_id` with the user id, and set `BROWSER_ISOLATION=per_thread`.
- **Object storage for files:** swap `files_dir()` for BTP Object Store or Azure Blob.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `web_search` always falls back to Bing or DuckDuckGo | Google often shows a captcha to headless browsers; the fallback is expected. Use `PW_HEADLESS=false` locally if you need Google |
| `data_ref '…' not found` | The agent used a wrong key. The error lists the available keys and the agent retries |
| CJK characters show as boxes in PDF | Install a CJK font (e.g. Noto Sans CJK) and add it to `FONT_CANDIDATES` in `docgen/pdf.py` |
| Word TOC shows placeholder text | Word asks to update fields when the file opens; accept, or right-click the TOC and choose **Update Field** |
| `Playwright MCP failed to start` | Run `npx -y @playwright/mcp@latest --help` manually to see the real error |
