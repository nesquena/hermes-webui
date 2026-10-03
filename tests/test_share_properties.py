"""Deterministic public-share snapshot and production-renderer properties."""
from __future__ import annotations
import base64
import copy
from contextlib import contextmanager
import faulthandler
import html
import hashlib
import json
import posixpath
import random
import re
import shutil
import signal
import struct
import subprocess
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, quote_from_bytes, unquote, urlsplit

import pytest
from api import shares
from api.models import Session
from tests.test_data_uri_images import _DRIVER_SRC

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT: Path | None = None
SEEDS = (7868, 20261003, 5391774930)
PRIVATE = "https://webui.example/api/media?path=/tmp/private.png"
PUBLIC = "https://cdn.example/public.png"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.fixture(scope="module", autouse=True)
def property_artifact_directory(tmp_path_factory):
    global ARTIFACT
    ARTIFACT = tmp_path_factory.mktemp("share_property_artifacts")
    yield
    print(f"SHARE_PROPERTY_ARTIFACTS={ARTIFACT}", flush=True)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images, self.links = [], []
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img": self.images.append(values.get("src", ""))
        if tag == "a": self.links.append(values.get("href", ""))


def private_route(value):
    # Independent output oracle: interpret emitted HTML src, not sanitizer refs.
    if value.lower().startswith("data:"): return False
    for _ in range(5):
        decoded = html.unescape(unquote(value))
        if decoded == value: break
        value = decoded
    if "file:" in value.lower(): return True
    candidates = [value] + [value[m.start():] for m in re.finditer(r"https?://", value, re.I)]
    for candidate in candidates:
        candidate = re.split(r"[\s<>\"'`\]\)]", candidate, maxsplit=1)[0]
        try: parsed = urlsplit(candidate.replace("\\", "/"))
        except ValueError: continue
        path = posixpath.normpath(re.sub(r"/+", "/", parsed.path)).lower()
        keys = [field.partition("=")[0].lower() for field in parsed.query.split("&") if "=" in field]
        if path == "/api/media" and "path" in keys: return True
    return False


@pytest.fixture(scope="module")
def renderer(tmp_path_factory):
    assert NODE, "Node is required for production-renderer evidence"
    path = tmp_path_factory.mktemp("share_systematic") / "batch.js"
    old = "process.stdout.write(renderMd(buf));"
    assert _DRIVER_SRC.count(old) == 1
    path.write_text(_DRIVER_SRC.replace(old, "process.stdout.write(JSON.stringify(JSON.parse(buf).map(x=>renderMd(x))));"))
    def render(bodies,timeout=60):
        answer = subprocess.run([NODE, str(path), str(ROOT / "static/ui.js")], input=json.dumps(bodies), text=True, capture_output=True, timeout=timeout, check=True)
        result = json.loads(answer.stdout)
        assert len(result) == len(bodies)
        return result
    return render


def snapshot(body, title=None, workspace=None):
    session = Session(session_id="systematic-share", title=title or "Probe", messages=[{"role":"assistant", "content":body}])
    if workspace is not None: session.workspace=workspace
    before = copy.deepcopy(vars(session))
    result = shares.build_share_snapshot(session)
    assert vars(session) == before, "source Session mutated"
    return result


def links(markup):
    parsed=Links();parsed.feed(markup);return parsed


def save(name, value):
    assert ARTIFACT is not None
    ARTIFACT.mkdir(exist_ok=True)
    (ARTIFACT / (name + ".json")).write_text(json.dumps(value, indent=2, ensure_ascii=False))


