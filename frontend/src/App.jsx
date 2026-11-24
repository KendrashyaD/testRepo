import { useEffect, useRef, useState } from 'react';
import Composer from './components/Composer.jsx';
import Message from './components/Message.jsx';
import { getInfo } from './api/chatStream';
import { useChat } from './hooks/useChat';

const EXAMPLES = [
  'Go to https://news.ycombinator.com, collect the top 30 stories with points and comments, and give me an Excel file with a chart.',
  'Open https://en.wikipedia.org/wiki/List_of_countries_by_GDP_(nominal), extract the main table and create a 5-slide deck on the top 10 economies.',
  'Find the official SAP BTP pricing page, summarise the service plans and produce a PDF brief.',
];

function PlanPanel({ plan }) {
  const [open, setOpen] = useState(true);
  if (!plan) return null;
  const lines = plan.split('\n').filter((l) => !/step\(s\) remaining|All steps complete/.test(l));
  return (
    <div className="border-b border-slate-200 bg-white">
      <div className="mx-auto max-w-3xl px-4 py-2 text-xs">
        <button type="button" onClick={() => setOpen((o) => !o)} className="font-medium text-slate-600">
          {open ? '▾' : '▸'} Plan
        </button>
        {open && (
          <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-slate-600">
            {lines.map((l, i) => (
              <li key={i} className={l.startsWith('[x]') ? 'text-emerald-700' : l.startsWith('[~]') ? 'text-indigo-700' : ''}>
                {l}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const { messages, running, status, threadId, plan, send, stop, reset } = useChat();
  const [info, setInfo] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    getInfo().then(setInfo);
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, status]);

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-3">
        <h1 className="text-sm font-semibold text-slate-800">Web Agent</h1>
        {info && (
          <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500">
            {info.model} · {info.headless ? 'headless' : 'headed'} · {info.mode} browser
          </span>
        )}
        {threadId && <span className="hidden text-[11px] text-slate-400 sm:inline">#{threadId.slice(0, 8)}</span>}
        <button
          type="button"
          onClick={reset}
          className="ml-auto rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          New chat
        </button>
      </header>

      <PlanPanel plan={plan} />

      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
          {messages.length === 0 ? (
            <div className="pt-16 text-center">
              <p className="text-sm text-slate-500">
                Describe a task. The agent browses, researches, analyses and can hand you Excel, Word, PDF or PowerPoint files.
              </p>
              <div className="mx-auto mt-6 grid max-w-xl gap-2">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => send(ex)}
                    className="rounded-md border border-slate-200 bg-white px-4 py-2.5 text-left text-sm text-slate-600 hover:border-slate-400"
                  >
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <Message key={m.id} message={m} status={i === messages.length - 1 ? status : null} />
            ))
          )}
          <div ref={bottomRef} />
        </div>
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto max-w-3xl px-4 py-3">
          <Composer running={running} onSend={send} onStop={stop} />
        </div>
      </footer>
    </div>
  );
}
