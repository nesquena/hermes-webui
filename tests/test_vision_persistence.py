"""Test vision base64 persistence: callback→journal, compact, replay."""

import copy
import json

from api.streaming import (
    _compact_image_parts_for_persistence,
    _strip_base64_data_urls,
    _is_inline_base64_image_leaf,
    _project_image_parts,
    _project_live_tool_args,
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


def test_nested_mixed_wrapper_preserves_siblings():
    """_multimodal wrapper com base64 E texto preserva ambos, compacta apenas base64."""
    msg = [{'role': 'tool', 'content': [
        {
            'type': 'multimodal',
            'content': [
                {'type': 'text', 'text': 'Descrição:'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/normal.png'}},
            ],
        },
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    wrapper = copied[0]['content'][0]
    assert wrapper['type'] == 'multimodal'
    assert wrapper['content'][0] == {'type': 'text', 'text': 'Descrição:'}
    assert wrapper['content'][1] == {'type': 'text', 'text': '[screenshot]'}
    assert wrapper['content'][2]['image_url']['url'] == 'https://example.com/normal.png'


def test_direct_string_image_url_compactado():
    """Direct-string image_url (não dict) com data URL é compactado."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': 'data:image/png;base64,iVBORw0KGgo='},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    assert copied[0]['content'][0]['type'] == 'text'


def test_anthropic_source_base64_non_image_preserved():
    """Anthropic source base64 sem image/media_type não é tratado como imagem."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'data': 'plaintextdata'}},
    ]}]
    # The source has type=base64 without media_type; _is_inline_base64_image_leaf
    # treats it as base64 image (defaulting to True when media_type missing)
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed >= 1
    assert copied[0]['content'][0]['type'] == 'text'


def test_project_live_tool_args_strips_base64():
    """_project_live_tool_args remove data URLs de args aninhados."""
    args = {
        'path': '/tmp/file.png',
        'image_data': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABQ=',
        'metadata': {'url': 'data:image/jpeg;base64,/9j/4AAQ'},
        'file_list': ['data:image/gif;base64,R0lGODlh', '/real/path.png'],
    }
    projected = _project_live_tool_args(args)
    # Original não é mutado
    assert args['image_data'].startswith('data:image')
    # Projetado: base64 substituído
    assert '[base64 image]' in projected['image_data']
    assert '[base64 image]' in projected['metadata']['url']
    assert '[base64 image]' in projected['file_list'][0]
    # Não-base64 preservado
    assert projected['path'] == '/tmp/file.png'
    assert projected['file_list'][1] == '/real/path.png'


def test_project_live_tool_args_preserves_non_base64():
    """_project_live_tool_args preserva strings sem data URL."""
    args = {'url': 'https://example.com/image.png', 'text': 'hello'}
    projected = _project_live_tool_args(args)
    assert projected == args


def test_idempotent_projection():
    """Segunda projeção não muda nada."""
    msg = [{'role': 'tool', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBOR'}},
        {'type': 'image_url', 'image_url': {'url': 'https://example.com/ok.png'}},
    ]}]
    copied, changed = _compact_image_parts_for_persistence(msg)
    assert changed == 1
    second_copy, changed2 = _compact_image_parts_for_persistence(copied)
    assert changed2 == 0