def verify(cases, renderer, name):
    start=time.monotonic(); rows=[]; failures=[]; snapshots=[]
    for index, case in enumerate(cases):
        try:
            snap=snapshot(case["body"], case.get("title"))
            content=snap["messages"][0]["content"]
            second=snapshot(content, snap["title"])
            row={"case":case,"snapshot":content,"title":snap["title"],"idempotent":second==snap}
            if second!=snap:failures.append({"property":"idempotence",**row})
        except Exception as exc:
            row={"case":case,"exception":type(exc).__name__+":"+str(exc)}
            failures.append({"property":"exception",**row});content=""
        rows.append(row); snapshots.append(content)
    rendered=[]
    for index in range(0,len(snapshots),128):rendered.extend(renderer(snapshots[index:index+128]))
    for row, markup in zip(rows, rendered, strict=True):
        images=links(markup).images
        row["image_srcs"]=images
        bad=[value for value in images if private_route(value)]
        if bad:failures.append({"property":"private-image",**row,"private_srcs":bad})
        if row["case"].get("preserve") and row.get("snapshot")!=row["case"]["body"]:
            failures.append({"property":"public-text",**row})
    evidence={"count":len(cases),"seconds":time.monotonic()-start,"failures":failures,"cases":rows}
    save(name,evidence)
    print(f"PROBE {name}: {len(cases)} cases, {len(failures)} failures, {evidence['seconds']:.2f}s", flush=True)
    assert not failures, json.dumps(failures[:3],ensure_ascii=False)[:4000]
    return evidence


def private_refs():
    refs=[PRIVATE,
      "https://webui.example/api/./media?path=x",
      "https://webui.example/api/a/../media?path=x",
      "https://webui.example/api//media?path=x",
      "https://webui.example/%61pi/%6Dedia?%70ath=x",
      "https://webui.example/api/media?x=1&amp;path=x",
      "https://webui.example/api/media?pa&#x74;h=x",
      "https://webui.example/API/MEDIA?PATH=x",
      "https://webui.example\\api\\media?path=x",
      "https://webui.example/api/media?path=日本語.png",
      "https://webui.example/api/media?path=%F0%9F%93%84.png",
      "https://webui.example/api/media?path=x&z=1",
      "file:///tmp/private.png"]
    refs += ["https://cdn.example/r?next="+quote(PRIVATE,safe=""),"https://cdn.example/r?next="+quote(quote(PRIVATE,safe=""),safe=""),"https://cdn.example/r?next=file%3A%2F%2F%2Ftmp%2Fprivate.png"]
    return refs


def templates(ref):
    img=f"![private]({ref})"
    forms=[img,f"![private]({ref} \"caption\")",f"![private](<{ref}>)",f"MEDIA:{ref}",f"`MEDIA:{ref}`",f'"MEDIA:{ref}"',f"'MEDIA:{ref}'"]
    forms += [prefix+img for prefix in ("![a](x ","![a](chart.png ","![a](x\n","![a](x\r\n","![a](x\t","![a](x\u00a0","![a](<x> ","![a](HTTPS://cdn.example/x ")]
    forms += ["![a](x "*depth+img for depth in (2,3,6)]
    forms += [f"![outer](https://cdn.example/r?next={img})",f"![left]({PUBLIC}) "+img+f" ![right]({PUBLIC})"]
    return forms


def test_handled_cartesian_matrix(renderer):
    cases=[{"kind":"handled-private","body":body,"ref":ref} for ref in private_refs() for body in templates(ref)]
    verify(cases,renderer,"handled-cartesian")


@pytest.mark.parametrize("seed",SEEDS)
def test_seeded_combinations(renderer,seed):
    rng=random.Random(seed);cases=[]
    for index in range(400):
        ref=rng.choice(private_refs());body=rng.choice(templates(ref))
        if rng.choice((True,False)):
            body=f"![left]({PUBLIC}) "+body+f" ![right]({PUBLIC})"
        if rng.randrange(3)==0:body="前文 📎 "+body+" 後文"
        cases.append({"seed":seed,"index":index,"body":body})
    verify(cases,renderer,"seed-"+str(seed))


def test_public_lookalikes(renderer):
    refs=[PUBLIC,"https://cdn.example/api/media/photos.png","https://cdn.example/albums/api/media/p.png#path=x","https://cdn.example/api/media?pathology=x","https://cdn.example/api/media?x=1#path=x","https://[2001:db8::1]/public.png","https://cdn.example/日本語.png","https://cdn.example/public.png?caption=path%3Dx"]
    cases=[{"body":body,"preserve":True} for ref in refs for body in (f"![p]({ref})",f"![p]({ref} \"caption\")",f"MEDIA:{ref}",f'"MEDIA:{ref}"') if not ("[" in ref and "MEDIA:" in body)]
    verify(cases,renderer,"public-lookalikes")




