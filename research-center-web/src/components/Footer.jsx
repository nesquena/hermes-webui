import { categories, regions } from "../data/mockData";

export default function Footer({ onCategoryClick }) {
  return (
    <footer className="bg-bg-secondary border-t border-border">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 sm:py-14">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8 lg:gap-12">
          <div className="sm:col-span-2 lg:col-span-1">
            <h3 className="font-serif text-xl font-bold text-text-primary mb-2">
              AI Research Center
            </h3>
            <p className="text-[11px] text-text-muted uppercase tracking-widest mb-4">
              Legal Tech & Policy
            </p>
            <p className="text-[13px] text-text-secondary leading-relaxed">
              Aggregating AI legal and technology news from trusted sources worldwide. Research-grade intelligence for policymakers and legal professionals.
            </p>
          </div>

          <div>
            <h4 className="text-[12px] font-bold text-text-primary uppercase tracking-wider mb-4">Categories</h4>
            <ul className="space-y-2">
              {categories.slice(1).map((cat) => (
                <li key={cat}>
                  <button
                    onClick={() => onCategoryClick(cat)}
                    className="text-[13px] text-text-secondary hover:text-accent transition-colors"
                  >
                    {cat}
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4 className="text-[12px] font-bold text-text-primary uppercase tracking-wider mb-4">Regions</h4>
            <ul className="space-y-2">
              {regions.slice(1).map((r) => (
                <li key={r}>
                  <span className="text-[13px] text-text-secondary">{r}</span>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4 className="text-[12px] font-bold text-text-primary uppercase tracking-wider mb-4">Contact</h4>
            <ul className="space-y-2 text-[13px] text-text-secondary">
              <li>contact@ai-research.org</li>
              <li>Bangkok, Thailand</li>
            </ul>
          </div>
        </div>

        <div className="mt-10 pt-6 border-t border-border flex flex-col sm:flex-row items-center justify-between gap-3">
          <p className="text-[12px] text-text-muted">
            &copy; {new Date().getFullYear()} AI Legal Tech Research Center. All rights reserved.
          </p>
          <p className="text-[11px] text-text-dim">
            Data from international news sources
          </p>
        </div>
      </div>
    </footer>
  );
}
