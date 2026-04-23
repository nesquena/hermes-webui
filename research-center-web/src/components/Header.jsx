import { useState } from "react";
import { categories, regions } from "../data/mockData";

export default function Header({
  search,
  onSearchChange,
  activeCategory,
  onCategoryChange,
  activeRegion,
  onRegionChange,
  resultCount,
  onClearFilters,
}) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const hasFilters = activeRegion !== "All" || activeCategory !== "All" || search;

  return (
    <header className="sticky top-0 z-50 bg-white/95 border-b border-border backdrop-blur-sm">
      {/* Top bar */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Mobile hamburger */}
          <button
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
            className="lg:hidden p-2 -ml-2 text-text-secondary hover:text-text-primary transition-colors"
            aria-label="Menu"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {mobileMenuOpen ? (
                <><path d="M18 6 6 18"/><path d="m6 6 12 12"/></>
              ) : (
                <><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="18" y2="18"/></>
              )}
            </svg>
          </button>

          {/* Logo */}
          <a href="/" className="flex items-center gap-3 shrink-0" onClick={(e) => { e.preventDefault(); onClearFilters(); }}>
            <div className="hidden sm:flex h-9 w-9 items-center justify-center bg-accent text-white font-bold text-sm">
              AI
            </div>
            <div className="flex flex-col">
              <h1 className="font-serif text-lg sm:text-xl font-bold tracking-tight text-text-primary leading-none">
                AI Research Center
              </h1>
              <span className="text-[10px] sm:text-[11px] text-text-muted tracking-widest uppercase leading-none mt-0.5">
                Legal Tech & Policy
              </span>
            </div>
          </a>

          {/* Desktop Category Nav */}
          <nav className="hidden lg:flex items-center gap-1 ml-8">
            {categories.map((cat) => (
              <button
                key={cat}
                onClick={() => onCategoryChange(cat)}
                className={`px-3 py-1.5 text-[13px] font-medium transition-colors rounded-md ${
                  activeCategory === cat
                    ? "text-accent bg-accent-bg"
                    : "text-text-secondary hover:text-text-primary hover:bg-bg-elevated"
                }`}
              >
                {cat}
              </button>
            ))}
          </nav>

          {/* Search + Meta */}
          <div className="flex items-center gap-3 ml-auto">
            <div className="relative hidden sm:block w-52">
              <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                <svg className="text-text-muted" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
                </svg>
              </div>
              <input
                type="text"
                placeholder="Search news..."
                value={search}
                onChange={(e) => onSearchChange(e.target.value)}
                className="w-full bg-bg-secondary border border-border rounded-lg py-2 pl-9 pr-8 text-[13px] text-text-primary placeholder:text-text-muted outline-none transition-colors focus:border-accent focus:bg-white"
              />
              {search && (
                <button
                  onClick={() => onSearchChange("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-primary transition-colors"
                  aria-label="Clear search"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M18 6 6 18"/><path d="m6 6 12 12"/>
                  </svg>
                </button>
              )}
            </div>

            <div className="hidden md:flex items-center gap-2 text-[11px] text-text-muted bg-bg-secondary rounded-full px-3 py-1.5">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
              </span>
              <span className="font-medium">{resultCount} articles</span>
            </div>
          </div>
        </div>
      </div>

      {/* Desktop Region Bar */}
      <div className="hidden lg:block border-t border-border bg-bg-secondary/50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-1 h-9">
            <span className="text-[11px] text-text-muted uppercase tracking-wider mr-2 font-semibold">Regions:</span>
            {regions.map((r) => (
              <button
                key={r}
                onClick={() => onRegionChange(r)}
                className={`px-2.5 py-0.5 text-[12px] transition-colors rounded ${
                  activeRegion === r
                    ? "text-accent font-semibold bg-accent-bg"
                    : "text-text-muted hover:text-text-secondary"
                }`}
              >
                {r}
              </button>
            ))}
            {hasFilters && (
              <button
                onClick={onClearFilters}
                className="ml-auto text-[11px] text-text-muted hover:text-accent transition-colors flex items-center gap-1"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>
                </svg>
                Reset
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Mobile Menu */}
      {mobileMenuOpen && (
        <div className="lg:hidden border-t border-border bg-bg-secondary animate-fade-in">
          <div className="px-4 py-4 space-y-4">
            <div className="relative sm:hidden">
              <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                <svg className="text-text-muted" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
                </svg>
              </div>
              <input
                type="text"
                placeholder="Search news..."
                value={search}
                onChange={(e) => onSearchChange(e.target.value)}
                className="w-full bg-white border border-border py-2.5 pl-9 pr-4 text-[13px] text-text-primary placeholder:text-text-muted outline-none focus:border-accent"
              />
            </div>

            <div>
              <h3 className="text-[11px] text-text-muted uppercase tracking-wider mb-2 font-semibold">Categories</h3>
              <div className="flex flex-wrap gap-2">
                {categories.map((cat) => (
                  <button
                    key={cat}
                    onClick={() => { onCategoryChange(cat); setMobileMenuOpen(false); }}
                    className={`px-3 py-1.5 text-[13px] border rounded-md transition-colors ${
                      activeCategory === cat
                        ? "border-accent text-accent bg-accent-bg"
                        : "border-border text-text-secondary hover:border-border-hover hover:text-text-primary"
                    }`}
                  >
                    {cat}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <h3 className="text-[11px] text-text-muted uppercase tracking-wider mb-2 font-semibold">Regions</h3>
              <div className="flex flex-wrap gap-2">
                {regions.map((r) => (
                  <button
                    key={r}
                    onClick={() => { onRegionChange(r); setMobileMenuOpen(false); }}
                    className={`px-3 py-1.5 text-[13px] border rounded-md transition-colors ${
                      activeRegion === r
                        ? "border-accent text-accent bg-accent-bg"
                        : "border-border text-text-secondary hover:border-border-hover hover:text-text-primary"
                    }`}
                  >
                    {r}
                  </button>
                ))}
              </div>
            </div>

            {hasFilters && (
              <button
                onClick={() => { onClearFilters(); setMobileMenuOpen(false); }}
                className="w-full py-2.5 text-[13px] font-medium text-text-secondary border border-border hover:border-accent hover:text-accent transition-colors rounded-md"
              >
                Reset Filters
              </button>
            )}
          </div>
        </div>
      )}
    </header>
  );
}
