export function timeAgo(dateStr) {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now - date;
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);
  const diffWeek = Math.floor(diffDay / 7);
  const diffMonth = Math.floor(diffDay / 30);

  if (diffSec < 60) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHour < 24) return `${diffHour}h ago`;
  if (diffDay < 7) return `${diffDay}d ago`;
  if (diffWeek < 4) return `${diffWeek}w ago`;
  if (diffMonth < 12) return `${diffMonth}mo ago`;
  return `${Math.floor(diffDay / 365)}y ago`;
}

export function formatDate(dateStr) {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function categoryLabel(category) {
  return category;
}

export function regionLabel(region) {
  return region;
}

export function categoryStyle(category) {
  return {
    color: "text-accent",
    bg: "bg-accent-bg",
    border: "border-accent/20",
    dot: "bg-accent",
  };
}

export function regionStyle(region) {
  return {
    color: "text-text-secondary",
    bg: "bg-bg-elevated",
    border: "border-border",
    dot: "bg-text-muted",
  };
}

export function severityStyle(severity) {
  switch (severity) {
    case "high":
      return { color: "text-accent", bg: "bg-accent-bg", border: "border-accent/20", dot: "bg-accent", pulse: true };
    case "medium":
      return { color: "text-warning", bg: "bg-warning/10", border: "border-warning/20", dot: "bg-warning", pulse: false };
    default:
      return { color: "text-success", bg: "bg-success/10", border: "border-success/20", dot: "bg-success", pulse: false };
  }
}

export function sourceInitials(source) {
  return source
    .split(/[\s&.]+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function sourceColor(source) {
  const colors = [
    "bg-text-muted/20 text-text-secondary",
    "bg-text-dim/30 text-text-muted",
    "bg-border text-text-secondary",
  ];
  let hash = 0;
  for (let i = 0; i < source.length; i++) hash = source.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

export function articleImage(articleId, width = 800, height = 500) {
  return `https://picsum.photos/seed/${articleId}/${width}/${height}`;
}
