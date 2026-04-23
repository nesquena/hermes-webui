# AI Legal Tech Research Center — System Architecture

**Project:** AI-Legal Research Lab (PhD Project)  
**Goal:** Aggregate AI-related news globally, summarize in Thai with proper citation, for legal research and victim assistance.  
**Update Frequency:** Every 3 hours  
**LLM:** Gemma 4 26B (Local, via Ollama) — *READY*

---

## 1. ทำไมถึงเลือก SQLite สำหรับ PhD Project

ตัวเลือกที่พิจารณา:

| ฐานข้อมูล | ข้อดี | ข้อเสีย | เหมาะกับ |
|-----------|--------|---------|----------|
| **SQLite** | ไฟล์เดียว พกพาได้ ไม่ต้องติดตั้ง server สำรองง่าย ใช้กับ Python สะดวก | ไม่รองรับ concurrent write ดี | **PhD research (single user)** |
| PostgreSQL | robust, full-text search, concurrent | ต้องลง server, ซับซ้อน | Multi-user production |
| JSON files | ง่าย อ่านได้ตรงๆ | ค้นหาลำบาก ไม่มี index | ข้อมูลน้อยๆ |
| Markdown | อ่านง่าย | ไม่มี structure | Archive เท่านั้น |

**ข้อสรุป:** สำหรับ PhD project ที่เป็นศูนย์วิจัยส่วนตัว SQLite เป็น choice ที่ดีที่สุดครับ
- ไฟล์ `.db` เดียว ย้ายเครื่องได้สบาย
- สำรองด้วย `cp database.db backup.db`
- รองรับ full-text search (FTS5)
- ไม่ต้องดูแล server

---

## 2. Database Schema (SQLite + FTS5)

**หลักการออกแบบ:**
- `sources` ต้องสร้างก่อน `articles` (FK dependency)
- `articles` เก็บข้อมูลต้นฉบับทันทีที่ fetch (`llm_status='pending'`)
- `thai_summary` เป็น NULL ได้จนกว่า LLM จะสรุปเสร็จ
- Deduplication ใช้ `(source_id, content_hash)` ไม่ใช่ hash อย่างเดียว
- Versioning: `articles` = current version เสมอ, `article_versions` = history table
- `original_source` เป็น denormalized snapshot (ไม่ใช่ source of truth)

