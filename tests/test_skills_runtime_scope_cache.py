"""Run production panels/sessions in Node with deferred skill responses.

The browser's skill working sets belong to one profile generation and each
loader's newest request, including error rendering and incomplete results.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node not on PATH')

NODE_PRELUDE = r"""
const fs = require('fs');
const vm = require('vm');
const elements = {
  skillsList: {innerHTML:''},
  cronFormSkillSearch: {value:'', style:{}},
  cronFormSkillDropdown: {
    children:[], style:{}, _html:'',
    set innerHTML(value){this._html=value;if(value==='')this.children=[];},
    get innerHTML(){return this._html;},
    appendChild(child){this.children.push(child);},
  },
  skillFormName: {value:'local-one'},
  skillFormContent: {value:'Skill content'},
  skillFormError: {style:{}},
};
const ctx = {
  console, setTimeout, clearTimeout, setInterval:()=>0, clearInterval,
  URL, URLSearchParams,
  S: {activeProfile:'A', session:null, messages:[]},
  localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},
  document: {
    addEventListener(){}, getElementById:id=>elements[id]||null,
    querySelector(){return null;}, querySelectorAll(){return [];},
    createElement(){return {className:'',textContent:'',style:{}};},
  },
  addEventListener(){}, location:{pathname:'/'},
  $: id => elements[id] || null,
  t: key => key, esc: value => String(value),
  rendered: [], requests: [], elements,
  showToast(){}, setStatus(){}, syncTopbar(){},
  showConfirmDialog: async () => true,
};
ctx.window=ctx;
ctx.global=ctx;
vm.createContext(ctx);
for(const file of ['sessions.js','panels.js']){
  vm.runInContext(fs.readFileSync(process.argv[1]+'/static/'+file,'utf8'),ctx,{filename:file});
}
// Keep unrelated UI/network work inert; the loaders and both switch paths are real.
vm.runInContext(`
  renderSkills = skills => rendered.push(skills.map(s=>({...s})));
  renderSessionList = async () => {};
  showSessionListSkeleton = () => {};
  closeSessionActionMenu = () => {};
  startGatewaySSE = () => {};
  applyBotName = () => {};
  _profileSwitchPanelLoad = async () => {};
  _refreshProfileSwitchBackground = () => {};
  _resetCronUnreadForProfileSwitch = () => {};
  _openProfileSwitchSessionBrowser = () => {};
  refreshProfileTransitionReasoningChip = () => {};
  openSkill = async () => {};
  _setSkillHeaderButtons = () => {};
  invalidateSlashSkillCaches = () => {};
  api = (path,opts) => {
    if(path==='/api/profile/switch') return Promise.resolve({active:JSON.parse(opts.body).name});
    if(path==='/api/skills/toggle') return Promise.resolve({ok:true});
    if(path==='/api/skills/save'||path==='/api/skills/delete') return Promise.resolve({ok:true});
    if(path!=='/api/skills') throw Error('unexpected API '+path);
    return new Promise((resolve,reject)=>requests.push({profile:S.activeProfile,resolve,reject}));
  };
`,ctx);
ctx.flush = () => new Promise(resolve=>setImmediate(resolve));
"""


def _run_node(body):
    script = NODE_PRELUDE + '\n' + (
        f'vm.runInContext({json.dumps("(async()=>{" + body + "})()")},ctx)'
        '.then(result=>console.log(JSON.stringify(result)))'
        '.catch(error=>{console.error(error);process.exitCode=1;});'
    )
    proc = subprocess.run([NODE, '-e', script, str(ROOT)], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_unavailable_skills_remain_usable_and_retry_after_recovery():
    result = _run_node("""
      const first=loadSkills();
      requests[0].resolve({runtime_scope:'unavailable',skills:[{name:'local-one',disabled:false}]});
      await first;
      const firstIncomplete=_skillsDataIncomplete;
      await toggleSkill('local-one',true);
      const afterToggle=rendered.at(-1);
      const second=loadSkills();
      requests[1].resolve({runtime_scope:'profile',skills:[
        {name:'local-one',disabled:true},{name:'external-one',disabled:false}]});
      await second;
      return {firstIncomplete,afterToggle,skillsGets:requests.length,
        finalIncomplete:_skillsDataIncomplete,finalNames:_skillsData.map(s=>s.name)};
    """)
    assert result == {
        'firstIncomplete': True, 'afterToggle': [{'name': 'local-one', 'disabled': True}],
        'skillsGets': 2, 'finalIncomplete': False, 'finalNames': ['local-one', 'external-one'],
    }


def test_cron_picker_uses_local_skills_and_retries_incomplete_result():
    result = _run_node("""
      _loadCronSkills(true);
      requests[0].resolve({runtime_scope:'unavailable',skills:[{name:'local-one',category:null}]});
      await flush();
      elements.cronFormSkillSearch.value='local';
      elements.cronFormSkillSearch.oninput();
      const firstOptions=elements.cronFormSkillDropdown.children.map(item=>item.textContent);
      const firstIncomplete=_cronSkillsCacheIncomplete;
      _loadCronSkills();
      requests[1].resolve({runtime_scope:'profile',skills:[{name:'external-one',category:'shared'}]});
      await flush();
      elements.cronFormSkillSearch.value='external';
      elements.cronFormSkillSearch.oninput();
      return {firstOptions,firstIncomplete,skillsGets:requests.length,
        finalIncomplete:_cronSkillsCacheIncomplete,
        finalOptions:elements.cronFormSkillDropdown.children.map(item=>item.textContent)};
    """)
    assert result == {
        'firstOptions': ['local-one'], 'firstIncomplete': True, 'skillsGets': 2,
        'finalIncomplete': False, 'finalOptions': ['external-one (shared)'],
    }


@pytest.mark.parametrize('loader', ['loadSkills()', '_loadCronSkills(true)'])
@pytest.mark.parametrize('switcher', ["switchToProfile('B')", "_switchProfileForSessionLoad('B')"])
@pytest.mark.parametrize('old_first', [True, False])
def test_profile_switch_discards_old_responses_in_both_orders(loader, switcher, old_first):
    result = _run_node(f"""
      {loader};
      await {switcher};
      {loader};
      if(requests.length!==2) throw Error('missing new-profile request');
      const settle = i => requests[i].resolve({{runtime_scope:'profile',skills:[
        {{name:i===0?'A-only':'B-only'}}]}});
      settle({0 if old_first else 1});
      await flush();
      const intermediate={{skills:_skillsData,cron:_cronSkillsCache,rendered:rendered.slice()}};
      settle({1 if old_first else 0});
      await flush();
      return {{intermediate,skills:_skillsData,cron:_cronSkillsCache,
        profiles:requests.map(r=>r.profile),rendered}};
    """)
    key = 'skills' if loader == 'loadSkills()' else 'cron'
    assert result['profiles'] == ['A', 'B']
    assert result['intermediate'][key] == (None if old_first else [{'name': 'B-only'}])
    assert result[key] == [{'name': 'B-only'}]
    assert all(row != [{'name': 'A-only'}] for row in result['rendered'])


@pytest.mark.parametrize('loader', ['loadSkills()', '_loadCronSkills(true)'])
@pytest.mark.parametrize('old_error', [True, False])
def test_only_newest_loader_request_can_publish(loader, old_error):
    result = _run_node(f"""
      {loader};
      {loader};
      requests[1].resolve({{runtime_scope:'profile',skills:[{{name:'newest'}}]}});
      await flush();
      const beforeError=elements.skillsList.innerHTML;
      if({str(old_error).lower()}) requests[0].reject(Error('stale failure'));
      else requests[0].resolve({{runtime_scope:'unavailable',skills:[{{name:'old'}}]}});
      await flush();
      return {{skills:_skillsData,cron:_cronSkillsCache,rendered,
        incomplete:_skillsDataIncomplete||_cronSkillsCacheIncomplete,
        beforeError,afterError:elements.skillsList.innerHTML}};
    """)
    key = 'skills' if loader == 'loadSkills()' else 'cron'
    assert result[key] == [{'name': 'newest'}]
    assert result['incomplete'] is False
    assert result['afterError'] == result['beforeError']


@pytest.mark.parametrize('switcher', ['switchToProfile', '_switchProfileForSessionLoad'])
def test_returning_to_same_profile_does_not_revive_pending_requests(switcher):
    result = _run_node(f"""
      loadSkills();
      _loadCronSkills();
      await {switcher}('B');
      await {switcher}('A');
      requests[0].resolve({{runtime_scope:'profile',skills:[{{name:'retired'}}]}});
      requests[1].resolve({{runtime_scope:'profile',skills:[{{name:'retired'}}]}});
      await flush();
      return {{profile:S.activeProfile,skills:_skillsData,cron:_cronSkillsCache,rendered}};
    """)
    assert result == {'profile': 'A', 'skills': None, 'cron': None, 'rendered': []}


def test_toggle_retires_pending_responses_and_keeps_local_working_set():
    result = _run_node("""
      _skillsDataIncomplete=true;
      _skillsData=[{name:'local-one',disabled:false}];
      loadSkills();
      _loadCronSkills();
      await toggleSkill('local-one',true);
      requests[0].resolve({runtime_scope:'profile',skills:[{name:'local-one',disabled:false}]});
      requests[1].resolve({runtime_scope:'profile',skills:[{name:'old'}]});
      await flush();
      return {skills:_skillsData,cron:_cronSkillsCache,incomplete:_skillsDataIncomplete};
    """)
    assert result == {'skills': [{'name': 'local-one', 'disabled': True}],
                      'cron': None, 'incomplete': True}


@pytest.mark.parametrize('action', ['saveSkillForm()', 'deleteCurrentSkill()'])
def test_skill_mutations_invalidate_pending_panel_and_cron_responses(action):
    result = _run_node(f"""
      loadSkills();
      _loadCronSkills();
      _currentSkillDetail={{name:'local-one'}};
      const action={action};
      await flush();
      _loadCronSkills();
      if(requests.length!==4) throw Error('mutation did not reload');
      requests[2].resolve({{runtime_scope:'profile',skills:[{{name:'saved'}}]}});
      requests[3].resolve({{runtime_scope:'profile',skills:[{{name:'saved'}}]}});
      await action;
      await flush();
      requests[0].resolve({{runtime_scope:'unavailable',skills:[{{name:'stale'}}]}});
      requests[1].resolve({{runtime_scope:'unavailable',skills:[{{name:'stale'}}]}});
      await flush();
      return {{skills:_skillsData,cron:_cronSkillsCache,rendered}};
    """)
    assert result['skills'] == result['cron'] == [{'name': 'saved'}]
    assert all(row != [{'name': 'stale'}] for row in result['rendered'])
