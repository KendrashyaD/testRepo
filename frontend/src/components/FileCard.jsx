const KIND = {
  xlsx: { label: 'XLSX', color: 'bg-emerald-600' },
  docx: { label: 'DOCX', color: 'bg-blue-600' },
  pptx: { label: 'PPTX', color: 'bg-orange-600' },
  pdf: { label: 'PDF', color: 'bg-red-600' },
};

const size = (bytes) => (bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

export default function FileCard({ file }) {
  const kind = KIND[file.kind] || { label: (file.kind || 'FILE').toUpperCase(), color: 'bg-slate-600' };
  return (
    <a
      href={file.url}
      download={file.name}
      className="flex items-center gap-3 rounded-md border border-slate-200 bg-white px-3 py-2 hover:border-slate-400"
    >
      <span className={`rounded px-1.5 py-1 text-[10px] font-bold text-white ${kind.color}`}>{kind.label}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-slate-800">{file.name}</span>
        <span className="block text-[11px] text-slate-500">{size(file.size)}</span>
      </span>
      <span className="text-xs font-medium text-slate-600">Download</span>
    </a>
  );
}
