import { formatDate, articleImage } from "../lib/utils";

export default function HeroSection({ article, onClick }) {
  if (!article) return null;

  return (
    <section className="relative w-full overflow-hidden cursor-pointer group" onClick={() => onClick(article)}>
      <div className="relative h-[320px] sm:h-[400px] lg:h-[460px]">
        <img
          src={articleImage(article.id, 1200, 600)}
          alt={article.title}
          className="absolute inset-0 w-full h-full object-cover transition-transform duration-700 group-hover:scale-105"
          loading="eager"
        />
        <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/50 to-black/10" />
        <div className="absolute inset-0 bg-gradient-to-r from-black/60 to-transparent" />
      </div>

      <div className="absolute inset-0 flex items-end">
        <div className="max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 pb-8 sm:pb-10">
          <div className="max-w-3xl">
            <span className="inline-block px-3 py-1 bg-accent text-white text-[11px] font-bold uppercase tracking-wider mb-3">
              {article.category}
            </span>
            <h2 className="font-serif text-2xl sm:text-3xl lg:text-4xl font-bold text-white leading-tight mb-3 group-hover:underline decoration-accent underline-offset-4">
              {article.title}
            </h2>
            <p className="text-sm sm:text-base text-gray-200 leading-relaxed mb-4 line-clamp-2 max-w-2xl hidden sm:block">
              {article.summary}
            </p>
            <div className="flex items-center gap-3 text-[13px] text-gray-300">
              <span className="font-semibold text-white">{article.source}</span>
              <span className="text-gray-500">|</span>
              <span>{formatDate(article.date)}</span>
              <span className="text-gray-500">|</span>
              <span>{article.region}</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