def test_mutation_bite(renderer,monkeypatch):
    bad_regex=re.compile(r"!\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)\r\n]+))(?:\s+[^)]*)?\s*\)",re.I)
    with monkeypatch.context() as local:
        local.setattr(shares,"_SHARE_MARKDOWN_IMAGE_RE",bad_regex)
        with pytest.raises(AssertionError):verify([{"body":f"![a](x ![b]({PRIVATE})"}],renderer,"mutation-old-regex")
    with monkeypatch.context() as local:
        local.setattr(shares,"_share_media_ref_is_private",lambda raw:False)
        with pytest.raises(AssertionError):verify([{"body":f"![private]({PRIVATE})"}],renderer,"mutation-no-private-classifier")


def png_bytes(width=3,height=2,metadata=b''):
    def chunk(kind,payload):
        return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
    rng=random.Random(7868+width*height)
    pixels=b''.join(b'\x00'+rng.randbytes(width*3) for _ in range(height))
    result=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
    if metadata: result+=chunk(b'tEXt',b'Comment\x00'+metadata)
    return result+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


def image_forms(data,mime='png'):
    encoded=base64.b64encode(data).decode()
    # Actual decoder-compatible forms: no percent double-decode, preserve literal +.
    return {
        'base64':f'data:image/{mime};base64,'+encoded,
        'rawpercent':f'data:image/{mime},'+quote_from_bytes(data,safe=':/'),
        'escapedpercent':f'data:image/{mime},'+quote_from_bytes(data,safe=''),
        'escapedbase64':f'data:image/{mime};base64,'+encoded.replace('/','%2F').replace('+','%2B'),
        'unpadded':f'data:image/{mime};base64,'+encoded.rstrip('=').replace('/','%2F').replace('+','%2B'),
        'asciiwrapped':f'data:image/{mime};base64,'+'%0D%0A'.join(encoded[i:i+64] for i in range(0,len(encoded),64)),
    }


def fetch_hashes(uris):
    # Node native fetch gives independent once-decoded WHATWG data URI bytes.
    script="const fs=require('fs'),crypto=require('crypto');(async()=>{const xs=JSON.parse(fs.readFileSync(0,'utf8'));const out=[];for(const x of xs){try{const b=Buffer.from(await(await fetch(x)).arrayBuffer());out.push({length:b.length,sha256:crypto.createHash('sha256').update(b).digest('hex')})}catch(e){out.push({error:String(e)})}}process.stdout.write(JSON.stringify(out))})()"
    return json.loads(subprocess.run([NODE,'-e',script],input=json.dumps(uris),text=True,capture_output=True,check=True,timeout=60).stdout)


def test_renderer_batch_correctness(renderer,tmp_path):
    bodies=[f'![p]({PUBLIC})',f'![p]({PRIVATE})','MEDIA:https://[2001:db8::1]/public.png','![a](x ![p]('+PRIVATE+')','![p]('+image_forms(png_bytes())['rawpercent']+')']
    single=tmp_path/'single.js';single.write_text(_DRIVER_SRC)
    originals=[subprocess.run([NODE,str(single),str(ROOT/'static/ui.js')],input=body,text=True,capture_output=True,check=True).stdout for body in bodies]
    assert renderer(bodies)==originals
    save('batch-calibration',[{'body':b,'original':m,'image_srcs':links(m).images} for b,m in zip(bodies,originals,strict=True)])


def test_title_matrix(renderer):
    cases=[]
    for ref in private_refs():
        for token in (f'MEDIA:{ref}',f'`MEDIA:{ref}`',f'"MEDIA:{ref}"',f"'MEDIA:{ref}'",f'![private]({ref})',f'![a](x ![private]({ref})'):
            cases.append({'body':'body', 'title':'前文 '+token+' 後文'})
    evidence=verify(cases,renderer,'title-private-matrix')
    for row in evidence['cases']:
        assert not private_route(row['title']), row
    publics=[f'Reference {token} here' for token in (f'MEDIA:{PUBLIC}',f'`MEDIA:{PUBLIC}`',f'"MEDIA:{PUBLIC}"',f"'MEDIA:{PUBLIC}'",f'![p]({PUBLIC})')]
    for title in publics: assert snapshot('body',title)['title']==title
    save('title-public-controls',publics)