```sql
-- =====================================================
-- ตารางแรก: แหล่งข่าว (สร้างก่อนเพราะ articles อ้างอิง)
-- =====================================================
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    url TEXT,
    region TEXT,
    category_focus TEXT,                  -- หมวดที่สำนักข่าวเน้น
    rss_feed_url TEXT,
    api_endpoint TEXT,
    reliability_score REAL CHECK(reliability_score BETWEEN 0 AND 10),
    language TEXT DEFAULT 'en',
    is_active BOOLEAN DEFAULT 1,
    last_fetch_at TIMESTAMP,
    fetch_frequency_hours INTEGER DEFAULT 3,
    notes TEXT
);

-- Fallback row for unmapped sources (required because source_id is NOT NULL)
-- Inserted at migration time; resolve its generated id at runtime (don't hardcode id=0)
INSERT INTO sources (name, url, region, reliability_score, notes)
VALUES ('Unknown', NULL, NULL, 0, 'Fallback for articles where source cannot be mapped');

-- =====================================================
-- ตารางหลัก: บทความข่าว
-- =====================================================
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- ข้อมูลต้นฉบับ (ได้ทันทีหลัง fetch)
    original_title TEXT NOT NULL,
    original_url TEXT NOT NULL,
    original_source TEXT NOT NULL,        -- e.g., "Reuters" (snapshot, not FK)
    source_id INTEGER NOT NULL,           -- FK → sources(id). Never NULL; fallback to "unknown" source row
    original_source_url TEXT,             -- homepage ของสำนักข่าว
    original_language TEXT DEFAULT 'en',  -- en, ja, zh, ko, ru
    published_date TEXT,                  -- YYYY-MM-DD
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- ข้อมูลที่สรุปแล้ว (ภาษาไทย) — จะมีค่าหลัง LLM ประมวลผล
    thai_title TEXT,                      -- NULL ได้จนกว่า LLM จะเสร็จ
    thai_summary TEXT,                    -- NULL ได้ (llm_status บอกสถานะ)
    thai_analysis TEXT,                   -- วิเคราะห์เชิงลึก (legal angle)
    thai_keywords TEXT,                   -- คีย์เวิร์ดภาษาไทย คั่นด้วย comma
    
    -- การจัดหมวดหมู่
    region TEXT NOT NULL CHECK(region IN (
        'Americas', 'Europe', 'Japan', 'China', 'South Korea', 'Russia'
    )),
    category TEXT NOT NULL CHECK(category IN (
        'AI Harms', 'Cybersecurity', 'Disruption', 
        'AI/AGI Governance', 'AI Technology'
    )),
    subcategory TEXT,                     -- หมวดย่อย เช่น "Deepfake Fraud"
    severity TEXT CHECK(severity IN ('low', 'medium', 'high', 'critical')),
    
    -- Legal context (สำหรับ PhD) — ใช้ join table article_legal_frameworks เท่านั้น
    legal_relevance_score INTEGER CHECK(legal_relevance_score BETWEEN 1 AND 10),
    victim_types TEXT,                    -- ผู้เสียหายที่อาจได้รับผลกระทบ
    affected_jurisdictions TEXT,          -- เขตอำนาจศาลที่เกี่ยวข้อง
    
    -- Citation มาตรฐานวิชาการ (PhD-grade)
    citation_apa TEXT,
    citation_chicago TEXT,
    citation_mla TEXT,
    citation_bibtex TEXT,                 -- BibTeX for LaTeX
    
    -- Citation metadata (แยกเก็บอย่างละเอียด)
    citation_author TEXT,
    citation_publisher TEXT,
    citation_published_at TEXT,
    citation_accessed_at TEXT,
    citation_canonical_url TEXT,
    citation_archive_url TEXT,
    citation_doi TEXT,
    
    -- สถานะและ metadata
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'archived', 'flagged')),
    
    -- Verification (automated/source-level)
    verification_status TEXT DEFAULT 'unverified' CHECK(verification_status IN (
        'unverified', 'verified', 'disputed', 'retracted'
    )),
    verification_method TEXT,             -- cross_check, official_source, manual
    verified_by TEXT,
    verified_at TIMESTAMP,
    
    -- Human review (สำคัญสำหรับ legal/victim assistance)
    human_review_status TEXT DEFAULT 'needs_review' CHECK(human_review_status IN (
        'needs_review', 'in_review', 'reviewed', 'published', 'rejected'
    )),
    reviewed_by TEXT,
    reviewed_at TIMESTAMP,
    review_notes TEXT,
    
    -- Extraction metadata
    is_paywalled BOOLEAN DEFAULT 0,
    extraction_method TEXT DEFAULT 'full_content' CHECK(extraction_method IN (
        'full_content', 'rss_fallback', 'og_tags', 'api_summary'
    )),
    
    -- LLM processing lifecycle
    llm_status TEXT DEFAULT 'pending' CHECK(llm_status IN (
        'pending', 'processing', 'completed', 'failed', 'retry_queued'
    )),
    llm_error_count INTEGER DEFAULT 0,    -- นับ retry, max 3
    
    -- Deduplication
    content_hash TEXT,
    UNIQUE(source_id, content_hash),      -- same content from same source = skip
    UNIQUE(source_id, original_url),      -- identity: same article (URL) from same source = UPDATE, not INSERT
    
    -- สถิติ
    word_count_original INTEGER,
    word_count_thai INTEGER,
    llm_tokens_used INTEGER,
    
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

-- =====================================================
-- Full-Text Search index
-- =====================================================
CREATE VIRTUAL TABLE articles_fts USING fts5(
    thai_title,
    thai_summary,
    thai_analysis,
    thai_keywords,
    content='articles',
    content_rowid='id'
);

-- FTS5 triggers: index ONLY completed rows (avoid noise from pending/failed)
CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, thai_title, thai_summary, thai_analysis, thai_keywords)
    SELECT new.id, new.thai_title, new.thai_summary, new.thai_analysis, new.thai_keywords
    WHERE new.llm_status = 'completed';
END;

CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, thai_title, thai_summary, thai_analysis, thai_keywords)
    SELECT 'delete', old.id, old.thai_title, old.thai_summary, old.thai_analysis, old.thai_keywords
    WHERE old.llm_status = 'completed';
END;

CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
    -- delete old index entry only if it was previously indexed
    INSERT INTO articles_fts(articles_fts, rowid, thai_title, thai_summary, thai_analysis, thai_keywords)
    SELECT 'delete', old.id, old.thai_title, old.thai_summary, old.thai_analysis, old.thai_keywords
    WHERE old.llm_status = 'completed';
    -- re-insert only if new status is completed
    INSERT INTO articles_fts(rowid, thai_title, thai_summary, thai_analysis, thai_keywords)
    SELECT new.id, new.thai_title, new.thai_summary, new.thai_analysis, new.thai_keywords
    WHERE new.llm_status = 'completed';
END;

-- =====================================================
-- ตาราง: ประวัติการ fetch (audit log)
-- =====================================================
CREATE TABLE fetch_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_id INTEGER,
    articles_found INTEGER,
    articles_new INTEGER,
    articles_duplicated INTEGER,
    articles_failed INTEGER,
    llm_requests INTEGER,
    llm_tokens_total INTEGER,
    duration_seconds REAL,
    error_message TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

-- =====================================================
-- ตาราง: กฎหมาย/กรอบกฎหมายที่เกี่ยวข้อง
-- =====================================================
CREATE TABLE legal_frameworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    jurisdiction TEXT,
    effective_date TEXT,
    description TEXT,
    relevance_to_ai TEXT,
    official_url TEXT
);

-- =====================================================
-- ตารางเชื่อม: บทความ ↔ กรอบกฎหมาย
-- =====================================================
CREATE TABLE article_legal_frameworks (
    article_id INTEGER,
    framework_id INTEGER,
    relevance_note TEXT,
    PRIMARY KEY (article_id, framework_id),
    FOREIGN KEY (article_id) REFERENCES articles(id),
    FOREIGN KEY (framework_id) REFERENCES legal_frameworks(id)
);

-- =====================================================
-- ตาราง: บทความที่เกี่ยวข้อง (normalized)
-- =====================================================
CREATE TABLE related_articles (
    article_id INTEGER NOT NULL,
    related_article_id INTEGER NOT NULL,
    relation_type TEXT DEFAULT 'related' CHECK(relation_type IN (
        'related', 'same_event', 'follow_up', 'contradicts', 'confirms'
    )),
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (article_id, related_article_id),
    FOREIGN KEY (article_id) REFERENCES articles(id),
    FOREIGN KEY (related_article_id) REFERENCES articles(id)
);

-- =====================================================
-- ตาราง: verification evidence (audit trail)
-- เก็บว่า source ไหนยืนยัน/ขัดแย้ง claim ไหน
-- =====================================================
CREATE TABLE verification_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('confirms', 'contradicts', 'related', 'primary_source')),
    evidence_url TEXT,
    evidence_title TEXT,
    evidence_summary TEXT,
    evidence_date TEXT,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (article_id) REFERENCES articles(id),
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE INDEX idx_verification_evidence_article ON verification_evidence(article_id);

-- =====================================================
-- ตาราง: article versions (history table)
-- 
-- Design: articles เก็บ current version เสมอ
--         article_versions เก็บ snapshot เก่าเมื่อ content เปลี่ยน
-- =====================================================
CREATE TABLE article_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    original_title TEXT,
    original_url TEXT,
    extracted_text TEXT,                  -- raw extracted content snapshot
    extracted_text_path TEXT,             -- path to archived raw content file
    extraction_method TEXT,               -- full_content, rss_fallback, og_tags, api_summary
    source_timestamp TEXT,                -- timestamp from source (if available)
    thai_summary TEXT,
    thai_analysis TEXT,
    fetched_at TIMESTAMP,
    change_detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (article_id) REFERENCES articles(id)
);

-- =====================================================
-- Indexes
-- =====================================================
CREATE INDEX idx_articles_region ON articles(region);
CREATE INDEX idx_articles_category ON articles(category);
CREATE INDEX idx_articles_severity ON articles(severity);
CREATE INDEX idx_articles_date ON articles(published_date);
CREATE INDEX idx_articles_status ON articles(status);
CREATE INDEX idx_articles_fetched ON articles(fetched_at);
CREATE INDEX idx_articles_source_id ON articles(source_id);
CREATE INDEX idx_articles_llm_status ON articles(llm_status);
CREATE INDEX idx_articles_human_review ON articles(human_review_status);
CREATE INDEX idx_article_versions_article ON article_versions(article_id);
```

