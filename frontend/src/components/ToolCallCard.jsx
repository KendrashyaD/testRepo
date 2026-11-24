import { useState } from 'react';

const LABELS = {
  browser_navigate: 'Navigate',
  browser_navigate_back: 'Go back',
  browser_snapshot: 'Read page',
  browser_click: 'Click',
  browser_type: 'Type',
  browser_fill_form: 'Fill form',
  browser_select_option: 'Select option',
  browser_press_key: 'Press key',
  browser_hover: 'Hover',
  browser_drag: 'Drag',
  browser_wait_for: 'Wait',
  browser_tabs: 'Tabs',
  browser_evaluate: 'Run script',
  browser_file_upload: 'Upload file',
  browser_handle_dialog: 'Handle dialog',
  browser_close: 'Close browser',
  think: 'Think',
  update_plan: 'Update plan',
  web_search: 'Web search',
  extract_page_content: 'Extract data',
  scratchpad_write: 'Save note',
  scratchpad_read: 'Read note',
  scratchpad_list: 'List notes',
  scratchpad_delete: 'Delete note',
  data_query: 'Analyse data',
  create_word_document: 'Create Word',
  create_pdf_document: 'Create PDF',
  create_excel_workbook: 'Create Excel',
  create_presentation: 'Create PowerPoint',
};

const label = (name) =>
  LABELS[name] || name.replace(/^browser_/, '').replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

function summary(name, args = {}) {
  if (name === 'think') return args.thought;
  if (name === 'update_plan') return args.goal;
  if (args.query) return args.query;
  if (args.url) return args.url;
  if (args.filename) return args.filename;
  if (args.element) return args.element;
  if (args.save_as) return `${args.kind || ''} → ${args.save_as}`;
  if (args.data_ref) return args.data_ref;
  if (args.key) return args.key;
  if (args.fields) return `${args.fields.length} field(s)`;
  if (args.text) return `"${args.text}"`;
  if (args.time) return `${args.time}s`;
  if (args.action) return args.action;
  return '';
}

function StatusIcon({ status }) {
  if (status === 'running')
    return <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />;
  const color = { success: 'bg-emerald-500', error: 'bg-red-500', cancelled: 'bg-slate-400' }[status] || 'bg-slate-400';
  return <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} />;
}

export default function ToolCallCard({ call }) {
  const [open, setOpen] = useState(false);
  const hint = summary(call.name, call.args);
  const reasoning = call.name === 'think' || call.name === 'update_plan';

  return (
    <div className={`rounded-md border text-xs ${reasoning ? 'border-indigo-100 bg-indigo-50/40' : 'border-slate-200 bg-white'}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50/60"
      >
        <StatusIcon status={call.status} />
        <span className="font-medium text-slate-700">{label(call.name)}</span>
        {hint && <span className="min-w-0 truncate text-slate-500">{hint}</span>}
        <span className="ml-auto shrink-0 text-slate-400">{open ? 'Hide' : 'Details'}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-slate-100 px-3 py-2">
          <Block title="Tool">{call.name}</Block>
          <Block title="Arguments">{JSON.stringify(call.args ?? {}, null, 2)}</Block>
          {call.output && <Block title="Result">{call.output}</Block>}
        </div>
      )}
    </div>
  );
}

function Block({ title, children }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">{title}</div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-700">
        {children}
      </pre>
    </div>
  );
}
