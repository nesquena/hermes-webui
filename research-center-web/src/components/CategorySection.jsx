import ArticleCard from "./ArticleCard";

export default function CategorySection({ category, articles, onArticleClick, onCategoryClick }) {
  if (articles.length === 0) return null;

  const displayArticles = articles.slice(0, 4);
  const hasMore = articles.length > 4;

  return (
    <section className="py-6 border-t border-border animate-fade-in">
      <div className="flex items-center justify-between mb-5">
        <div className="flex items-center gap-3">
          <h2 className="text-lg sm:text-xl font-bold text-text-primary">{category}</h2>
          <span className="text-[11px] text-text-muted font-medium">{articles.length} articles</span>
        </div>
        {hasMore && (
          <button
            onClick={() => onCategoryClick(category)}
            className="text-[12px] text-text-muted hover:text-accent transition-colors flex items-center gap-1"
          >
            View all
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>
            </svg>
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 sm:gap-6">
        {displayArticles.map((article) => (
          <ArticleCard
            key={article.id}
            article={article}
            onClick={onArticleClick}
            variant="vertical"
          />
        ))}
      </div>
    </section>
  );
}