### Article Lifecycle (จาก fetch จนถึง publish)

```
fetch → compute content_hash → check identity (source_id + original_url)
         → IF exists AND hash unchanged → skip (duplicate)
         → IF exists AND hash changed → INSERT into article_versions → UPDATE articles
         → IF new → INSERT (llm_status='pending', thai_summary=NULL)
              →
         LLM summarize → UPDATE (llm_status='completed', thai_summary='...')
              →
         verification → populate verification_evidence (manual for MVP)
              →
         human_review_status='needs_review'
              →
         [human reviews] → 'reviewed' → 'published'
              →
         visible in UI + available for citation
```

**สำคัญ:** `thai_summary` เป็น NULL ได้จนกว่า LLM จะประมวลผลเสร็จ ไม่ใช่ NOT NULL

---

## 3. LLM Integration — Local Models (Ready)

### 3.1 Available Models

| Model | Size | Quant | RAM | Status |
|-------|------|-------|-----|--------|
| **Gemma 4 Abliterated** | 26B | q4_K | ~17 GB | Ready — primary for summarization |
| **Qwen 3.6 Abliterated** | 35B | q4 | ~23 GB | Ready — fallback for long-context / multilingual |
| **bge-m3** | — | — | ~1.2 GB | Ready — embeddings |

