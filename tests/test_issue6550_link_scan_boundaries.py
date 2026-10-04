"""Real renderMd boundaries: malformed predecessors and literal raw code."""
import html
from html.parser import HTMLParser
import itertools

import pytest

from tests import test_renderer_js_behaviour as _renderer

_render = _renderer._render
driver_path = _renderer.driver_path


class _Rendered(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.code = []
        self._in_code = False
        self._code_text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs).get('href'))
        if tag == 'code':
            self._in_code = True
            self._code_text = []

    def handle_endtag(self, tag):
        if tag == 'code':
            self.code.append(''.join(self._code_text))
            self._in_code = False

    def handle_data(self, data):
        if self._in_code:
            self._code_text.append(data)


@pytest.mark.parametrize('context', ['{}', '- {}', '> {}', '| label |\n| --- |\n| {} |'])
@pytest.mark.parametrize('bad', ['[bad](https://broken.test/path ', '[bad](file:///tmp/broken path ', '[bad](https://broken.test/path [broken](https://broken.test/ '])
@pytest.mark.parametrize('good', ['[Good](https://good.test/path)', '[Good](https://good.test/a b.pdf "Title")'])
def test_malformed_predecessor_never_consumes_valid_successor(driver_path, context, bad, good):
    expected = _Rendered(_render(driver_path, context.format(good))).links
    actual = _Rendered(_render(driver_path, context.format(bad + good))).links
    assert expected[-1] in actual
    assert not any('[Good]' in (link or '') for link in actual)
    assert '[bad]' in html.unescape(_render(driver_path, context.format(bad + good)))


@pytest.mark.parametrize('context', ['See {} here', '- {}', '> {}', '| label |\n| --- |\n| {} |'])
@pytest.mark.parametrize('literal', ['[Literal](https://gw.example/a b.pdf)', 'https://gw.example/a b.pdf', '**[Literal](https://gw.example/a b.pdf)**', '[Literal](file:///tmp/a b.pdf)'])
def test_raw_code_is_literal_through_all_link_passes(driver_path, context, literal):
    rendered = _Rendered(_render(driver_path, context.format('<code>' + literal + '</code>')))
    assert rendered.links == []
    assert rendered.code == [literal]


@pytest.mark.parametrize('dest', ['https://good.test/a b.pdf', 'file:///tmp/a b.pdf', 'mailto:a@example.test', 'https://good.test/a%20b.pdf'])
def test_invalid_prefix_insertion_preserves_successor_link_property(driver_path, dest):
    target = f'[Good]({dest})'
    expected = _Rendered(_render(driver_path, target)).links
    for count, separator in itertools.product([1, 2, 8, 32], [' ', '\t']):
        prefix = ('[bad](https://broken.test/path' + separator) * count
        rendered = _Rendered(_render(driver_path, prefix + target))
        assert expected[-1] in rendered.links
        assert not any('[Good]' in (link or '') for link in rendered.links)


@pytest.mark.parametrize('literal', ['`backticks` [Literal](https://gw.example/a b.pdf)', '$x$ [Literal](https://gw.example/a b.pdf)', 'A &amp; B [Literal](https://gw.example/a b.pdf)', 'file:///tmp/a.pdf [Literal](https://gw.example/a b.pdf)'])
@pytest.mark.parametrize('wrapper', ['<code>{}</code>', '<pre><code>{}</code></pre>'])
def test_raw_preformatted_regions_keep_nested_syntax_and_entities(driver_path, literal, wrapper):
    source = _render(driver_path, wrapper.format(literal))
    rendered = _Rendered(source)
    assert rendered.links == []
    assert rendered.code == [html.unescape(literal)]
    assert '\x00' not in source


@pytest.mark.parametrize('url', ['https://good.test/a[part](name', 'https://good.test/a%5Bpart%5D%28name%29'])
def test_attached_bracket_bytes_do_not_create_a_separate_link(driver_path, url):
    source = _render(driver_path, f'[Good]({url})')
    assert _Rendered(source).links == [url]


@pytest.mark.parametrize('title', ['"See [Other](https://other.test/path)"', "'See [Other](https://other.test/path)'"])
def test_link_looking_title_text_does_not_split_a_valid_destination(driver_path, title):
    source = _render(driver_path, f'[Good](https://good.test/a b.pdf {title})')
    assert _Rendered(source).links == ['https://good.test/a b.pdf']