def test_real_image_byte_neighbors(renderer):
    images={'smallPNG':png_bytes(metadata=b'inert file:///tmp/description http://host/api/media?path=sample'),'largePNG':png_bytes(120,100,metadata='日本語'.encode()),'JPEG':(ROOT/'tests/fixtures/multipicture.jpg').read_bytes(),'GIF':base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')}
    variants=[]
    for name,data in images.items():
        mime={'JPEG':'jpeg','GIF':'gif'}.get(name,'png')
        for form,uri in image_forms(data,mime).items():
            variants.append((name,form,uri,data))
    hashes=fetch_hashes([row[2] for row in variants])
    rows=[];cases=[]
    for (name,form,uri,data),decoded in zip(variants,hashes,strict=True):
        assert decoded=={'length':len(data),'sha256':hashlib.sha256(data).hexdigest()},(name,form,decoded)
        markup=renderer([f'![real]({uri})'])[0]
        assert uri in links(markup).images,(name,form,markup[:100])
        rows.append({'name':name,'form':form,'uri_length':len(uri),'decoded':decoded})
        for prefix in ('','![a](x ','![a](HTTPS://public/x ','![a](<x> ','![a](x\r\n'):
            body=prefix+f'![real]({uri}) ![private]({PRIVATE}) ![public]({PUBLIC})'
            cases.append({'body':body,'expected':uri,'form':form,'name':name})
        for token in (f'MEDIA:{uri}',f'`MEDIA:{uri}`',f'"MEDIA:{uri}"'):
            title='Reference '+token+' here'
            assert snapshot('body',title)['title']==title,(name,form)
    evidence=verify(cases,renderer,'real-image-neighbors')
    for row in evidence['cases']:
        assert row['case']['expected'] in row['image_srcs'],row['case']['name']+':'+row['case']['form']
        assert PUBLIC in row['image_srcs'],row['case']['name']+':'+row['case']['form']
    save('real-byte-decoder',rows)


def test_mime_policy_and_malformed(renderer):
    # MIME grammar policy, distinct from pixel-container proof above.
    valid=[];invalid=[]
    for mime in ('png','jpg','jpeg','gif','webp','avif'):
        for value in ('YQ==','YQ%3D%3D','YQ','Y%0AQ%3D%3D','Y%09Q%3D%3D','Y%0CQ%3D%3D','Y%20Q%3D%3D','YQ%3D=','A%2F'):
            uri=f'data:image/{mime};base64,'+value
            valid.append({'body':f'![p]({uri})','preserve':True,'uri':uri})
        uri=f'data:image/{mime},%41file:///tmp/inert'
        valid.append({'body':f'![p]({uri})','preserve':True,'uri':uri})
        for value in ('Y%252FQ==','YQ%3D%3D?junk','YQ%3D%3D#junk','YQ%3D%3Dfile%3A%2F%2F%2Ftmp%2Fx','YQ%3D%3D%0B','Y%FFQ==','YQ%3D%3D%7F','Y%25Q=='):
            uri=f'data:image/{mime};base64,'+value
            invalid.append({'body':f'![p]({uri})','uri':uri})
    svg='data:image/svg+xml;base64,'+base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()
    valid.append({'body':f'![p]({svg})','preserve':True,'uri':svg})
    good=verify(valid,renderer,'mime-policy-valid')
    for row in good['cases']: assert row['case']['uri'] in row['image_srcs']
    bad=verify(invalid,renderer,'malformed-escaped-base64')
    for row in bad['cases']:
        assert row['case']['uri'] not in row['snapshot'],row['case']['uri']



@contextmanager
def bounded_deadline(seconds):
    def expired(*_args):
        raise TimeoutError(f'isolated snapshot exceeded {seconds}s capacity budget')
    old=signal.signal(signal.SIGALRM,expired)
    faulthandler.dump_traceback_later(seconds-1)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        faulthandler.cancel_dump_traceback_later()
        signal.signal(signal.SIGALRM,old)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="capacity watchdog requires POSIX signals")
