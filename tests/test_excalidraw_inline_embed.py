"""Test: Excalidraw inline embed (#479)"""
import re


def test_excalidraw_extension_regex():
    """Verify _EXCALIDRAW_EXTS regex is defined."""
    with open('static/ui.js') as f:
        src = f.read()
    assert '_EXCALIDRAW_EXTS' in src, "Missing _EXCALIDRAW_EXTS regex"
    assert '.excalidraw' in src, "Excalidraw regex should match .excalidraw"


def test_excalidraw_media_handler():
    """Verify MEDIA: .excalidraw files trigger inline loading."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'excalidraw-inline-load' in src, "Missing excalidraw-inline-load class"
    assert 'excalidraw_loading' in src, "Missing excalidraw_loading i18n key usage"


def test_loadExcalidrawInline_function():
    """Verify loadExcalidrawInline lazy-load function exists."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'function loadExcalidrawInline()' in src, "Missing loadExcalidrawInline function"


def test_excalidraw_json_validation():
    """Verify Excalidraw handler validates JSON format."""
    with open('static/ui.js') as f:
        src = f.read()
    func = src[src.find('function loadExcalidrawInline()'):src.find('function loadExcalidrawInline()') + 2000]
    assert 'JSON.parse' in func, "Should parse JSON"
    assert 'excalidraw_invalid' in func, "Should handle invalid format"
    assert "data.type!=='excalidraw'" in func, "Should validate type field is 'excalidraw'"


def test_excalidraw_size_cap():
    """Verify Excalidraw inline rendering has a size cap."""
    with open('static/ui.js') as f:
        src = f.read()
    func = src[src.find('function loadExcalidrawInline()'):src.find('function loadExcalidrawInline()') + 2000]
    assert 'EXCALIDRAW_MAX_SIZE' in func, "Should have EXCALIDRAW_MAX_SIZE constant"
    assert 'excalidraw_too_large' in func, "Should use excalidraw_too_large i18n for oversized files"


def test_excalidraw_error_handling():
    """Verify Excalidraw error handling."""
    with open('static/ui.js') as f:
        src = f.read()
    func = src[src.find('function loadExcalidrawInline()'):src.find('function loadExcalidrawInline()') + 3500]
    assert 'excalidraw_error' in func, "Should use excalidraw_error i18n on fetch failure"


def test_excalidraw_svg_renderer_exists():
    """Verify SVG renderer for Excalidraw elements exists."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'function _renderExcalidrawCanvases()' in src, "Missing _renderExcalidrawCanvases function"
    render = src[src.find('function _renderExcalidrawCanvases()'):src.find('function _renderExcalidrawCanvases()') + 4000]
    assert '<svg' in render, "Should generate SVG"
    assert 'excalidraw-svg' in render, "Should use excalidraw-svg CSS class"


def test_excalidraw_renders_element_types():
    """Verify SVG renderer handles common Excalidraw element types."""
    with open('static/ui.js') as f:
        src = f.read()
    render = src[src.find('function _renderExcalidrawCanvases()'):src.find('function _renderExcalidrawCanvases()') + 4000]
    element_types = ['rectangle', 'ellipse', 'text', 'line', 'arrow', 'diamond', 'draw']
    for etype in element_types:
        assert f"el.type==='{etype}'" in render, f"Should handle element type: {etype}"


def test_excalidraw_arrow_marker():
    """Verify SVG renderer includes arrow marker definition."""
    with open('static/ui.js') as f:
        src = f.read()
    render = src[src.find('function _renderExcalidrawCanvases()'):src.find('function _renderExcalidrawCanvases()') + 4000]
    assert 'arrowhead' in render, "Should define arrowhead marker for arrows"
    assert '<marker' in render, "Should use SVG <marker> element"


def test_excalidraw_bounds_calculation():
    """Verify SVG renderer calculates viewBox from element bounds."""
    with open('static/ui.js') as f:
        src = f.read()
    render = src[src.find('function _renderExcalidrawCanvases()'):src.find('function _renderExcalidrawCanvases()') + 4000]
    assert 'viewBox' in render, "Should calculate SVG viewBox"
    assert 'minX' in render, "Should track minimum X bound"
    assert 'maxX' in render, "Should track maximum X bound"


def test_excalidraw_empty_elements():
    """Verify empty diagrams show a message."""
    with open('static/ui.js') as f:
        src = f.read()
    render = src[src.find('function _renderExcalidrawCanvases()'):src.find('function _renderExcalidrawCanvases()') + 4000]
    assert 'excalidraw_empty' in render, "Should handle empty diagrams"
    assert 'excalidraw_render_error' in render, "Should handle render errors"


def test_excalidraw_download_link():
    """Verify Excalidraw embed includes download link."""
    with open('static/ui.js') as f:
        src = f.read()
    func = src[src.find('function loadExcalidrawInline()'):src.find('function loadExcalidrawInline()') + 2000]
    assert 'excalidraw-open-link' in func, "Should include open/download link"
    assert 'excalidraw_download' in func, "Should use excalidraw_download i18n"


def test_excalidraw_called_after_render():
    """Verify loadExcalidrawInline is called after message rendering."""
    with open('static/ui.js') as f:
        src = f.read()
    assert src.count('loadExcalidrawInline()') >= 2, \
        "loadExcalidrawInline should be called at least twice"


def test_excalidraw_embed_wrap_structure():
    """Verify Excalidraw embed uses proper container structure."""
    with open('static/ui.js') as f:
        src = f.read()
    assert 'excalidraw-embed-wrap' in src, "Missing excalidraw-embed-wrap container"
    assert 'excalidraw-canvas' in src, "Missing excalidraw-canvas div"
    assert 'data-excalidraw' in src, "Missing data-excalidraw attribute"


def test_excalidraw_i18n_keys():
    """Verify Excalidraw i18n keys exist in all 7 locales."""
    with open('static/i18n.js') as f:
        src = f.read()
    required_keys = [
        'excalidraw_loading', 'excalidraw_too_large', 'excalidraw_invalid',
        'excalidraw_error', 'excalidraw_label', 'excalidraw_download',
        'excalidraw_empty', 'excalidraw_render_error',
    ]
    for key in required_keys:
        count = src.count(f"{key}:")
        assert count == 7, f"Key '{key}' found {count} times, expected 7"


def test_excalidraw_css_classes():
    """Verify Excalidraw CSS classes are defined."""
    with open('static/style.css') as f:
        src = f.read()
    required_classes = [
        'excalidraw-embed-wrap', 'excalidraw-canvas', 'excalidraw-svg',
        'excalidraw-empty', 'excalidraw-open-link',
    ]
    for cls in required_classes:
        assert cls in src, f"Missing CSS class: .{cls}"
