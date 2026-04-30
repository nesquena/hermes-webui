"""Tests for #480 (PDF first-page preview) and #482 (HTML iframe sandbox).

Validates that the MEDIA: restore block in ui.js produces the correct
placeholder HTML for .pdf and .html files, that lazy-load functions exist,
and that CSS classes are defined.
"""
import os
import re
import pytest


def _read_js(name):
    with open(os.path.join('static', name)) as f:
        return f.read()


def _read_css():
    with open(os.path.join('static', 'style.css')) as f:
        return f.read()


# ── Extension regexes ──────────────────────────────────────────────────────

class TestExtensionRegexes:
    """PDF and HTML extension regexes must be defined at module scope."""

    def test_pdf_exts_regex_exists(self):
        ui = _read_js('ui.js')
        assert '_PDF_EXTS' in ui, '_PDF_EXTS regex must be defined'
        idx = ui.find('_PDF_EXTS')
        assert '.pdf' in ui[idx:idx+100], '_PDF_EXTS must match .pdf extension'

    def test_html_exts_regex_exists(self):
        ui = _read_js('ui.js')
        assert '_HTML_EXTS' in ui, '_HTML_EXTS regex must be defined'
        idx = ui.find('_HTML_EXTS')
        assert 'html' in ui[idx:idx+100], '_HTML_EXTS must match .html extension'

    def test_pdf_not_matched_by_image_exts(self):
        """PDF files must not be caught by _IMAGE_EXTS."""
        ui = _read_js('ui.js')
        m = re.search(r'const _IMAGE_EXTS=/(.+?)/[a-z]*;', ui)
        assert m
        pattern = m.group(1)
        assert 'pdf' not in pattern, 'PDF must not be in _IMAGE_EXTS (would render as broken <img>)'

    def test_html_not_matched_by_image_exts(self):
        """HTML files must not be caught by _IMAGE_EXTS."""
        ui = _read_js('ui.js')
        m = re.search(r'const _IMAGE_EXTS=/(.+?)/[a-z]*;', ui)
        assert m
        pattern = m.group(1)
        assert 'html' not in pattern, 'HTML must not be in _IMAGE_EXTS'


# ── MEDIA: placeholder HTML ────────────────────────────────────────────────

class TestPdfMediaPlaceholder:
    """PDF files in MEDIA: tokens must produce a lazy-load placeholder div."""

    def test_pdf_media_produces_placeholder_div(self):
        ui = _read_js('ui.js')
        m = re.search(r'_PDF_EXTS\.test\(ref\)', ui)
        assert m, 'MEDIA restore must check _PDF_EXTS for PDF files'
        body = ui[m.start():m.start() + 300]
        assert 'pdf-preview-load' in body, 'PDF MEDIA must produce .pdf-preview-load placeholder'
        assert 'data-path' in body, 'PDF placeholder must include data-path attribute'

    def test_pdf_media_uses_i18n_loading_key(self):
        ui = _read_js('ui.js')
        m = re.search(r'_PDF_EXTS\.test\(ref\)', ui)
        body = ui[m.start():m.start() + 300]
        assert 'pdf_loading' in body, 'PDF placeholder must use pdf_loading i18n key'


class TestHtmlMediaPlaceholder:
    """HTML files in MEDIA: tokens must produce a lazy-load placeholder div."""

    def test_html_media_produces_placeholder_div(self):
        ui = _read_js('ui.js')
        m = re.search(r'_HTML_EXTS\.test\(ref\)', ui)
        assert m, 'MEDIA restore must check _HTML_EXTS for HTML files'
        body = ui[m.start():m.start() + 300]
        assert 'html-preview-load' in body, 'HTML MEDIA must produce .html-preview-load placeholder'
        assert 'data-path' in body, 'HTML placeholder must include data-path attribute'

    def test_html_media_uses_i18n_loading_key(self):
        ui = _read_js('ui.js')
        m = re.search(r'_HTML_EXTS\.test\(ref\)', ui)
        body = ui[m.start():m.start() + 300]
        assert 'html_loading' in body, 'HTML placeholder must use html_loading i18n key'

    def test_html_iframe_has_sandbox_attribute(self):
        """HTML preview iframe must use sandbox attribute for security."""
        ui = _read_js('ui.js')
        assert 'sandbox=' in ui, 'loadHtmlInline must set sandbox attribute on iframe'
        assert 'allow-scripts' in ui, 'sandbox must include allow-scripts for interactive content'