**Both models are already downloaded** in `/Users/kei/.ollama/models/` (40 GB total).

**Selection logic:**
- Default: Gemma 4 26B (faster, good Thai output)
- Fallback: Qwen 3.6 35B (longer context window, better at multilingual reasoning)
- Switch triggered by: token count > 8k, or Gemma fails 2+ times in a batch

### 3.2 Running via Ollama

```bash
# Start Ollama server (if not running)
ollama serve

# Test Thai summarization
ollama run huihui_ai/gemma-4-abliterated:26b-q4_K

# Or via API (OpenAI-compatible)
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "huihui_ai/gemma-4-abliterated:26b-q4_K",
    "messages": [{"role": "user", "content": "สวัสดี กรุณาตอบเป็นภาษาไทย"}]
  }'
```

**API endpoint for pipeline:** `http://localhost:11434/v1/chat/completions`

**Smoke test before pipeline use:**
```bash
# Verify model responds in Thai
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "huihui_ai/gemma-4-abliterated:26b-q4_K",
    "messages": [{"role": "user", "content": "สวัสดี กรุณาตอบเป็นภาษาไทย"}]
  }'
```
If output is incoherent or model errors, switch to Qwen fallback before running pipeline.

### 3.3 Prompt Template สำหรับสรุปข่าว (Thai)

```
คุณเป็นผู้เชี่ยวชาญด้านกฎหมายเทคโนโลยี (Legal Tech) และ AI Ethics

งานของคุณคือ อ่านข่าวต้นฉบับแล้วสรุปให้เข้าใจง่ายในภาษาไทย พร้อมวิเคราะห์มุมมองกฎหมาย

ข้อกำหนด:
1. สรุปเนื้อหาหลักในภาษาไทยที่เข้าใจง่าย แต่มีความละเอียดครบถ้วน
2. ระบุว่าเหตุการณ์นี้ส่งผลกระทบต่อผู้ใช้ AI หรือประชาชนทั่วไปอย่างไร
3. ระบุกรอบกฎหมายที่อาจเกี่ยวข้อง (ถ้ามี)
4. ให้คะแนนความรุนแรง (1-10) และระบุระดับ (ต่ำ/ปานกลาง/สูง/วิกฤต)
5. ระบุว่าข่าวนี้เกี่ยวข้องกับภูมิภาค/ประเทศใด

รูปแบบ output (JSON):
{
  "thai_title": "...",
  "thai_summary": "...",
  "legal_analysis": "...",
  "keywords": ["...", "..."],
  "severity_score": 7,
  "severity_level": "สูง",
  "legal_frameworks": ["EU AI Act", "GDPR"],
  "victim_types": ["ผู้บริโภค", "พนักงาน"],
  "affected_jurisdictions": ["สหภาพยุโรป", "สหรัฐอเมริกา"]
}

ข่าวต้นฉบับ:
[TITLE]
{original_title}

[CONTENT]
{article_content}
```

---

## 4. Pipeline Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Sources   │────▶│   Fetcher   │────▶│   Deduplic  │────▶│    LLM      │
│ (RSS/APIs)  │     │  (Python)   │     │  (Hashing)  │     │ (Gemma 4)   │
└─────────────┘     └─────────────┘     └─────────────┘     └──────┬──────┘
                                                                    │
