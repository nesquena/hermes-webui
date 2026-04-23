import { categories } from "../data/mockData";

export default function FilterBar({ activeCategory, onCategoryChange }) {
  return (
    <div className="flex flex-wrap gap-2">
      {categories.map((cat) => (
        <button
          key={cat}
          onClick={() => onCategoryChange(cat)}
          className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
            activeCategory === cat
              ? "bg-accent text-white"
              : "bg-bg-card text-text-secondary hover:bg-bg-secondary hover:text-text-primary border border-border"
          }`}
        >
          {cat}
        </button>
      ))}
    </div>
  );
}
