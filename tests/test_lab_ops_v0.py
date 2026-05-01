import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "lab-ops"


def test_lab_ops_scaffold_exists_with_command_board_and_team_manifest():
    assert (LAB / "command_board.md").exists()
    manifest = json.loads((LAB / "team_manifest.json").read_text())
    roles = {worker["id"] for worker in manifest["workers"]}
    assert {"news_health", "summarization", "impact_brief", "qa_eval", "taeyoon_improvement"}.issubset(roles)
    assert manifest["control_plane"] == "yuto"
    assert manifest["safety"]["auto_publish"] is False


def test_news_health_worker_dry_run_writes_status_without_db_writes(tmp_path):
    out = tmp_path / "status.json"
    result = subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "news_health_worker.py"),
            "--dry-run",
            "--output",
            str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(out.read_text())
    assert payload["worker"] == "news_health"
    assert payload["mode"] == "dry_run"
    assert payload["db_write"] is False
    assert "llm_status" in payload["metrics"]
    assert "pending_by_text_length" in payload["metrics"]
    assert "recommendations" in payload
    assert "NEWS_HEALTH_STATUS" in result.stdout


def test_impact_brief_worker_dry_run_writes_ranked_candidates(tmp_path):
    out = tmp_path / "impact.json"
    subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "impact_brief_worker.py"),
            "--dry-run",
            "--limit",
            "5",
            "--output",
            str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(out.read_text())
    assert payload["worker"] == "impact_brief"
    assert payload["mode"] == "dry_run"
    assert payload["db_write"] is False
    assert len(payload["candidates"]) <= 5
    if payload["candidates"]:
        first = payload["candidates"][0]
        assert "impact_score" in first
        assert "signals" in first
        assert "confidence" in first


def test_taeyoon_improvement_hook_creates_pending_inbox_task(tmp_path):
    out_dir = tmp_path / "inbox"
    result = subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "taeyoon_improvement_hook.py"),
            "--dry-run",
            "--inbox-dir",
            str(out_dir),
            "--symptom",
            "worker queue backlog rising",
            "--evidence",
            "/tmp/example-status.json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    created = list(out_dir.glob("*.md"))
    assert len(created) == 1
    text = created[0].read_text()
    assert "to: jarvis-dev-qa" in text or "to: jarvis-dev-tech-lead" in text
    assert "approved_by: kei" in text
    assert "status: pending" in text
    assert "worker queue backlog rising" in text
    assert "TAEYOON_TASK" in result.stdout


def test_completion_contract_validator_rejects_closed_without_metric_movement():
    module_path = LAB / "completion_contract.py"
    spec = importlib.util.spec_from_file_location("completion_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    contract = {
        "task": "fake close",
        "target_metric_before": {"ready_for_summary": 8},
        "action_taken": "wrote a report",
        "target_metric_after": {"ready_for_summary": 8},
        "verification_command": "python3 example.py",
        "status": "closed",
    }

    result = module.validate_contract(contract)

    assert result["valid"] is False
    assert "closed_without_metric_change" in result["errors"]


def test_completion_contract_validator_accepts_partial_with_next_owner():
    module_path = LAB / "completion_contract.py"
    spec = importlib.util.spec_from_file_location("completion_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    contract = {
        "task": "reduce enrichment bottleneck",
        "target_metric_before": {"enrichment_needed": 2049, "ready_for_summary": 0},
        "action_taken": "source enrichment write-limited run",
        "target_metric_after": {"enrichment_needed": 2041, "ready_for_summary": 8},
        "verification_command": "sqlite metric query",
        "status": "partial",
        "next_owner": "summarization_worker",
    }

    result = module.validate_contract(contract)

    assert result["valid"] is True
    assert result["metric_changed"] is True


