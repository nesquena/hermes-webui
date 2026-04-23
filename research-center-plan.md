# AI Legal Tech Research Center - Master Plan

Goal: Aggregate, summarize, and cite AI-related news across 6 regions for legal tech research and victim assistance.

---

## Coverage

### Categories
1. AI Harms (deepfake fraud, scams, algorithmic bias, autonomous harm, misinformation, copyright)
2. Cybersecurity (attacks on AI infra, API breaches, local LLM vulns, supply chain)
3. Disruption (job displacement, market manipulation, political disinfo, election deepfakes)
4. AI/AGI Development & Governance (breakthroughs, open source releases, safety, regulation, treaties)
5. AI Technology & Infrastructure (local LLM, API cloud, AR/VR, neural interfaces, edge AI, robotics)

### Regions
- Americas (US focus + Canada, Brazil)
- Europe (EU + UK + Switzerland)
- Japan
- China
- South Korea
- Russia

---

## Architecture (Simple)

```
Sources → Fetch → Filter → Summarize → Cite → Store → Query
```

### Phase 1: MVP (Week 1-2)
- 2 regions: Americas + Europe (English sources easiest)
- 2 categories: AI Harms + Cybersecurity
- Storage: Markdown files organized by date/region/category
- Delivery: Local folder of daily digests

### Phase 2: Expansion (Week 3-4)
- Add Japan + South Korea (English sources first, then native if needed)
- Add remaining categories
- Storage: SQLite with simple schema
- Add tagging and search

### Phase 3: Full (Month 2+)
- Add China + Russia (requires curated sources, often paywalled or filtered)
- All categories + regions
- Optional: Simple web interface or API for querying
- Optional: Weekly report generation

---

## Data Sources by Region

### Americas
- English: TechCrunch, Ars Technica, Wired, The Verge, Reuters, Bloomberg Law, Lawfare
- US Gov: CISA advisories, FTC AI guidance, NIST AI RMF updates
- Academic: arXiv AI safety, OpenAI / Anthropic / Google safety blogs

### Europe
- English: Euractiv, EUobserver, Politico EU, European Commission AI updates
- Regulatory: EU AI Act implementation news, EDPS statements
- UK: ICO, DSIT updates, Alan Turing Institute

### Japan
- English: Japan Times tech section, Nikkei Asia, METI AI policy page
- Gov: Personal Information Protection Commission updates

### South Korea
- English: Korea Herald, Korea Times tech, KISA (Korea Internet & Security Agency)
- Gov: MSIT (Ministry of Science and ICT) AI policy updates

### China
- English: SCMP tech, Caixin Global, South China Morning Post AI section
- Policy: CAC (Cyberspace Administration) rules, MIIT guidelines
- Note: Heavy filtering required, often state-aligned narratives

### Russia
- English: Moscow Times, TASS tech (filtered), Bell (if accessible)
- Note: Very limited reliable English sources post-2022, may need monitoring of Western reports on Russian AI rather than domestic sources

---

## Pipeline Stages

### 1. Fetch
- RSS feeds (primary)
- News APIs: NewsAPI, GNews, or Event Registry (free tiers available)
- Manual source lists per region
- Frequency: Daily automated run

### 2. Filter
- Keyword matching across categories
- Language detection (prioritize English first, non-English later)
- Deduplication by URL/title
- Relevance scoring (keep > threshold)

### 3. Summarize
- Extract: headline, date, source, URL, key points
- Summarize in English even if source is non-English
- Tag with: region, category, subcategory, severity (if applicable)

### 4. Cite
- Full URL
- Source name
- Publication date
- Author if available
- Archive link if source unstable (optional)

### 5. Store

#### MVP: Markdown structure
```
research-center/
  2025-06-01/
    americas-ai-harms.md
    americas-cybersecurity.md
    europe-ai-harms.md
    europe-cybersecurity.md
```

#### Full: SQLite schema
```sql
articles (
  id INTEGER PRIMARY KEY,
  title TEXT,
  url TEXT UNIQUE,
  source TEXT,
  published_date DATE,
  summary TEXT,
  region TEXT,
  category TEXT,
  subcategory TEXT,
  fetched_at TIMESTAMP,
  hash TEXT -- content hash for dedup
)
```

### 6. Query / Output
- Daily digest per region
- Weekly cross-region synthesis
- Search by keyword, date range, category
- Export for legal team use

---

## Where Local Assistants Fit

- **Codex CLI**: Can help write/adjust fetch scripts, fix bugs, add new source parsers quickly
- **Kimi CLI**: Can help summarize non-English articles if needed, or generate report drafts
- **Hermes subagents**: Use only when processing large batches in parallel (e.g. backfilling 30 days of news from 6 regions at once)
- Rule: Don't delegate small tasks. Do directly. Delegate only bulk parallel work.

---

## First Action (This Week)

1. Set up MVP fetcher for Americas + Europe
2. Pick 5-10 RSS feeds per region
3. Run one-day test
4. Review output format with Kei
5. Adjust before expanding

---

## Risk & Notes

- **Non-English sources**: Summarize in English, keep original link
- **Paywalls**: Extract what available, mark paywall status
- **China/Russia**: Expect lower volume and higher bias; may need triangulation with Western analysis
- **Rate limits**: Respect robots.txt and API limits; add delays between requests
- **Legal sensitivity**: This is research database, not public publication; keep internal until reviewed
