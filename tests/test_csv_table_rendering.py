"""Test: CSV table rendering (#485)"""
import re


def test_csv_extension_regex():
    """Verify _CSV_EXTS regex is defined."""
    with open('static/ui.js') as f:
        src = f.read()
    assert '_CSV_EXTS' in src, "Missing _CSV_EXTS regex"
    assert '.csv' in src, "CSV regex should match .csv extension"


def test_csv_fence_block_handler():
    """Verify fenced ```csv blocks are handled."""
    with open('static/ui.js') as f:
        src = f.read()
    assert "lang==='csv'" in src, "Missing csv language detection in fence handler"
    assert 'csv-table' in src, "Missing csv-table class for fenced CSV rendering"
    assert 'csv-table-wrap' in src, "Missing csv-table-wrap class"


def test_csv_fence_renders_table_structure():
    """Verify fenced CSV blocks produce proper table HTML."""
    with open('static/ui.js') as f:
        src = f.read()
    # Should have thead, tbody, th, td
    assert '<thead>' in src, "CSV table should have <thead>"
    assert '<tbody>' in src, "CSV table should have <tbody>"
    # In the fence handler section
    fence_section = src[src.find("lang==='csv'"):src.find("lang==='csv'") + 800]
    assert '<th>' in fence_section, "CSV headers should use <th>"
    assert '<td>' in fence_section, "CSV body should use <td>"


def test_csv_fence_fallback_for_insufficient_rows():
    """Verify CSV with < 2 rows falls back to code block."""
    with open('static/ui.js') as f:
        src = f.read()
    fence_section = src[src.find("lang==='csv'"):src.find("lang==='csv'") + 800]
    assert 'rows.length>=2' in fence_section, "Should check for at least 2 rows"
    assert '<pre><code' in fence_section, "Fallback should render as <pre><code>"


def test_csv_media_file_handler():
    """Verify MEDIA: CSV files trigger inline loading."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'csv-inline-load' in src, "Missing csv-inline-load class for MEDIA: CSV"
    assert 'csv_loading' in src, "Missing csv_loading i18n key usage"


def test_loadCsvInline_function():
    """Verify loadCsvInline lazy-load function exists."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'function loadCsvInline()' in src, "Missing loadCsvInline function"


def test_csv_inline_max_size():
    """Verify CSV inline rendering has a size cap."""
    with open('static/ui.js') as f:
        src = f.read()
    csv_section = src[src.find('function loadCsvInline()'):src.find('function loadCsvInline()') + 2000]
    assert 'CSV_MAX_SIZE' in csv_section, "Should have CSV_MAX_SIZE constant"
    assert 'csv_too_large' in csv_section, "Should use csv_too_large i18n for oversized files"


def test_csv_auto_detect_separator():
    """Verify CSV handler auto-detects separator."""
    with open('static/ui.js') as f:
        src = f.read()
    csv_section = src[src.find('function loadCsvInline()'):src.find('function loadCsvInline()') + 2000]
    assert 'separators' in csv_section, "Should have separator detection"
    assert ';' in csv_section, "Should detect semicolon separator"
    assert 'tab' in csv_section.lower() or '\\t' in csv_section, "Should detect tab separator"


def test_csv_quote_stripping():
    """Verify CSV handler strips surrounding quotes from fields."""
    with open('static/ui.js') as f:
        src = f.read()
    assert "replace(/^[\"']|[\"']$/g,'')" in src, "Should strip quotes from CSV fields"


def test_csv_error_handling():
    """Verify CSV error and empty data handling."""
    with open('static/ui.js') as f:
        src = f.read()
    csv_section = src[src.find('function loadCsvInline()'):src.find('function loadCsvInline()') + 2500]
    assert 'csv_error' in csv_section, "Should use csv_error i18n on fetch failure"
    assert 'csv_no_data' in csv_section, "Should use csv_no_data i18n for insufficient data"


def test_csv_loadCsvInline_called_after_render():
    """Verify loadCsvInline is called in requestAnimationFrame after rendering."""
    with open('static/ui.js') as f:
        src = f.read()
    assert src.count('loadCsvInline()') >= 2, \
        "loadCsvInline should be called at least twice (initial render + cache restore)"


def test_csv_line_ending_normalization():
    """Verify CSV handler normalizes line endings."""
    with open('static/ui.js') as f:
        src = f.read()
    csv_section = src[src.find('function loadCsvInline()'):src.find('function loadCsvInline()') + 2000]
    assert '\\r\\n' in csv_section, "Should handle \\r\\n line endings"
    assert '\\r' in csv_section, "Should handle \\r line endings"


def test_csv_i18n_keys():
    """Verify CSV i18n keys exist in all 7 locales."""
    with open('static/i18n.js') as f:
        src = f.read()
    required_keys = ['csv_loading', 'csv_too_large', 'csv_no_data', 'csv_error']
    for key in required_keys:
        count = src.count(f"{key}:")
        assert count == 7, f"Key '{key}' found {count} times, expected 7"


def test_csv_css_classes():
    """Verify CSV table CSS classes are defined."""
    with open('static/style.css') as f:
        src = f.read()
    required_classes = ['csv-table-wrap', 'csv-table', 'csv-table th', 'csv-table td']
    for cls in required_classes:
        assert cls in src, f"Missing CSS: {cls}"
    # Check for hover effect
    assert 'csv-table tbody tr:hover' in src, "Missing hover effect for CSV rows"


def test_csv_not_matched_by_image_exts():
    """Verify .csv is NOT in _IMAGE_EXTS."""
    with open('static/ui.js') as f:
        src = f.read()
    match = re.search(r"const _IMAGE_EXTS=/([^/]+)/i", src)
    assert match
    exts = match.group(1)
    assert 'csv' not in exts.lower(), ".csv should NOT be in _IMAGE_EXTS"
