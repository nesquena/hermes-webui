import { useState, useMemo } from "react";
import { articles } from "./data/mockData";
import Header from "./components/Header";
import HeroSection from "./components/HeroSection";
import CategorySection from "./components/CategorySection";
import ArticleCard from "./components/ArticleCard";
import Sidebar from "./components/Sidebar";
import Newsletter from "./components/Newsletter";
import Footer from "./components/Footer";
import ArticleDetail from "./components/ArticleDetail";
import Charts from "./components/Charts";

export default function App() {
  const [search, setSearch] = useState("");
  const [activeCategory, setActiveCategory] = useState("All");
  const [activeRegion, setActiveRegion] = useState("All");
  const [selectedArticle, setSelectedArticle] = useState(null);

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    return articles.filter((a) => {
      if (activeCategory !== "All" && a.category !== activeCategory) return false;
      if (activeRegion !== "All" && a.region !== activeRegion) return false;
      if (q) {
        const haystack = `${a.title} ${a.summary} ${a.source} ${a.category} ${a.region}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }, [search, activeCategory, activeRegion]);

  const grouped = useMemo(() => {
    const groups = {};
    for (const a of filtered) {
      if (!groups[a.category]) groups[a.category] = [];
      groups[a.category].push(a);
    }
    return groups;
  }, [filtered]);

  const heroArticle = filtered[0] || null;
  const sidebarArticles = [...filtered].sort((a, b) => new Date(b.date) - new Date(a.date)).slice(0, 10);

  const handleClearFilters = () => {
    setSearch("");
    setActiveCategory("All");
    setActiveRegion("All");
  };

  return (
    <div className="min-h-screen bg-bg-primary font-sans text-text-primary antialiased">
      <Header
        search={search}
        onSearchChange={setSearch}
        activeCategory={activeCategory}
        onCategoryChange={setActiveCategory}
        activeRegion={activeRegion}
        onRegionChange={setActiveRegion}
        resultCount={filtered.length}
        onClearFilters={handleClearFilters}
      />

      {activeCategory === "All" && activeRegion === "All" && !search && (
        <HeroSection article={heroArticle} onClick={setSelectedArticle} />
      )}

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        <Charts articles={filtered} />

        {activeCategory !== "All" || activeRegion !== "All" || search ? (
          <section className="py-6 animate-fade-in">
            <div className="flex items-center justify-between mb-5">
              <div className="flex items-center gap-3">
                <h2 className="text-lg sm:text-xl font-bold text-text-primary">
                  {search ? `Search Results` : activeCategory !== "All" ? activeCategory : activeRegion}
                </h2>
                <span className="text-[11px] text-text-muted font-medium">{filtered.length} articles</span>
              </div>
              {filtered.length === 0 && (
                <button
                  onClick={handleClearFilters}
                  className="text-[13px] text-text-muted hover:text-accent transition-colors"
                >
                  Reset filters
                </button>
              )}
            </div>

            {filtered.length === 0 ? (
              <div className="text-center py-16">
                <p className="text-text-muted text-[15px] mb-2">No articles found.</p>
                <p className="text-text-dim text-[13px]">Try adjusting your search or filters.</p>
              </div>
            ) : (
              <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 sm:gap-6">
                {filtered.map((article) => (
                  <ArticleCard
                    key={article.id}
                    article={article}
                    onClick={setSelectedArticle}
                    variant="vertical"
                  />
                ))}
              </section>
            )}
          </section>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
            <div className="lg:col-span-3">
              {Object.entries(grouped).map(([cat, catArticles]) => (
                <CategorySection
                  key={cat}
                  category={cat}
                  articles={catArticles}
                  onArticleClick={setSelectedArticle}
                  onCategoryClick={setActiveCategory}
                />
              ))}
            </div>
            <div className="lg:col-span-1">
              <Sidebar articles={sidebarArticles} onArticleClick={setSelectedArticle} />
            </div>
          </div>
        )}
      </main>

      <Newsletter />
      <Footer onCategoryClick={setActiveCategory} />

      {selectedArticle && (
        <ArticleDetail article={selectedArticle} onClose={() => setSelectedArticle(null)} />
      )}
    </div>
  );
}
