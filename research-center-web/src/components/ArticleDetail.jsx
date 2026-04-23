import { useEffect } from "react";
import { formatDate, categoryLabel, regionLabel, articleImage } from "../lib/utils";

export default function ArticleDetail({ article, onClose }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  if (!article) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/80 overflow-y-auto animate-fade-in"
      onClick={onClose}
    >
      <div
        className="min-h-screen flex items-start justify-center p-4 sm:p-8"
        onClick={(e) => e.stopPropagation()}
      >
        <article className="w-full max-w-3xl bg-white border border-border rounded-xl shadow-2xl animate-fade-in overflow-hidden">
          <button
            onClick={onClose}
            className="fixed top-4 right-4 z-50 flex h-10 w-10 items-center justify-center bg-white border border-border text-text-muted hover:text-text-primary hover:border-accent transition-colors rounded-lg"
            aria-label="Close"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6 6 18"/><path d="m6 6 12 12"/>
            </svg>
          </button>

          <div className="relative h-56 sm:h-72 overflow-hidden">
            <img
              src={articleImage(article.id, 900, 450)}
              alt={article.title}
              className="w-full h-full object-cover"
            />
            <div className="absolute inset-0 bg-gradient-to-t from-white via-white/30 to-transparent" />
          </div>

          <div className="px-6 sm:px-10 pb-10 -mt-12 relative">
            <span className="inline-block px-3 py-1 bg-accent text-white text-[11px] font-bold uppercase tracking-wider mb-4 rounded">
              {article.category}
            </span>

            <h1 className="font-serif text-2xl sm:text-3xl lg:text-4xl font-bold text-text-primary leading-tight mb-5">
              {article.title}
            </h1>

            <div className="flex flex-wrap items-center gap-3 text-[13px] text-text-muted mb-8 pb-6 border-b border-border">
              <span className="font-semibold text-text-primary">{article.source}</span>
              <span className="text-text-dim">|</span>
              <span>{regionLabel(article.region)}</span>
              <span className="text-text-dim">|</span>
              <span>{formatDate(article.date)}</span>
              <span className="text-text-dim">|</span>
              <span className={`text-[11px] font-bold uppercase tracking-wider ${
                article.severity === "high" ? "text-accent" : "text-text-muted"
              }`}>
                {article.severity === "high" ? "HIGH SEVERITY" : article.severity === "medium" ? "MEDIUM" : "LOW"}
              </span>
            </div>

            <div className="prose max-w-none">
              <p className="text-base sm:text-lg text-text-secondary leading-relaxed mb-6">
                {article.summary}
              </p>

              <div className="space-y-4 text-[15px] text-text-secondary leading-relaxed">
                <p>
                  This article was sourced from {article.source} covering developments in {regionLabel(article.region)}. The information has been aggregated for research purposes and does not constitute legal advice.
                </p>
                <p>
                  Full article available at{" "}
                  <a href={article.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                    {article.source}
                  </a>
                </p>
              </div>
            </div>

            <div className="mt-10 pt-6 border-t border-border flex flex-wrap items-center gap-4">
              <a
                href={article.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent text-white text-[13px] font-bold uppercase tracking-wider hover:bg-accent-hover transition-colors rounded-lg"
              >
                Read Original
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/>
                </svg>
              </a>
              <button
                onClick={onClose}
                className="px-5 py-2.5 border border-border text-text-secondary text-[13px] font-medium hover:border-accent hover:text-accent transition-colors rounded-lg"
              >
                Close
              </button>
            </div>
          </div>
        </article>
      </div>
    </div>
  );
}