┌─────────────┐     ┌─────────────┐     ┌─────────────┐            │
│     UI      │◀────│   SQLite    │◀────│   Store     │◀───────────┘
│  (Vite/Web) │     │   (FTS5)    │     │  (Python)   │
└─────────────┘     └─────────────┘     └─────────────┘
       ▲
       │
┌─────────────┐
│   Cronjob   │
│ (3 hours)   │
└─────────────┘
```

### 4.1 Components

| Component | Tech | หน้าที่ |
|-----------|------|---------|
| Fetcher | Python + `feedparser` + `requests` | ดึงข่าวจาก RSS/API |
| Deduplicator | Python + `hashlib` | ตรวจสอบข่าวซ้ำ + versioning |
| Summarizer | Python + `requests` → Ollama API | ส่งข่าวให้ Gemma สรุป (fallback: Qwen) |
| Store | Python + `sqlite3` | บันทึกลงฐานข้อมูล |
| Scheduler | `cronjob` (Hermes) | รันทุก 3 ชั่วโมง |
| UI | Vite + React | แสดงผลข่าว |

### 4.2 Workflow แต่ละรอบ

1. **Fetch** (5-10 นาที)
   - ดึง RSS feeds ทั้งหมด
   - ดึง News API (ถ้ามี)
   - รวบรวม metadata + compute content_hash

2. **Identity Check** (1-2 นาที)
   - Lookup by `(source_id, original_url)` → ถ้ามี: นี่คือ article เดิมที่อาจเปลี่ยน content
   - ถ้า hash ตรงกับของเดิม → skip (duplicate)
   - ถ้า hash ไม่ตรง → INSERT snapshot ลง `article_versions` → UPDATE `articles` ด้วย content ใหม่ → reset llm_status='pending'
   - ถ้าไม่พบ → INSERT ใหม่ (llm_status='pending')

3. **Filter** (1-2 นาที)
   - กรองเฉพาะที่เกี่ยวกับ AI/Legal
   - ตรวจ keywords

4. **Store (raw)** (1-2 นาที)
   - INSERT ลง `articles` (llm_status='pending', thai_summary=NULL)
   - FTS trigger จะ skip (เพราะ status ไม่ใช่ completed)

5. **Summarize** (20-40 นาที ขึ้นกับจำนวนข่าว)
   - ส่งแต่ละข่าวให้ Gemma สรุป (fallback เป็น Qwen ถ้า token > 8k)
   - UPDATE `articles` ด้วยผลลัพธ์ JSON
   - FTS trigger จะ index row นี้ (เพราะ status เป็น completed)
   - ถ้าล้มเหลว: llm_status='failed', llm_error_count++

6. **Verification** (manual for MVP)
   - ข่าว verified ต้อง cross-check ≥2 sources
   - เก็บ evidence ลง `verification_evidence`
   - สำหรับ MVP: skip ขั้นตอนนี้ ให้ verification_status='unverified' และใช้ human_review แทน

7. **Human Review Queue** (< 1 นาที)
   - บันทึกสถิติการทำงาน
   - ข่าวใหม่รอ human review (needs_review)

**รวมเวลาต่อรอบ:** ~30-60 นาที (ส่วนใหญ่เป็นการรอ LLM)

---

## 5. Citation Format (มาตรฐานวิชาการ)

สำหรับ PhD ต้องมี citation ที่ถูกต้อง:

### APA 7th
```
Reuters. (2025, April 15). EU AI Act enforcement begins: First fines issued.
Retrieved from https://www.reuters.com/...
```

### Chicago
```
Reuters. "EU AI Act Enforcement Begins: First Fines Issued."
Last modified April 15, 2025. https://www.reuters.com/...
```

### MLA 9th
```
"EU AI Act Enforcement Begins." Reuters, 15 Apr. 2025,
www.reuters.com/...
```

**ในระบบ:** สร้างอัตโนมัติจาก metadata ที่มี (source, date, title, url, author, publisher)

---

## 6. Source Verification Workflow

### Verification Rules

| Status | Condition |
|--------|-----------|
| **verified** | Cross-checked against ≥2 independent reliable sources, OR from official government/source |
| **disputed** | At least 1 reliable source contradicts the claim |
| **retracted** | Original source has published correction/retraction |
| **unverified** | Default; only 1 source or source reliability < 7 |

### Human Review Workflow

```
needs_review → in_review → reviewed → published
     ↓                ↓
   rejected         rejected
