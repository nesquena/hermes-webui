import { timeAgo, regionStyle, categoryStyle, severityStyle, sourceInitials, sourceColor } from "../lib/utils";

export default function FeedCard({ article, onClick, index = 0 }) {
  const rStyle = regionStyle(article.region);
  const cStyle = categoryStyle(article.category);
  const sStyle = severityStyle(article.severity);
  const sInitials = sourceInitials(article.source);
  const sColor = sourceColor(article.source);

  return (
    <div
      onClick={onClick}
      className="group relative cursor-pointer overflow-hidden rounded-2xl border border-border-subtle bg-bg-card p-5 card-lift animate-fade-in"
      style={{ animationDelay: `${Math.min(index * 50, 500)}ms` }}
    >
      {/* Top accent line with glow on hover */}
      <div className="absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-transparent via-accent/40 to-transparent opacity-0 transition-all duration-500 group-hover:opacity-100" />
      <div className="absolute inset-x-0 top-0 h-20 bg-gradient-to-b from-accent/[0.07] to-transparent opacity-0 transition-all duration-500 group-hover:opacity-100 pointer-events-none" />

      {/* Corner glow effect */}
      <div className="absolute -right-10 -top-10 h-32 w-32 rounded-full bg-accent/[0.03] blur-3xl transition-all duration-500 group-hover:bg-accent/[0.08]" />

      {/* Header row: Region, Severity, Time */}
      <div className="relative mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider shadow-sm ${rStyle.bg} ${rStyle.color} ${rStyle.border}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full shadow-[0_0_6px_currentColor] ${rStyle.dot}`} />
            {article.region}
          </span>
          <span
            className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider shadow-sm ${sStyle.bg} ${sStyle.color} ${sStyle.border} ${sStyle.pulse ? "animate-pulse-glow" : ""}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full shadow-[0_0_6px_currentColor] ${sStyle.dot} ${sStyle.pulse ? "animate-pulse" : ""}`} />
            {article.severity}
          </span>
        </div>
        <span className="flex items-center gap-1.5 text-[11px] font-semibold text-text-muted">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          {timeAgo(article.date)}
        </span>
      </div>

      {/* Title */}
      <h3 className="relative mb-3 text-[15px] font-bold leading-snug tracking-tight text-text-primary transition-colors duration-300 group-hover:text-accent">
        {article.title}
      </h3>

      {/* Summary */}
      <p className="relative mb-6 text-[13px] leading-relaxed text-text-secondary line-clamp-3">
        {article.summary}
      </p>

      {/* Footer row: Category badge + Source avatar */}
      <div className="relative flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-bold shadow-sm ${cStyle.bg} ${cStyle.color} ${cStyle.border}`}
          >
            <span className={`h-1 w-1 rounded-full shadow-[0_0_4px_currentColor] ${cStyle.dot}`} />
            {article.category}
          </span>
        </div>
        <div className="flex items-center gap-2.5">
          <span className={`flex h-7 w-7 items-center justify-center rounded-full text-[10px] font-bold ring-2 ring-bg-card ${sColor}`}>
            {sInitials}
          </span>
          <span className="text-[11px] font-semibold text-text-muted">{article.source}</span>
        </div>
      </div>

      {/* Hover indicator arrow */}
      <div className="absolute bottom-5 right-5 opacity-0 transition-all duration-300 group-hover:opacity-100 group-hover:translate-x-0 translate-x-2">
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-accent/10 text-accent ring-1 ring-accent/20 backdrop-blur-sm">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 12h14" />
            <path d="m12 5 7 7-7 7" />
          </svg>
        </div>
      </div>
    </div>
  );
}
