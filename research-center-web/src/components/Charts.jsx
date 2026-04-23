import { useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, AreaChart, Area, Legend
} from "recharts";

const COLORS = ["#d92b2b", "#3b82f6", "#f59e0b", "#22c55e", "#8b5cf6", "#06b6d4"];

export default function Charts({ articles }) {
  const regionData = useMemo(() => {
    const counts = {};
    articles.forEach((a) => {
      counts[a.region] = (counts[a.region] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [articles]);

  const categoryData = useMemo(() => {
    const counts = {};
    articles.forEach((a) => {
      counts[a.category] = (counts[a.category] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [articles]);

  const timelineData = useMemo(() => {
    const counts = {};
    articles.forEach((a) => {
      counts[a.date] = (counts[a.date] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([date, count]) => ({ date, count }))
      .sort((a, b) => a.date.localeCompare(b.date));
  }, [articles]);

  const severityData = useMemo(() => {
    const counts = { high: 0, medium: 0, low: 0 };
    articles.forEach((a) => {
      counts[a.severity] = (counts[a.severity] || 0) + 1;
    });
    return [
      { name: "High", value: counts.high },
      { name: "Medium", value: counts.medium },
      { name: "Low", value: counts.low },
    ];
  }, [articles]);

  const total = articles.length;
  const highSeverity = articles.filter((a) => a.severity === "high").length;

  return (
    <section className="py-8 border-b border-border">
      <div className="mb-6">
        <h2 className="text-xl font-bold text-text-primary mb-1">Real-Time Intelligence Dashboard</h2>
        <p className="text-[13px] text-text-muted">
          {total} articles tracked · {highSeverity} high severity · 6 regions · 5 categories
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Articles by Region */}
        <div className="bg-bg-secondary border border-border rounded-xl p-5">
          <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider mb-4">Articles by Region</h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={regionData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e9ecef" />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#868e96" }} />
              <YAxis tick={{ fontSize: 11, fill: "#868e96" }} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "#fff", border: "1px solid #e9ecef", borderRadius: "8px", fontSize: "12px" }}
                cursor={{ fill: "rgba(0,0,0,0.03)" }}
              />
              <Bar dataKey="value" fill="#d92b2b" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Articles by Category */}
        <div className="bg-bg-secondary border border-border rounded-xl p-5">
          <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider mb-4">Articles by Category</h3>
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie
                data={categoryData}
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={80}
                paddingAngle={3}
                dataKey="value"
              >
                {categoryData.map((_, index) => (
                  <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ background: "#fff", border: "1px solid #e9ecef", borderRadius: "8px", fontSize: "12px" }}
              />
              <Legend verticalAlign="bottom" height={36} iconSize={8} iconType="circle" wrapperStyle={{ fontSize: "11px" }} />
            </PieChart>
          </ResponsiveContainer>
        </div>

        {/* Timeline */}
        <div className="bg-bg-secondary border border-border rounded-xl p-5">
          <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider mb-4">Publication Timeline</h3>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={timelineData}>
              <defs>
                <linearGradient id="colorCount" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#d92b2b" stopOpacity={0.15} />
                  <stop offset="95%" stopColor="#d92b2b" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#e9ecef" />
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#868e96" }} tickFormatter={(d) => d.slice(5)} />
              <YAxis tick={{ fontSize: 11, fill: "#868e96" }} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "#fff", border: "1px solid #e9ecef", borderRadius: "8px", fontSize: "12px" }}
              />
              <Area type="monotone" dataKey="count" stroke="#d92b2b" fillOpacity={1} fill="url(#colorCount)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Severity Distribution */}
        <div className="bg-bg-secondary border border-border rounded-xl p-5">
          <h3 className="text-[13px] font-bold text-text-primary uppercase tracking-wider mb-4">Severity Distribution</h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={severityData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="#e9ecef" />
              <XAxis type="number" tick={{ fontSize: 11, fill: "#868e96" }} allowDecimals={false} />
              <YAxis dataKey="name" type="category" tick={{ fontSize: 12, fill: "#495057" }} width={70} />
              <Tooltip
                contentStyle={{ background: "#fff", border: "1px solid #e9ecef", borderRadius: "8px", fontSize: "12px" }}
                cursor={{ fill: "rgba(0,0,0,0.03)" }}
              />
              <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                {severityData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.name === "High" ? "#d92b2b" : entry.name === "Medium" ? "#f59e0b" : "#22c55e"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  );
}
