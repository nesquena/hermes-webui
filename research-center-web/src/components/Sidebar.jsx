import { formatDate, articleImage } from "../lib/utils";

export default function Sidebar({ articles, onArticleClick }) {
  return (
    <aside className="space-y-5">
      <div className="border border-border bg-bg-secondary rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider">Latest Posts</h3>
        </div>
        <div className="divide-y divide-border">
          {articles.slice(0, 8).map((article) => (
            <article
              key={article.id}
              onClick={() => onArticleClick(article)}
              className="flex gap-3 px-4 py-3 cursor-pointer hover:bg-bg-elevated transition-colors group"
            >
              <div className="shrink-0 w-16 h-12 overflow-hidden bg-bg-primary rounded">
                <img
                  src={articleImage(article.id, 120, 90)}
                  alt={article.title}
                  className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
                  loading="lazy"
                />
              </div>
              <div className="min-w-0">
                <h4 className="text-[13px] font-medium text-text-primary leading-snug line-clamp-2 group-hover:text-accent transition-colors">
                  {article.title}
                </h4>
                <div className="flex items-center gap-1.5 mt-1 text-[11px] text-text-muted">
                  <span>{article.source}</span>
                  <span className="text-text-dim">·</span>
                  <span>{formatDate(article.date)}</span>
                </div>
              </div>
            </article>
          ))}
        </div>
      </div>

      <div className="border border-border bg-bg-secondary rounded-lg p-4">
        <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider mb-3">Regions Covered</h3>
        <div className="flex flex-wrap gap-2">
          {["Americas", "Europe", "Japan", "China", "South Korea", "Russia"].map((region) => (
            <span
              key={region}
              className="px-2.5 py-1 text-[11px] text-text-secondary border border-border hover:border-accent hover:text-accent transition-colors cursor-default rounded"
            >
              {region}
            </span>
          ))}
        </div>
      </div>
    </aside>
  );
}
