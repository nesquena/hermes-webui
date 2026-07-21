"""Test vision base64 persistence: callback→journal, compact, replay."""

import copy
import json

from api.streaming import (
    _compact_image_parts_for_persistence,
    _strip_base64_data_urls,
    _part_is_inline_base64_image,
    _tool_result_snippet,
)


def test_callback_journal_no_base64():
    """_tool_result_snippet com _multimodal → sem base64 no resultado."""
    raw = {"type": "function_result", "content": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ="}
    result = _tool_result_snippet(raw)
    # Base64 data is replaced with [base64 image] placeholder — no raw payload
    assert 'base64,' not in result
    assert '[base64 image]' in result


def test_non_base64_images_preserved():
    """http/file references sobrevivem à compactação."""
    msg = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.com/img.png'}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 0
    assert copied[0]['content'][0]['image_url']['url'] == 'https://example.com/img.png'


def test_base64_inline_replaced():
    """data:image base64 compactado."""
    msg = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    assert copied[0]['content'][0]['type'] == 'text'


def test_anthropic_source_base64():
    """Anthropic source: {type: 'base64'} compactado."""
    msg = [{'role': 'tool', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}}]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1


def test_in_place_not_mutated():
    """Objeto original não é alterado."""
    original = [{'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}}]}]
    frozen = copy.deepcopy(original)
    _compact_image_parts_for_persistence(original)
    assert original == frozen


def test_mixed_images():
    """Trecho com imagens base64 E http preserva http, compacta base64."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
        {'type': 'image_url', 'image_url': {'url': 'https://example.com/ok.png'}},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 1
    assert copied[0]['content'][0]['type'] == 'text'
    assert copied[0]['content'][1]['image_url']['url'] == 'https://example.com/ok.png'
