export default function SearchBar({ value, onChange }) {
  return (
    <div className="relative w-full group">
      <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none">
        <svg
          className="text-text-muted transition-all duration-300 group-focus-within:text-accent group-focus-within:scale-110"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="11" cy="11" r="8" />
          <path d="m21 21-4.3-4.3" />
        </svg>
      </div>
      <input
        type="text"
        placeholder="Search intelligence..."
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-border-subtle bg-bg-card py-2.5 pl-10 pr-10 text-sm font-medium text-text-primary placeholder:text-text-muted/60 outline-none transition-all duration-300 focus:border-accent/40 focus:bg-bg-elevated focus:shadow-[0_0_30px_-4px_rgba(167,139,250,0.2)] hover:border-border hover:bg-bg-elevated/50"
      />
      {value && (
        <button
          onClick={() => onChange("")}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-primary transition-all duration-200 rounded-md p-1 hover:bg-bg-elevated hover:scale-110"
          aria-label="Clear search"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 6 6 18" />
            <path d="m6 6 12 12" />
          </svg>
        </button>
      )}
    </div>
  );
}