def test_uri_size_boundaries_and_complexity(renderer):
    limit=2*1024*1024; rows=[];cases=[]
    for size in (16383,16384,16385,limit-1,limit,limit+1):
        for header in ('data:image/png,','data:image/png;base64,','data:image/png;base64,%2F'):
            uri=header+'A'*(size-len(header))
            # Escaped base64 requires decoding length modulo !=1.
            valid=(size<=limit and (header.endswith(',') or len(unquote(uri.partition(',')[2]))%4!=1))
            print(f'SIZE phase=body chars={size} header={header}',flush=True)
            start=time.monotonic()
            with bounded_deadline(15): result=snapshot(f'![p]({uri})')
            elapsed=time.monotonic()-start
            content=result['messages'][0]['content']; rendered=renderer([content])[0]
            assert (uri in links(rendered).images)==valid,(size,header,valid)
            print(f'SIZE phase=title chars={size} header={header} body_seconds={elapsed:.3f}',flush=True)
            with bounded_deadline(15): title=snapshot('body',f'`MEDIA:{uri}`')['title']
            assert (title==f'`MEDIA:{uri}`')==valid,(size,header,'title')
            rows.append({'bytes':size,'header':header,'expected_acceptance':valid,'snapshot_seconds':elapsed})
    for depth in (1,8,32,64,128,256,512):
        body='![a](x '*depth+f'![private]({PRIVATE}) ![public]({PUBLIC})'
        cases.append({'body':body,'depth':depth})
    evidence=verify(cases,renderer,'nested-complexity')
    for row in evidence['cases']: assert PUBLIC in row['image_srcs']
    save('size-capacity',rows)
    print('CAPACITY '+json.dumps(rows),flush=True)




def test_protected_image_spans_and_adjacent_files(renderer):
    data=png_bytes(metadata=b'inert file:///etc/x')
    cases=[];titles=[]
    for form,uri in image_forms(data).items():
        for right in (' file:///tmp/outside', '`file:///tmp/outside`', '![p](file:///tmp/outside)', 'file:///tmp/outside'):
            body=f'![real]({uri})'+right+f' ![public]({PUBLIC})'
            cases.append({'body':body,'expected':uri,'form':form})
            titles.append((uri,f'`MEDIA:{uri}`'+right))
        for token in (f'`MEDIA:{uri}`',f'"MEDIA:{uri}"',f"'MEDIA:{uri}'"):
            # Paired quote followed by non-punctuation is one malformed MEDIA
            # ref under the production splitter; only backticks close here.
            titles.append((uri,token+' file:///tmp/outside'))
            if token.startswith('`'): titles.append((uri,token+'file:///tmp/outside'))
    result=verify(cases,renderer,'image-span-adjacency')
    for row in result['cases']:
        assert row['case']['expected'] in row['image_srcs']
        assert PUBLIC in row['image_srcs']
        assert 'file:///tmp/outside' not in row['snapshot']
    for uri,title in titles:
        value=snapshot('body',title)['title']
        assert uri in value and 'file:///tmp/outside' not in value,(title,value)
    save('image-span-title-adjacency',{'count':len(titles)})


def test_encoded_depth_and_file_matrix(renderer):
    refs=[]
    for depth in range(7):
        ref=PRIVATE
        for _ in range(depth): ref=quote(ref,safe='')
        refs.append((depth,'https://cdn.example/r?next='+ref))
    forms=[]
    for depth,ref in refs:
        for body in templates(ref): forms.append({'body':body,'depth':depth})
    files=('file:///tmp/private.png','FILE:///tmp/private.png','file%3A%2F%2F%2Ftmp%2Fprivate.png','file&amp;#58;///tmp/private.png','file&#58;///tmp/private.png')
    for ref in files:
        for body in (ref,f'`{ref}`',f'![p]({ref})',f'[p]({ref})',f'![left]({PUBLIC}) {ref} ![right]({PUBLIC})'):
            forms.append({'body':body,'ref':ref})
    verify(forms,renderer,'decode-depth-file-matrix')


def test_real_preservation_mutation_bite(renderer,monkeypatch):
    uri=image_forms(png_bytes(120,100,metadata=b'file:///inert'))['rawpercent']
    with monkeypatch.context() as local:
        local.setattr(shares,'_share_media_ref_is_self_contained_image',lambda _raw:False)
        with pytest.raises(AssertionError):
            verify([{'body':f'![p]({uri})','preserve':True}],renderer,'mutation-image-helper')
    assert uri in links(renderer([snapshot(f'![p]({uri})')['messages'][0]['content']])[0]).images