# ── Lazy-load functions ────────────────────────────────────────────────────

class TestLoadPdfInlineFunction:
    """loadPdfInline() must exist and follow the same pattern as loadDiffInline()."""

    def test_function_exists(self):
        ui = _read_js('ui.js')
        assert 'function loadPdfInline()' in ui, 'loadPdfInline() function must exist'

    def test_selects_pdf_preview_load_elements(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline()')
        body = ui[idx:idx + 500]
        assert 'pdf-preview-load' in body, 'Must query .pdf-preview-load elements'
        assert 'data-loaded' in body, 'Must use data-loaded attribute to prevent double-processing'

    def test_fetches_via_api_media(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline()')
        body = ui[idx:idx + 1500]
        assert 'api/media?path=' in body, 'Must fetch PDF via api/media endpoint'

    def test_has_size_cap(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline()')
        body = ui[idx:idx + 1500]
        assert 'MAX_SIZE' in body or 'byteLength' in body, 'Must enforce a size cap on PDF files'

    def test_fallback_on_error(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline()')
        body = ui[idx:idx + 3000]
        assert 'pdf_error' in body, 'Must show error fallback on failure'
        assert 'pdf_download' in body or 'download=' in body, 'Error fallback must include download link'

    def test_lazy_loads_pdfjs_from_cdn(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline()')
        body = ui[idx:idx + 3000]
        assert 'pdfjs' in body, 'Must lazy-load PDF.js from CDN'

    def test_pdfjs_state_variables(self):
        ui = _read_js('ui.js')
        assert '_pdfjsReady' in ui, '_pdfjsReady state variable must exist'
        assert '_pdfjsLoading' in ui, '_pdfjsLoading state variable must exist'


class TestLoadHtmlInlineFunction:
    """loadHtmlInline() must exist and render HTML in a sandboxed iframe."""

    def test_function_exists(self):
        ui = _read_js('ui.js')
        assert 'function loadHtmlInline()' in ui, 'loadHtmlInline() function must exist'

    def test_selects_html_preview_load_elements(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 500]
        assert 'html-preview-load' in body, 'Must query .html-preview-load elements'
        assert 'data-loaded' in body, 'Must use data-loaded attribute'

    def test_fetches_via_api_media(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 1000]
        assert 'api/media?path=' in body, 'Must fetch HTML via api/media endpoint'

    def test_has_size_cap(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 1000]
        assert 'MAX_SIZE' in body or 'html.length' in body, 'Must enforce a size cap on HTML files'

    def test_fallback_on_error(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 2000]
        assert 'html_error' in body, 'Must show error fallback on failure'

    def test_uses_srcdoc_attribute(self):
        """Must use srcdoc (not src) for HTML content to keep it same-origin sandboxed."""
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 1500]
        assert 'srcdoc=' in body, 'Must use srcdoc attribute for inline HTML rendering'

    def test_escapes_html_for_srcdoc(self):
        """HTML content must be escaped before embedding in srcdoc to prevent attribute injection."""
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline()')
        body = ui[idx:idx + 1500]
        # Must escape &, <, >, " to prevent breaking out of srcdoc attribute
        assert '&amp;' in body or 'replace' in body, 'Must escape HTML entities for srcdoc'


# ── requestAnimationFrame integration ──────────────────────────────────────

class TestRAFIntegration:
    """Both lazy-load functions must be called in the requestAnimationFrame blocks."""

    def test_loadPdfInline_called_after_render(self):
        ui = _read_js('ui.js')
        raf_blocks = re.findall(r'requestAnimationFrame\(\(\)=>\{[^}]+\}\)', ui)
        load_blocks = [b for b in raf_blocks if 'loadDiffInline' in b]
        assert len(load_blocks) >= 2, 'Expected at least 2 rAF blocks with loadDiffInline'
        for block in load_blocks:
            assert 'loadPdfInline()' in block, 'loadPdfInline() must be called alongside loadDiffInline'

    def test_loadHtmlInline_called_after_render(self):
        ui = _read_js('ui.js')
        raf_blocks = re.findall(r'requestAnimationFrame\(\(\)=>\{[^}]+\}\)', ui)
        load_blocks = [b for b in raf_blocks if 'loadDiffInline' in b]
        for block in load_blocks:
            assert 'loadHtmlInline()' in block, 'loadHtmlInline() must be called alongside loadDiffInline'

    def test_initTreeViews_blocks_also_call_loaders(self):
        """rAF blocks with initTreeViews (not loadDiffInline) must also call PDF/HTML loaders."""
        ui = _read_js('ui.js')
        raf_blocks = re.findall(r'requestAnimationFrame\(\(\)=>\{[^}]+\}\)', ui)
        tree_blocks = [b for b in raf_blocks if 'initTreeViews' in b and 'loadDiffInline' not in b]
        for block in tree_blocks:
            assert 'loadPdfInline()' in block, 'initTreeViews rAF block must also call loadPdfInline'
            assert 'loadHtmlInline()' in block, 'initTreeViews rAF block must also call loadHtmlInline'


# ── CSS classes ────────────────────────────────────────────────────────────

class TestCSSClasses:
    """CSS must define styles for PDF and HTML preview components."""

    def test_pdf_preview_wrap(self):
        css = _read_css()
        assert '.pdf-preview-wrap' in css

    def test_pdf_preview_header(self):
        css = _read_css()
        assert '.pdf-preview-header' in css

    def test_pdf_preview_body(self):
        css = _read_css()
        assert '.pdf-preview-body' in css

    def test_pdf_preview_canvas(self):
        css = _read_css()
        assert '.pdf-preview-canvas' in css

    def test_pdf_preview_fallback(self):
        css = _read_css()
        assert '.pdf-preview-fallback' in css

    def test_pdf_download_link(self):
        css = _read_css()
        # pdf-download-link class used in JS; styled via header a selector
        assert '.pdf-download-link' in css or '.pdf-preview-header a' in css

    def test_html_preview_wrap(self):
        css = _read_css()
        assert '.html-preview-wrap' in css

    def test_html_preview_header(self):
        css = _read_css()
        assert '.html-preview-header' in css

    def test_html_preview_iframe(self):
        css = _read_css()
        assert '.html-preview-iframe' in css

    def test_html_preview_fallback(self):
        css = _read_css()
        assert '.html-preview-fallback' in css

    def test_html_iframe_has_fixed_height(self):
        """HTML iframe must have a fixed height to prevent overflow."""
        css = _read_css()
        m = re.search(r'\.html-preview-iframe\{[^}]+\}', css)
        assert m, '.html-preview-iframe rule must exist'
        assert 'height' in m.group(), 'HTML iframe must have a height constraint'


# ── i18n keys ──────────────────────────────────────────────────────────────

class TestI18nKeys:
    """All required i18n keys must exist in the en locale."""

    PDF_KEYS = ['pdf_loading', 'pdf_too_large', 'pdf_no_pages', 'pdf_error', 'pdf_download']
    HTML_KEYS = ['html_loading', 'html_too_large', 'html_error', 'html_open_full', 'html_sandbox_label']

    def _find_locale_block(self, locale):
        with open('static/i18n.js') as f:
            content = f.read()
        start = content.find(f"'{locale}':")
        if start < 0:
            start = content.find(f'{locale}:')
        if start < 0:
            return ''
        # Find end by scanning for next top-level locale
        locales = ['en', 'ru', 'es', 'de', 'zh', 'zh-Hant', 'ko']
        end = len(content)
        for loc in locales:
            if loc == locale:
                continue
            pos = content.find(f"'{loc}':", start + 5)
            if pos > start and pos < end:
                end = pos
        return content[start:end]

    def test_pdf_keys_in_en(self):
        block = self._find_locale_block('en')
        for key in self.PDF_KEYS:
            assert f'{key}:' in block, f'en locale must have key {key}'

    def test_html_keys_in_en(self):
        block = self._find_locale_block('en')
        for key in self.HTML_KEYS:
            assert f'{key}:' in block, f'en locale must have key {key}'

    def test_pdf_keys_in_all_locales(self):
        for loc in ['ru', 'es', 'de', 'zh', 'zh-Hant', 'ko']:
            block = self._find_locale_block(loc)
            missing = [k for k in self.PDF_KEYS if f'{k}:' not in block]
            assert not missing, f'{loc} locale missing PDF keys: {missing}'

    def test_html_keys_in_all_locales(self):
        for loc in ['ru', 'es', 'de', 'zh', 'zh-Hant', 'ko']:
            block = self._find_locale_block(loc)
            missing = [k for k in self.HTML_KEYS if f'{k}:' not in block]
            assert not missing, f'{loc} locale missing HTML keys: {missing}'