def test_repeated_spaced_bad_openers_keep_successor_with_bounded_growth(tmp_path):
    import json
    import subprocess
    script = _renderer._TIMING_DRIVER_SRC.replace(
        "const inputs = [('[x](').repeat(4096), ('[x](').repeat(8192)];",
        "const inputs = [2048,4096,8192].map(n => '[bad](https://broken.test/path '.repeat(n)+'[Good](https://good.test/path)');",
    )
    path = tmp_path / 'bad_opener_growth.js'
    path.write_text(script)
    result = subprocess.run([_renderer.NODE, str(path), str(_renderer.UI_JS_PATH)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    small, middle, large = json.loads(result.stdout)['medians']
    assert large < 500, (small, middle, large)
    assert large <= max(3 * middle, 3 * small, 20), (small, middle, large)


def _live_hrefs(markdown, chunk_size, *, repeat_sizes=None):
    import json
    import subprocess

    script = r'''
import fs from 'node:fs';
import * as smd from './static/vendor/smd.min.js';
const source=fs.readFileSync('static/ui.js','utf8');
const start=source.indexOf('function _normalizeMarkdownLinkDestination(');
const end=source.indexOf('\nfunction ',start+1);
const normalize=(0,eval)('('+source.slice(start,end)+')');
const messages=fs.readFileSync('static/messages.js','utf8');
const hrefStart=messages.indexOf('function _smdLinkHref(');
const hrefEnd=messages.indexOf('\n  function ',hrefStart+1);
const linkHref=(new Function('_normalizeMarkdownLinkDestination','_sessionUrlForSid',
  'return ('+messages.slice(hrefStart,hrefEnd)+');'))(
    normalize,sid=>'/app/session/'+encodeURIComponent(sid));
const safe=(0,eval)(messages.match(/const _SMD_SAFE_URL_RE=([^;\n]+);/)[1]);
const input=JSON.parse(process.argv[1]);
function run(text,chunkSize){
const hrefs=[];
const parser=smd.parser({
  data:{},add_token(){},end_token(){},add_text(){},
  set_attr(_data,attr,value){
    if(attr===smd.HREF){
      const href=linkHref(value);
      if(safe.test(href)) hrefs.push(href);
    }
  },
});
for(let i=0;i<text.length;i+=chunkSize)
  smd.parser_write(parser,text.slice(i,i+chunkSize));
smd.parser_end(parser);
return hrefs;
}
if(input.repeatSizes){
  run(input.text,1);
  const counts=[],medians=[];
  for(const n of input.repeatSizes){
    const text='[bad](https://broken.test/path '.repeat(n)+input.text;
    const samples=[];
    for(let i=0;i<3;i++){
      const start=performance.now(),hrefs=run(text,input.chunkSize);
      samples.push(performance.now()-start);
      if(i===0)counts.push(hrefs.length);
    }
    medians.push(samples.sort((a,b)=>a-b)[1]);
  }
  console.log(JSON.stringify({counts,medians}));
}else console.log(JSON.stringify(run(input.text,input.chunkSize)));
'''
    result = subprocess.run(
        [_renderer.NODE, '--input-type=module', '-e', script,
         json.dumps({'text': markdown, 'chunkSize': chunk_size,
                     'repeatSizes': repeat_sizes})],
        cwd=_renderer.REPO_ROOT, capture_output=True, text=True, timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize('chunk_size', [1, 7, 4096])
@pytest.mark.parametrize('markdown', [
    '[bad](https://broken.test/path [Good](https://good.test/path)',
    '[bad](https://broken.test/path\t[Good](https://good.test/a b.pdf "Title")',
    '[bad](https://broken.test/path [broken](https://broken.test/ [Good](https://good.test/path)',
    '[Good](https://good.test/a b.pdf)',
    '[Good](https://good.test/a[part](name)',
    '[Good](https://good.test/a b.pdf "Title")',
    '[bad](https://broken.test/path [Good](mailto:good@example.test)',
    '[bad](https://broken.test/path [Good](file:///tmp/report final.pdf)',
    '[bad](https://broken.test/path [Good](workspace://reports/report.pdf)',
    '[bad](javascript:bad [Good](https://good.test/path)',
    '[bad](https://broken.test/path [Good](javascript:bad)',
])
def test_live_and_settled_link_destinations_agree(driver_path, markdown, chunk_size):
    expected = _Rendered(_render(driver_path, markdown)).links
    assert _live_hrefs(markdown, chunk_size) == expected


@pytest.mark.parametrize('title', [
    '"See [Other](https://other.test/path)"',
    "'See [Other](https://other.test/path)'",
    '(See [Other](https://other.test/path))',
])
def test_live_title_lookalikes_do_not_gain_successor_anchors(title):
    # Existing SMD title grammar is unchanged; the boundary patch must not turn
    # text inside a title into an extra anchor.
    hrefs = _live_hrefs(f'[Good](https://good.test/a b.pdf {title})', 1)
    assert len(hrefs) == 1
    assert not any(href == 'https://other.test/path' for href in hrefs)


def test_live_successor_recovery_keeps_bounded_growth():
    sizes = [2048, 4096, 8192]
    result = _live_hrefs('[Good](https://good.test/path)', 7, repeat_sizes=sizes)
    assert result['counts'] == [n + 1 for n in sizes]
    small, middle, large = result['medians']
    assert large < 500, result
    assert large <= max(3 * middle, 3 * small, 20), result
