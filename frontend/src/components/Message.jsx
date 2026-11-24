import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import FileCard from './FileCard.jsx';
import ToolCallCard from './ToolCallCard.jsx';

function Markdown({ text }) {
  return (
    <div className="md text-sm leading-relaxed text-slate-800">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function TypingDots() {
  return (
    <div className="flex gap-1 py-1">
      {[0, 150, 300].map((d) => (
        <span key={d} className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: `${d}ms` }} />
      ))}
    </div>
  );
}

export default function Message({ message, status }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-slate-800 px-4 py-2 text-sm text-white">
          {message.parts[0]?.text}
        </div>
      </div>
    );
  }

  const streaming = message.status === 'streaming';
  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-slate-200 text-xs font-semibold text-slate-600">
        AI
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        {message.parts.map((part, i) =>
          part.type === 'tool' ? (
            <ToolCallCard key={part.id || i} call={part} />
          ) : (
            <Markdown key={i} text={part.text} />
          ),
        )}
        {message.files?.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2">
            {message.files.map((f) => (
              <FileCard key={f.url} file={f} />
            ))}
          </div>
        )}
        {streaming && (status ? <p className="text-xs text-slate-500">{status}</p> : message.parts.length === 0 && <TypingDots />)}
        {message.status === 'cancelled' && <p className="text-xs text-slate-400">Stopped.</p>}
        {message.error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{message.error}</div>
        )}
      </div>
    </div>
  );
}