@pytest.mark.parametrize('seed',SEEDS)
def test_seeded_grammar_shapes(renderer,seed):
    rng=random.Random(seed^0x7868);cases=[]
    destinations=('x','chart.png','HTTPS://cdn.example/x','<x>','<x','DATA:image/png,x','[unknown]','日本語','')
    gaps=(' ','\t','\n','\r\n','\u00a0',' "caption" ',' \"a ) title\" ','\u2028')
    labels=('a','nested [alt','日本語📎','quote " x','`a`','a![')
    endings=('',')',' "title")','\n)',') ![public]('+PUBLIC+')')
    for index in range(600):
        ref=rng.choice(private_refs())
        leaf=f'![{rng.choice(labels)}]({ref})'
        for _ in range(rng.randrange(5)):
            leaf='!['+rng.choice(labels)+']('+rng.choice(destinations)+rng.choice(gaps)+leaf+rng.choice(endings)
        if rng.randrange(2): leaf='![left]('+PUBLIC+') '+leaf+' ![right]('+PUBLIC+')'
        if rng.randrange(4)==0: leaf='```\n'+leaf+'\n```\n![active]('+ref+')'
        cases.append({'body':leaf,'seed':seed,'index':index})
    verify(cases,renderer,'grammar-seed-'+str(seed))








def test_bounded_patterns_preserve_original_matches_and_snapshots(renderer, monkeypatch):
    rng=random.Random(7868);bodies=[]
    for ref in private_refs()+[PUBLIC]: bodies.extend(templates(ref))
    bodies.extend(['![]('+x+')' for x in (' ',' \n','\t\r\n','<x)>','<x)![a]>','<https://x)![a]>','< x > \n','<x>','<x> ', '<x> ![a](x')])
    for i in range(6000):
        base=rng.choice(bodies[:340])
        prefix=rng.choice(('![a[b](','![a![b](','![a\nb](','![a](<','![a](x ', '!['*rng.randrange(1,10)))
        bodies.append(prefix+base+rng.choice(('',')','> )','\n)')))
    data=png_bytes(120,100,metadata=b'file:///inert')
    for uri in image_forms(data).values():
        for alt in ('a[b','a![b','a[![b','a![b![c'):
            bodies.append(f'![{alt}]({uri}) ![p]({PRIVATE})')
    for name in ("_SHARE_MARKDOWN_IMAGE_RE", "_SHARE_FILE_MARKDOWN_RE"):
        wrapped = getattr(shares, name)
        original = wrapped.original
        for body in bodies:
            expected=[(m.span(),m.groups()) for m in original.finditer(body)]
            actual=[(m.span(),m.groups()) for m in wrapped.finditer(body)]
            assert actual==expected,(name,body,expected,actual)
            assert wrapped.sub('<replacement>',body)==original.sub('<replacement>',body)
    bounded = [snapshot(body) for body in bodies]
    with monkeypatch.context() as legacy:
        for name in ("_SHARE_MARKDOWN_IMAGE_RE", "_SHARE_FILE_MARKDOWN_RE"):
            legacy.setattr(shares, name, getattr(shares, name).original)
        assert [snapshot(body) for body in bodies] == bounded
    verify([{'body':body} for body in bodies],renderer,'bounded-pattern-differential')


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="capacity watchdog requires POSIX signals")
@pytest.mark.parametrize('kind', ['unclosed_alt', 'missing_close', 'broken_angle', 'unknown_angle', 'unknown_bare'])
def test_malformed_markdown_snapshot_capacity(kind):
    forms = {
        'unclosed_alt': lambda n: '![' * n,
        'missing_close': lambda n: '![a](https://cdn.example/' * n,
        'broken_angle': lambda n: '![a](<https://cdn.example/' * n + ')',
        'unknown_angle': lambda n: '![a](<x ' * n + '>)',
        'unknown_bare': lambda n: '![a](x ' * n + ')',
    }
    rows = []
    for count in (256, 1024, 8192, 32768):
        body = forms[kind](count)
        started = time.monotonic()
        # An expired budget must fail the test; it is never a passing diagnostic.
        with bounded_deadline(8):
            result = snapshot(body)
            assert result['messages'][0]['content'] == body
            assert snapshot(body) == result
        rows.append({'count': count, 'chars': len(body), 'seconds': time.monotonic() - started})
    save('malformed-capacity-' + kind, rows)