```

| State | Meaning |
|-------|---------|
| **needs_review** | LLM summary done, waiting for human verification |
| **in_review** | Being reviewed by human |
| **reviewed** | Human approved, not yet public |
| **published** | Approved and visible in UI |
| **rejected** | Human found issue, do not use |

**Important:** LLM summaries are NEVER "verified legal analysis" automatically. They must pass human review before being used for victim assistance or cited in research.

---

## 7. Error & Policy Handling

| Issue | Policy |
|-------|--------|
| **robots.txt blocks** | Respect and skip. Log as "blocked_by_robots" |
| **Paywall** | Extract title + summary from RSS/OG tags only. Mark `is_paywalled = true`. Do not bypass. |
| **Content extraction fails** | Use RSS description as fallback. Mark `extraction_method = "rss_fallback"` |
| **Canonical URL mismatch** | Use canonical URL if found in `<link rel="canonical">`. Otherwise use fetched URL. |
| **Source blocked (geo/IP)** | Retry once after 5min delay. If still blocked, skip and log. |
| **Rate limited** | Exponential backoff: 1min → 5min → 15min. Log wait time. |
| **Article changed after fetch** | Content hash detects change on next fetch. INSERT into `article_versions` (history), UPDATE `articles` with new content. |
| **LLM fails/hallucinates** | If JSON parsing fails or output is nonsensical, mark `llm_status = 'failed'` and queue for retry. Max 3 retries (`llm_error_count`). After 3 failures, flag for manual review. |
| **Source reliability < 5** | Flag for manual review. Do not auto-publish. |

---

## 8. File Structure

```
legal-research-center/
├── config/
│   ├── sources.yaml              # รายการแหล่งข่าว
│   ├── categories.yaml           # หมวดหมู่และ keywords
│   └── llm.yaml                  # LLM config (endpoint, prompt)
├── src/
│   ├── fetcher/
│   │   ├── __init__.py
│   │   ├── rss_fetcher.py        # ดึง RSS
│   │   ├── api_fetcher.py        # ดึง News API
│   │   └── base.py               # Base class
│   ├── processor/
│   │   ├── __init__.py
│   │   ├── deduplicator.py       # ตรวจซ้ำ
│   │   ├── filter.py             # กรอง relevance
│   │   └── content_extractor.py  # ดึงเนื้อหาจาก URL
│   ├── summarizer/
│   │   ├── __init__.py
|   |   ├── llm_client.py         # เรียก Ollama API
│   │   ├── prompt_templates.py   # Prompt ภาษาไทย
│   │   └── post_processor.py     # จัดรูปแบบผลลัพธ์
│   ├── database/
│   │   ├── __init__.py
│   │   ├── models.py             # Schema + ORM
│   │   ├── queries.py            # คำสั่งค้นหา
│   │   └── fts.py                # Full-text search
│   ├── citation/
│   │   ├── __init__.py
│   │   └── formatter.py          # สร้าง citation หลาย format
│   └── utils/
│       ├── __init__.py
│       ├── config_loader.py
│       └── logger.py
├── data/
│   ├── legal_research.db         # SQLite database
│   ├── backups/                  # สำรอง
│   └── exports/                  # ส่งออกรายงาน
├── pipeline.py                   # Script หลัก (เรียกทั้ง pipeline)
├── run.sh                        # รัน manual
├── requirements.txt
└── README.md
```

---

## 9. ขั้นตอนถัดไป

1. **สร้าง SQLite database** พร้อม schema ทั้งหมด (รวม fallback source row 'Unknown')
2. **สร้าง Python pipeline** (fetch → hash → dedup → store raw → summarize → update)
3. **ทดสอบ Thai summarization** ด้วย Gemma 4 26B ผ่าน Ollama API — ถ้าผลไม่ดี เลื่อนเป็น Qwen 3.6 35B
4. **ทดสอบสรุปข่าวตัวอย่าง** 1 รอบ
5. **ตั้ง cronjob** รันทุก 3 ชั่วโมง
6. **เชื่อมต่อกับ UI** (ดึงจาก SQLite แทน mock data)

**ที่รักอยากให้แชมินเริ่มจากขั้นตอนไหนครับ?**
