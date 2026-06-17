import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_user_renderer(input_text: str) -> str:
    script = r'''
const input = JSON.parse(process.argv[1]);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function _sentSelectionContextBlockHtml(label, quoteText){
  const safeLabel=String(label||'').trim()||'Context';
  const safeQuote=String(quoteText||'').replace(/\s+$/,'');
  return `<figure class="sent-selection-context" data-selected-context="1"><figcaption class="sent-selection-context-label">${esc(safeLabel)}</figcaption><blockquote class="sent-selection-context-quote">${esc(safeQuote)}</blockquote></figure>`;
}
function _stashUserSelectedContextBlocks(text, stashContext){
  const lines=String(text||'').split('\n');
  const marker='<!-- hermes-selected-context -->';
  const out=[];
  for(let i=0;i<lines.length;i++){
    const labelMatch=lines[i].match(/^\*\*([^\n]{1,200}):\*\*\s*$/);
    if(!labelMatch){out.push(lines[i]);continue;}
    const quoteLines=[];
    let j=i+1;
    if(lines[j]!==marker){out.push(lines[i]);continue;}
    j++;
    while(j<lines.length&&/^>/.test(lines[j])){
      quoteLines.push(lines[j].replace(/^>[ \t]?/,''));
      j++;
    }
    if(!quoteLines.length){out.push(lines[i]);continue;}
    out.push(stashContext(labelMatch[1], quoteLines.join('\n')));
    i=j-1;
  }
  return out.join('\n');
}
function _renderUserFencedBlocks(text){
  const stash=[];
  const contextStash=[];
  const stashContext=(label,quote)=>{contextStash.push(_sentSelectionContextBlockHtml(label,quote));return '\x00UC'+(contextStash.length-1)+'\x00';};
  let s=String(text||'');
  s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g,(_,lead,_fence,info,code)=>{
    stash.push(`<pre><code>${esc(code||'')}</code></pre>`);
    return lead+'\x00UF'+(stash.length-1)+'\x00';
  });
  s=_stashUserSelectedContextBlocks(s, stashContext);
  s=esc(s).replace(/\n/g,'<br>');
  s=s.replace(/\x00UF(\d+)\x00/g,(_,i)=>stash[+i]);
  s=s.replace(/\x00UC(\d+)\x00/g,(_,i)=>contextStash[+i]||'');
  return s;
}
process.stdout.write(_renderUserFencedBlocks(input));
'''
    result = subprocess.run(
        ["node", "-e", script, json.dumps(input_text)],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return result.stdout


def test_sent_selected_context_renders_as_semantic_card_and_escapes_content():
    html = _run_user_renderer(
        "Here is my reply.\n\n**<img src=x onerror=alert(1)>:**\n"
        "<!-- hermes-selected-context -->\n> <script>alert(1)</script>\n> second line"
    )

    assert '<figure class="sent-selection-context" data-selected-context="1">' in html
    assert '<figcaption class="sent-selection-context-label">&lt;img src=x onerror=alert(1)&gt;</figcaption>' in html
    assert '<blockquote class="sent-selection-context-quote">&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line</blockquote>' in html
    assert "**<img" not in html
    assert "&gt; &lt;script" not in html
    assert "<script>" not in html


def test_sent_selected_context_parser_accepts_user_renamed_edge_labels():
    html = _run_user_renderer("***Evidence: alpha:**\n<!-- hermes-selected-context -->\n> quoted")

    assert 'class="sent-selection-context"' in html
    assert '<figcaption class="sent-selection-context-label">*Evidence: alpha</figcaption>' in html
    assert "***Evidence" not in html


def test_matching_shape_inside_user_code_fence_stays_code_not_context_card():
    html = _run_user_renderer("```md\n**Evidence:**\n> keep literal\n```")

    assert 'class="sent-selection-context"' not in html
    assert '<pre><code>' in html
    assert '**Evidence:**' in html
    assert '&gt; keep literal' in html


def test_manual_labelled_blockquote_without_marker_stays_literal_user_text():
    html = _run_user_renderer("**Manual note:**\n> manually typed quote")

    assert 'class="sent-selection-context"' not in html
    assert '**Manual note:**' in html
    assert '&gt; manually typed quote' in html
