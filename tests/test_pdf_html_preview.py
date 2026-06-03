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
        assert 'function loadPdfInline' in ui, 'loadPdfInline() function must exist'

    def test_selects_pdf_preview_load_elements(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline')
        body = ui[idx:idx + 500]
        assert 'pdf-preview-load' in body, 'Must query .pdf-preview-load elements'
        assert 'data-loaded' in body, 'Must use data-loaded attribute to prevent double-processing'

    def test_fetches_via_api_media(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline')
        body = ui[idx:idx + 1800]
        assert '_mediaApiHref(' in body, 'Must build PDF fetch/download URLs through the media URL helper'

    def test_pdf_media_helper_is_base_aware(self):
        ui = _read_js('ui.js')
        assert 'function _appHref' in ui, 'Local media helpers must resolve against document.baseURI'
        assert 'new URL(rel,document.baseURI||location.href)' in ui, 'Deep-linked session pages need base-aware local media URLs'

    def test_media_helper_is_defined_before_render_and_loaders(self):
        ui = _read_js('ui.js')
        helper_idx = ui.find('function _mediaApiHref')
        render_idx = ui.find('function renderMd')
        pdf_idx = ui.find('function loadPdfInline')
        assert helper_idx != -1, 'Media helper must exist at module scope'
        assert render_idx != -1 and pdf_idx != -1, 'renderMd and loadPdfInline must exist'
        assert helper_idx < render_idx < pdf_idx, 'Media helper must be defined before renderMd/loadPdfInline so global preview loaders can call it'

    def test_has_size_cap(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline')
        body = ui[idx:idx + 1500]
        assert 'MAX_SIZE' in body or 'byteLength' in body, 'Must enforce a size cap on PDF files'

    def test_fallback_on_error(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadPdfInline')
        body = ui[idx:idx + 3000]
        assert 'pdf_error' in body, 'Must show error fallback on failure'
        assert 'pdf_download' in body or 'download=' in body, 'Error fallback must include download link'

    def test_lazy_loads_pdfjs_from_cdn(self):
        ui = _read_js('ui.js')
        idx = ui.find('function _ensurePdfJsLoaded')
        body = ui[idx:idx + 1200]
        assert 'pdfjs' in body, 'Must lazy-load PDF.js from CDN'
        assert 'import(pdfJsSrc)' in body, 'PDF.js loader must use dynamic import so the bootstrap code actually runs'

    def test_pdfjs_loader_does_not_mix_script_src_with_inline_module_body(self):
        ui = _read_js('ui.js')
        idx = ui.find('function _ensurePdfJsLoaded')
        body = ui[idx:idx + 1400]
        assert "document.createElement('script')" not in body, 'PDF.js bootstrap must not rely on dynamically created <script src=...> with inline module text'
        assert 's.textContent=' not in body, 'PDF.js bootstrap must not attach inline module text to a script with src because browsers ignore the inline body'

    def test_pdfjs_state_variables(self):
        ui = _read_js('ui.js')
        assert '_pdfjsReady' in ui, '_pdfjsReady state variable must exist'
        assert '_pdfjsLoading' in ui, '_pdfjsLoading state variable must exist'


class TestLoadHtmlInlineFunction:
    """loadHtmlInline() must exist and render HTML in a sandboxed iframe."""

    def test_function_exists(self):
        ui = _read_js('ui.js')
        assert 'function loadHtmlInline' in ui, 'loadHtmlInline() function must exist'

    def test_selects_html_preview_load_elements(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 500]
        assert 'html-preview-load' in body, 'Must query .html-preview-load elements'
        assert 'data-loaded' in body, 'Must use data-loaded attribute'

    def test_fetches_via_api_media(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 1200]
        assert '_mediaApiHref(' in body, 'Must build HTML fetch/open URLs through the media URL helper'

    def test_has_size_cap(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 1000]
        assert 'MAX_SIZE' in body or 'html.length' in body, 'Must enforce a size cap on HTML files'

    def test_fallback_on_error(self):
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 2000]
        assert 'html_error' in body, 'Must show error fallback on failure'

    def test_uses_srcdoc_attribute(self):
        """Must use srcdoc (not src) for HTML content to keep it same-origin sandboxed."""
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 1500]
        assert 'srcdoc=' in body, 'Must use srcdoc attribute for inline HTML rendering'

    def test_escapes_html_for_srcdoc(self):
        """HTML content must be escaped before embedding in srcdoc to prevent attribute injection."""
        ui = _read_js('ui.js')
        idx = ui.find('function loadHtmlInline')
        body = ui[idx:idx + 1500]
        # Must escape &, <, >, " to prevent breaking out of srcdoc attribute
        assert '&amp;' in body or 'replace' in body, 'Must escape HTML entities for srcdoc'


# ── requestAnimationFrame integration ──────────────────────────────────────

class TestRAFIntegration:
    """Lazy-load functions must be called by the consolidated post-render pass."""

    def test_loadPdfInline_called_after_render(self):
        ui = _read_js('ui.js')
        idx = ui.find('function postProcessRenderedMessages')
        body = ui[idx:idx + 500]
        assert 'loadDiffInline(container)' in body, 'post-process must call loadDiffInline'
        assert 'loadPdfInline(container)' in body, 'post-process must call loadPdfInline alongside loadDiffInline'

    def test_loadHtmlInline_called_after_render(self):
        ui = _read_js('ui.js')
        idx = ui.find('function postProcessRenderedMessages')
        body = ui[idx:idx + 500]
        assert 'loadDiffInline(container)' in body, 'post-process must call loadDiffInline'
        assert 'loadHtmlInline(container)' in body, 'post-process must call loadHtmlInline alongside loadDiffInline'

    def test_initTreeViews_blocks_also_call_loaders(self):
        """Tree views and inline loaders must share the same post-process pass."""
        ui = _read_js('ui.js')
        idx = ui.find('function postProcessRenderedMessages')
        body = ui[idx:idx + 500]
        assert 'initTreeViews(container)' in body, 'post-process must initialize tree views'
        assert 'loadPdfInline(container)' in body, 'post-process must also call loadPdfInline'
        assert 'loadHtmlInline(container)' in body, 'post-process must also call loadHtmlInline'

    def test_message_render_uses_single_post_process_raf(self):
        ui = _read_js('ui.js')
        assert ui.count('requestAnimationFrame(()=>postProcessRenderedMessages(inner))') == 2


# ── CSS classes ────────────────────────────────────────────────────────────

class TestCSSClasses:
    """CSS must define styles for PDF and HTML preview components."""

    def test_pdf_preview_wrap(self):
        css = _read_css()
        assert '.pdf-preview-wrap' in css
        assert '.pdf-preview-wrap--interactive' in css, 'PDF preview should expose a clickable interactive state for full-page expand'
        assert '.msg-body li>.pdf-preview-wrap' in css, 'Inline PDF previews inside markdown list items should align with their bullet instead of dropping to a new line'
        assert '.msg-body li.list-block-embed' in css, 'List items whose content is just an embed should be tagged for special bullet alignment'
        assert '.msg-body li.list-block-embed::before' in css, 'Embed-only list items should draw their own bullet so the marker sits at the top of the block like code blocks do'

    def test_pdf_preview_header(self):
        css = _read_css()
        assert '.pdf-preview-header' in css
        m = re.search(r'\.pdf-preview-header\{[^}]+\}', css)
        assert m, '.pdf-preview-header rule must exist'
        rule = m.group()
        assert 'flex-wrap:wrap' in rule, 'PDF preview header should wrap on narrow layouts so filename and download link do not collide'

    def test_pdf_preview_body(self):
        css = _read_css()
        assert '.pdf-preview-body' in css
        m = re.search(r'\.pdf-preview-body\{[^}]+\}', css)
        assert m, '.pdf-preview-body rule must exist'
        rule = m.group()
        assert 'overflow:auto' in rule, 'PDF preview body should scroll instead of hiding oversized pages'
        assert 'max-height:min(78vh,960px)' in rule, 'PDF preview body should use a viewport-relative height cap instead of a fixed 500px crop'

    def test_pdf_preview_canvas(self):
        css = _read_css()
        assert '.pdf-preview-canvas' in css
        rules = re.findall(r'\.pdf-preview-canvas\{[^}]+\}', css)
        assert rules, '.pdf-preview-canvas rule must exist'
        assert any('width:auto' in rule for rule in rules), 'PDF canvas should keep its intrinsic width instead of being forced to shrink to the chat column'
        assert any('max-width:none' in rule for rule in rules), 'PDF canvas should not be hard-clamped to 100% width'

    def test_pdf_preview_fallback(self):
        css = _read_css()
        assert '.pdf-preview-fallback' in css

    def test_pdf_download_link(self):
        css = _read_css()
        # pdf-download-link class used in JS; styled via header a selector
        assert '.pdf-download-link' in css or '.pdf-preview-header a' in css

    def test_pdf_lightbox_css(self):
        css = _read_css()
        for cls in ['.pdf-lightbox', '.pdf-lightbox-dialog', '.pdf-lightbox-bar', '.pdf-lightbox-body', '.pdf-lightbox-pages', '.pdf-lightbox-page', '.pdf-lightbox-page-canvas', '.pdf-lightbox-page-input', '.pdf-lightbox-go', '.pdf-lightbox-close']:
            assert cls in css, f'{cls} must be defined for full-page PDF.js preview overlay'
        overlay = re.search(r'\.pdf-lightbox\{[^}]+\}', css)
        dialog = re.search(r'\.pdf-lightbox-dialog\{[^}]+\}', css)
        bar = re.search(r'\.pdf-lightbox-bar\{[^}]+\}', css)
        body = re.search(r'\.pdf-lightbox-body\{[^}]+\}', css)
        assert overlay and 'padding:8px' in overlay.group(), 'Quick tighten should reduce outer PDF modal padding'
        assert dialog and 'width:min(98vw,1440px)' in dialog.group() and 'height:min(96vh,1160px)' in dialog.group(), 'Quick tighten should expand the usable PDF dialog area'
        assert bar and 'padding:6px 10px' in bar.group(), 'Quick tighten should reduce title-bar padding'
        assert body and 'overflow:auto' in body.group(), 'PDF.js full viewer body should support continuous vertical scroll'

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


class TestPdfCanvasAttachmentNotSerialized:
    """Regression: canvas.outerHTML serializes only the <canvas> element wrapper,
    NOT the rendered bitmap. Interpolating ${canvas.outerHTML} into a template
    string produces a fresh empty <canvas> when parsed back into the DOM, so the
    PDF preview renders as a blank rectangle.

    The PDF preview must attach the canvas via appendChild / replaceWith so the
    rendered DOM node carries its bitmap state across the swap.
    """

    def _pdf_block(self):
        ui = _read_js('ui.js')
        start = ui.find('// ── PDF inline preview')
        end = ui.find('// ── HTML inline preview', start)
        assert start != -1 and end != -1, 'PDF preview block not found in ui.js'
        return ui[start:end]

    def test_pdf_does_not_serialize_canvas_via_outerhtml(self):
        block = self._pdf_block()
        assert '${canvas.outerHTML}' not in block, (
            'canvas.outerHTML loses the rendered bitmap when interpolated; '
            'attach the canvas via appendChild or replaceWith instead'
        )

    def test_pdf_attaches_canvas_as_dom_node(self):
        block = self._pdf_block()
        attaches_dom = 'appendChild(canvas)' in block or '.replaceWith(' in block
        assert attaches_dom, (
            'PDF preview must attach the rendered canvas as a DOM node '
            '(appendChild / replaceWith), not interpolate it as a string'
        )

    def test_pdf_preview_wrap_is_marked_interactive_with_open_url(self):
        block = self._pdf_block()
        assert 'pdf-preview-wrap--interactive' in block, 'Rendered PDF preview should be marked interactive for click-to-expand behavior'
        assert 'wrap.dataset.openUrl=openUrl' in block, 'Rendered PDF preview should carry a full-preview URL'
        assert "wrap.setAttribute('role','button')" in block, 'Rendered PDF preview should expose button semantics for keyboard users'
        ui = _read_js('ui.js')
        assert "const _liCls=/^\\x00D\\d+\\x00$/.test(_ih)?' class=\"list-block-embed\"':'';" in ui, 'Unordered markdown list items that are pure embed placeholders should be tagged for special bullet alignment'
        assert "const clsAttr=/^\\x00D\\d+\\x00$/.test(itemHtml)?' class=\"list-block-embed\"':'';" in ui, 'Ordered markdown list items that are pure embed placeholders should be tagged for special bullet alignment'

    def test_pdf_lightbox_helpers_exist(self):
        ui = _read_js('ui.js')
        for fn in ['function _openPdfLightbox', 'function _closePdfLightbox', 'function _openPdfPreviewFromWrap', 'function _pdfLightboxSetStatus', 'function _pdfLightboxScrollToPage', 'function _pdfLightboxSyncCurrentPage', 'async function _renderPdfLightbox']:
            assert fn in ui, f'{fn} must exist for the PDF.js full viewer'
        assert 'pageStatus.className=' in ui and 'pdf-lightbox-page-status' in ui, 'PDF.js viewer should expose current-page status'
        assert "pageInput.className='pdf-lightbox-page-input'" in ui, 'PDF.js viewer should expose a jump-to-page input'
        assert "goBtn.className='pdf-lightbox-go'" in ui, 'PDF.js viewer should expose a jump-to-page action'
        assert "body.addEventListener('scroll',()=>_pdfLightboxSyncCurrentPage(lb),{passive:true});" in ui, 'PDF.js viewer should track the visible page while scrolling'
        assert "const pagesWrap=document.createElement('div');" in ui and "pagesWrap.className='pdf-lightbox-pages'" in ui, 'PDF.js viewer should render into a dedicated multi-page container'