def test_source_enrichment_worker_dry_run_exports_candidates_without_db_writes(tmp_path):
    out = tmp_path / "source_enrichment.json"
    queue = tmp_path / "source_enrichment.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "source_enrichment_worker.py"),
            "--dry-run",
            "--limit",
            "10",
            "--output",
            str(out),
            "--queue-output",
            str(queue),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(out.read_text())
    assert payload["worker"] == "source_enrichment"
    assert payload["mode"] == "dry_run"
    assert payload["db_write"] is False
    assert payload["candidate_count"] <= 10
    assert queue.exists()
    lines = [json.loads(line) for line in queue.read_text().splitlines() if line.strip()]
    assert len(lines) == payload["candidate_count"]
    if lines:
        first = lines[0]
        assert first["source_status"] in {"title_only", "rss_summary", "short_text", "unknown_short"}
        assert first["confidence"] in {"low", "medium"}
        assert first["recommended_next_action"] in {"resolve_canonical_url", "fetch_full_text", "manual_review", "skip"}
        assert first["db_write"] is False
    assert "SOURCE_ENRICHMENT" in result.stdout


def test_source_enrichment_treats_summary_ready_text_as_full_candidate():
    module_path = LAB / "workers" / "source_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("source_enrichment_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    text = "x" * 645

    assert module.enrichment_status_for_text(text) == "full_text_candidate"
    assert module.confidence_for_enriched_text(text) in {"medium", "high"}


def test_source_enrichment_selects_high_score_items_before_newer_low_score_items(tmp_path):
    module_path = LAB / "workers" / "source_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("source_enrichment_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db = tmp_path / "research.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            original_title TEXT,
            original_url TEXT,
            original_source TEXT,
            published_date TEXT,
            fetched_at TEXT,
            llm_status TEXT,
            content_extraction_status TEXT,
            extracted_text TEXT,
            category TEXT,
            region TEXT,
            severity TEXT,
            selection_score REAL,
            selection_tier TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO articles VALUES (1,'new-low','https://example.com/new','Example','2026-04-26','2026-04-26 12:00:00','pending','rss_summary','short','AI Technology','Europe',NULL,10,'noise')"
    )
    conn.execute(
        "INSERT INTO articles VALUES (2,'old-high','https://example.com/old','Example','2026-04-25','2026-04-25 12:00:00','pending','rss_summary','short','AI Technology','Europe',NULL,90,'critical')"
    )
    conn.commit(); conn.close()

    rows = module.select_candidates(1, db_path=db)

    assert rows[0]["id"] == 2


def test_source_enrichment_write_updates_only_full_text_candidates(tmp_path):
    module_path = LAB / "workers" / "source_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("source_enrichment_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db = tmp_path / "research.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            original_title TEXT,
            original_url TEXT,
            original_source TEXT,
            original_source_url TEXT,
            citation_canonical_url TEXT,
            published_date TEXT,
            fetched_at TEXT,
            llm_status TEXT,
            content_extraction_status TEXT,
            extracted_text TEXT,
            extraction_method TEXT,
            verification_status TEXT,
            human_review_status TEXT,
            category TEXT,
            region TEXT,
            severity TEXT,
            selection_score REAL,
            selection_tier TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO articles VALUES (1,'ready','https://news.google.com/a','Google News',NULL,NULL,'2026-04-25','2026-04-25','pending','title_only','short','rss_fallback','unverified','needs_review','AI Technology','Europe',NULL,50,'product_signal')"
    )
    conn.execute(
        "INSERT INTO articles VALUES (2,'not-ready','https://news.google.com/b','Google News',NULL,NULL,'2026-04-25','2026-04-25','pending','title_only','short','rss_fallback','unverified','needs_review','AI Technology','Europe',NULL,50,'product_signal')"
    )
    conn.commit(); conn.close()

    packets = [
        {
            "article_id": 1,
            "source_status": "full_text_candidate",
            "canonical_url": "https://publisher.example/full",
            "extracted_text": "x" * 900,
            "extraction_method": "bounded_http_fetch_after_google_decode",
        },
        {
            "article_id": 2,
            "source_status": "short_text",
            "canonical_url": "https://publisher.example/short",
            "extracted_text": "y" * 200,
            "extraction_method": "bounded_http_fetch_after_google_decode",
        },
    ]

    result = module.write_full_text_candidates(packets, db_path=db, backup_dir=tmp_path)

    assert result["db_write"] is True
    assert result["updated_count"] == 1
    assert result["skipped_count"] == 1
    assert Path(result["backup_path"]).exists()
    conn = sqlite3.connect(db)
    row1 = conn.execute("SELECT extracted_text, content_extraction_status, extraction_method, citation_canonical_url FROM articles WHERE id=1").fetchone()
    row2 = conn.execute("SELECT extracted_text, content_extraction_status FROM articles WHERE id=2").fetchone()
    conn.close()
    assert len(row1[0]) == 900
    assert row1[1] == "full_content"
    assert row1[2] == "full_content"
    assert row1[3] == "https://publisher.example/full"
    assert row2[0] == "short"
    assert row2[1] == "title_only"


