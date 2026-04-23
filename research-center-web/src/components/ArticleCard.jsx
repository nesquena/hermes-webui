import { formatDate, articleImage } from "../lib/utils";

export default function ArticleCard({ article, onClick, variant = "horizontal" }) {
  const isVertical = variant === "vertical";

  return (
    <article
      onClick={() => onClick(article)}
      className={`group cursor-pointer ${
        isVertical
          ? "flex flex-col"
          : "flex gap-4 sm:gap-5"
      }`}
    >
      <div className={`shrink-0 overflow-hidden bg-bg-secondary rounded-lg ${isVertical ? "w-full aspect-[16/10] mb-3" : "w-28 h-20 sm:w-36 sm:h-24"}`}>
        <img
          src={articleImage(article.id, isVertical ? 600 : 300, isVertical ? 375 : 200)}
          alt={article.title}
          className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
          loading="lazy"
        />
      </div>

      <div className="flex-1 min-w-0">
        <span className="inline-block text-[11px] font-bold text-accent uppercase tracking-wider mb-1.5">
          {article.category}
        </span>
        <h3 className={`font-semibold text-text-primary leading-snug group-hover:text-accent transition-colors ${
          isVertical ? "text-[15px] line-clamp-2" : "text-[14px] line-clamp-2 sm:line-clamp-3"
        }`}>
          {article.title}
        </h3>
        <div className="flex items-center gap-2 mt-2 text-[11px] sm:text-[12px] text-text-muted">
          <span>{article.source}</span>
          <span className="text-text-dim">·</span>
          <span>{formatDate(article.date)}</span>
        </div>
      </div>
    </article>
  );
}