def test_source_enrichment_html_extraction_and_confidence_helpers():
    module_path = LAB / "workers" / "source_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("source_enrichment_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    html = """
    <html><head><title>Ignore nav</title><script>bad()</script></head>
    <body><nav>menu menu menu</nav><article>
      <h1>Important AI safety investigation</h1>
      <p>Regulators opened a formal inquiry into AI chatbot privacy and child safety practices.</p>
      <p>The source text includes enough detail for a cautious limited legal-risk summary.</p>
    </article></body></html>
    """
    text = module.extract_article_text_from_html(html)
    assert "Regulators opened a formal inquiry" in text
    assert "bad()" not in text
    assert module.enrichment_status_for_text(text) in {"full_text_candidate", "short_text"}
    assert module.confidence_for_enriched_text(text) in {"medium", "high"}


def test_source_enrichment_decodes_shift_jis_without_mojibake():
    module_path = LAB / "workers" / "source_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("source_enrichment_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw = "<article>日本語の記事本文です。AIとデータ保護について説明します。</article>".encode("shift_jis")
    decoded = module.decode_response_body(raw, declared_charset="utf-8")

    assert "日本語の記事本文" in decoded
    assert "�" not in decoded


def test_ollama_api_disables_thinking_at_top_level(monkeypatch):
    sys.path.insert(0, str(ROOT / "legal-research-center"))
    import summarizer

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "{\"ok\": true}"}}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(summarizer.requests, "post", fake_post)

    output = summarizer.call_ollama_api("Return JSON", "test-model", max_tokens=64)

    assert output == '{"ok": true}'
    assert captured["payload"]["think"] is False
    assert "think" not in captured["payload"].get("options", {})


def test_summarization_worker_writes_each_article_immediately_and_marks_failures(tmp_path):
    module_path = LAB / "workers" / "summarization_worker.py"
    spec = importlib.util.spec_from_file_location("summarization_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    articles = [
        {"id": 1, "original_title": "ok", "extracted_text": "x" * 900},
        {"id": 2, "original_title": "fail", "extracted_text": "y" * 900},
    ]
    updates = []
    failures = []

    def summarize(title, content):
        if title == "fail":
            return None
        return {"thai_title": "สรุป", "thai_summary": "ข้อความ", "category": "AI Technology", "region": "Japan"}

    result = module.process_articles(
        articles,
        write=True,
        summarize_fn=summarize,
        update_fn=lambda article_id, summary: updates.append((article_id, summary)),
        mark_failed_fn=lambda article_id: failures.append(article_id),
    )

    assert result["attempted"] == 2
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert updates[0][0] == 1
    assert failures == [2]
    assert result["results"][0]["db_write"] is True


def test_summarization_worker_reconciles_region_from_article_source():
    module_path = LAB / "workers" / "summarization_worker.py"
    spec = importlib.util.spec_from_file_location("summarization_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    articles = [
        {
            "id": 1,
            "original_title": "김천시, 인공지능 기반 AI 법률정보검색서비스 도입 - 대경일보",
            "original_source": "Google News AI Korea",
            "extracted_text": "x" * 900,
        }
    ]
    updates = []

    def summarize(title, content):
        return {"thai_title": "สรุป", "thai_summary": "ข้อความ", "category": "AI Technology", "region": "Americas"}

    result = module.process_articles(
        articles,
        write=True,
        summarize_fn=summarize,
        update_fn=lambda article_id, summary: updates.append((article_id, summary)),
        mark_failed_fn=lambda article_id: None,
    )

    assert result["completed"] == 1
    assert updates[0][1]["region"] == "South Korea"


def test_qa_eval_worker_flags_missing_fields_and_region_mismatch():
    module_path = LAB / "workers" / "qa_eval_worker.py"
    spec = importlib.util.spec_from_file_location("qa_eval_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rows = [
        {
            "id": 1,
            "original_title": "湖南AI中心在湘江新区正式发布 - DoNews",
            "original_source": "Google News AI China",
            "thai_title": "เปิดตัวศูนย์ AI Hunan ในเขต Xiangjiang",
            "thai_summary": "สรุปข่าวภาษาไทยที่มีข้อมูลเพียงพอเกี่ยวกับศูนย์ AI และผลกระทบด้านนโยบาย รวมถึงบริบทการพัฒนาเทคโนโลยีในจีน",
            "thai_analysis": "มีผลต่อการกำกับดูแลและการลงทุนด้าน AI",
            "category": "AI Technology",
            "region": "Americas",
            "severity": "low",
            "legal_relevance_score": 3,
            "word_count_thai": 12,
            "extracted_text": "x" * 900,
        },
        {
            "id": 2,
            "original_title": "ok",
            "original_source": "Google News AI Korea",
            "thai_title": "ข่าว AI เกาหลีใต้",
            "thai_summary": "สรุปข่าวภาษาไทยที่มีข้อมูลครบถ้วนเกี่ยวกับการใช้ AI ในเกาหลีใต้ พร้อมบริบทเชิงนโยบายและผลกระทบต่อองค์กร",
            "thai_analysis": "วิเคราะห์ผลกระทบทางกฎหมาย",
            "category": "AI Technology",
            "region": "South Korea",
            "severity": "low",
            "legal_relevance_score": 3,
            "word_count_thai": 8,
            "extracted_text": "x" * 900,
        },
    ]

    report = module.evaluate_rows(rows)

    assert report["worker"] == "qa_eval"
    assert report["sample_size"] == 2
    assert report["fail_count"] == 1
    assert report["pass_count"] == 1
    assert report["status"] == "fail"
    assert report["items"][0]["issues"] == ["region_mismatch_expected_china"]
    assert report["db_write"] is False


def test_comms_watchdog_flags_stale_pending_requests_without_side_effects(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox" / "yuto"
    status = tmp_path / "status.json"
    inbox.mkdir(parents=True)
    outbox.mkdir(parents=True)
    request = inbox / "2026-04-25-stale-request.md"
    request.write_text(
        "---\n"
        "id: stale-request-1\n"
        "from: yuto\n"
        "to: taeyoon-claude-live\n"
        "type: request\n"
        "status: pending\n"
        "needs_reply: true\n"
        "priority: high\n"
        "---\n"
        "Please respond at response-stale-request-1.md\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "comms_watchdog_worker.py"),
            "--inbox-dir",
            str(inbox),
            "--outbox-dir",
            str(outbox),
            "--max-pending-minutes",
            "0",
            "--output",
            str(status),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(status.read_text())
    assert payload["worker"] == "comms_watchdog"
    assert payload["mode"] == "dry_run"
    assert payload["db_write"] is False
    assert payload["pending_needing_reply"] == 1
    assert payload["stale_pending_count"] == 1
    assert payload["alerts"][0]["request_id"] == "stale-request-1"
    assert "COMMS_WATCHDOG" in result.stdout


def test_comms_watchdog_does_not_treat_ack_as_final_reply(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox" / "yuto"
    status = tmp_path / "status.json"
    inbox.mkdir(parents=True)
    outbox.mkdir(parents=True)
    request = inbox / "2026-04-25-needs-real-response.md"
    request.write_text(
        "---\n"
        "id: needs-real-response\n"
        "from: yuto\n"
        "to: taeyoon-claude-live\n"
        "type: request\n"
        "status: pending\n"
        "needs_reply: true\n"
        "priority: high\n"
        "---\n"
        "Please do the work, not only ACK.\n"
    )
    (outbox / "ack-needs-real-response.md").write_text("ACK only\n")

    subprocess.run(
        [
            sys.executable,
            str(LAB / "workers" / "comms_watchdog_worker.py"),
            "--inbox-dir",
            str(inbox),
            "--outbox-dir",
            str(outbox),
            "--max-pending-minutes",
            "0",
            "--output",
            str(status),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(status.read_text())
    assert payload["requests"][0]["has_ack"] is True
    assert payload["requests"][0]["has_response"] is False
    assert payload["stale_pending_count"] == 1
