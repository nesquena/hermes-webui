import re
import sys
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar, overload

os.environ['TIMEOUT_ScreenshotEvent'] = '2'
os.environ['TIMEOUT_BrowserStateRequestEvent'] = '20'
os.environ['BROWSER_USE_CDP_TIMEOUT_S'] = '90'  # JS 函數內含多層級展開搜尋，可能跑較久

sys.path.insert(0, '/Users/fongyimac/hermes-webui/browser-use')

# Monkey-patch ScreenshotWatchdog 讓截圖直接回傳空字串，避免 CDP captureScreenshot 卡死
from browser_use.browser.watchdogs import screenshot_watchdog as _sw

async def on_ScreenshotEvent(self, event):
    return ''

_sw.ScreenshotWatchdog.on_ScreenshotEvent = on_ScreenshotEvent

from dotenv import load_dotenv
from pydantic import BaseModel
from ollama import Options
from browser_use import Agent, Browser, BrowserProfile, ChatOllama
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.llm.messages import BaseMessage

load_dotenv()

T = TypeVar('T', bound=BaseModel)


@dataclass
class CleanChatOllama(ChatOllama):
    """Wrapper that strips non-JSON prefixes (like '---') from Gemma responses."""

    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        if output_format is None:
            return await super().ainvoke(messages, output_format=None, **kwargs)

        from browser_use.llm.ollama.serializer import OllamaMessageSerializer
        from browser_use.llm.exceptions import ModelProviderError

        ollama_messages = OllamaMessageSerializer.serialize_messages(messages)
        try:
            schema = output_format.model_json_schema()
            response = await self.get_client().chat(
                model=self.model,
                messages=ollama_messages,
                format=schema,
                options=self.ollama_options,
            )
            completion = response.message.content or ''
            completion = re.sub(r'^[^{]*', '', completion, count=1)
            parsed = output_format.model_validate_json(completion)
            return ChatInvokeCompletion(completion=parsed, usage=None)
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(SCRIPT_DIR, 'agent_memory.md')


def init_memory():
    """初始化共享 memory 檔。"""
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        f.write(f"""# Agent 共享記憶
建立時間：{datetime.now().isoformat()}

## 進度狀態
- [ ] Phase A: 登入並進入「查詢人才」頁
- [ ] Phase B: 填寫關鍵字、希望職類、希望工作地、居住地
- [ ] Phase C: 填寫年齡、工具、證照，送出搜尋

## 各階段筆記
""")


def update_memory(phase: str, status: str, notes: str = ''):
    """更新 memory 檔。"""
    with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    if phase == 'A':
        content = content.replace('- [ ] Phase A:', f'- [x] Phase A: {status} ✓ —' if status == 'done' else '- [ ] Phase A:')
    elif phase == 'B':
        content = content.replace('- [ ] Phase B:', f'- [x] Phase B: {status} ✓ —' if status == 'done' else '- [ ] Phase B:')
    elif phase == 'C':
        content = content.replace('- [ ] Phase C:', f'- [x] Phase C: {status} ✓ —' if status == 'done' else '- [ ] Phase C:')

    if notes:
        content += f"\n### Phase {phase} ({datetime.now().strftime('%H:%M:%S')})\n{notes}\n"

    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        f.write(content)


def read_memory() -> str:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    return ''


account = os.getenv('ACCOUNT_104')
password = os.getenv('PASSWORD_104')
keyword = os.getenv('SEARCH_KEYWORD', '機電 水電')

# 驗證 .env 帳密有正確讀取
if not account or not password:
    raise RuntimeError(
        '❌ 無法從 .env 讀取 ACCOUNT_104 或 PASSWORD_104。\n'
        f'   ACCOUNT_104 = {account!r}\n'
        f'   PASSWORD_104 = {"***" if password else None!r}\n'
        f'   請確認 {SCRIPT_DIR}/.env 檔案內容正確。'
    )

print(f"📧 ACCOUNT_104  = {account}")
print(f"🔑 PASSWORD_104 = {'*' * len(password)}")
print(f"🔍 SEARCH_KEYWORD = {keyword}\n")


def load_criteria_from_md(md_path: str) -> dict:
    """從 MD 檔案中提取 ```python ... SEARCH_CRITERIA = {...} ... ``` 區塊。"""
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(
        r'```python\s*\n(SEARCH_CRITERIA\s*=\s*\{.*?\n\})\s*\n```',
        content, re.DOTALL,
    )
    if not match:
        raise ValueError(f'在 {md_path} 找不到 SEARCH_CRITERIA 區塊')
    namespace = {}
    exec(match.group(1), namespace)  # noqa: S102 — 信任本地 MD 檔
    return namespace['SEARCH_CRITERIA']


CRITERIA_FILE = os.path.join(SCRIPT_DIR, 'criteria_jidian_deputy_tainan.md')
SEARCH_CRITERIA = load_criteria_from_md(CRITERIA_FILE)
print(f"📋 已載入查詢條件: {os.path.basename(CRITERIA_FILE)}")
for k, v in SEARCH_CRITERIA.items():
    print(f"   {k}: {v}")
print()

gemma_options = Options(num_ctx=32768, num_predict=4096)
# gemma4:26b 能正確使用 task 中的具體值（帳密、關鍵字等）
# num_ctx=32768：查詢人才頁 DOM 很大，需要大 context；num_predict=4096 確保 JSON 不被截斷
llm = CleanChatOllama(model='gemma4:26b', ollama_options=gemma_options)

browser = Browser(
    browser_profile=BrowserProfile(
        executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        user_data_dir=os.path.expanduser('~/Library/Application Support/Google/Chrome'),
        profile_directory='Profile 4',  # 小豐 / fongchien19@gmail.com，已記住 104 帳密
        headless=False,
        keep_alive=True,
        enable_default_extensions=False,
        disable_security=True,
        wait_for_network_idle_page_load_time=5,
        minimum_wait_page_load_time=2,
        args=[
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-features=PaintHolding,LazyImageLoading,CalculateNativeWinOcclusion',
        ],
    )
)


def make_agent(task: str) -> Agent:
    return Agent(
        task=task,
        llm=llm,
        browser=browser,
        max_failures=5,
        max_steps=15,
        step_timeout=300,
        llm_timeout=300,
        use_vision=False,
        use_thinking=False,
    )


async def warmup_model():
    """預熱 LLM，讓 ollama 把模型載入記憶體，避免首次推論超時。
    用比較長的 prompt 避免 ollama cache 進不正常的短回應狀態。"""
    print("🔥 Warming up model (loading into memory)...")
    try:
        await llm.get_client().chat(
            model=llm.model,
            messages=[{'role': 'user', 'content': 'Reply with a JSON object: {"status": "ready"}'}],
            options=llm.ollama_options,
        )
        print("✅ Model loaded.\n")
    except Exception as e:
        print(f"⚠️ Warmup failed: {e}\n")


# ========== Phase A: 登入（Python 預先 navigate 後讓 agent 處理表單）==========
# Chrome Profile 4 已記住 104 帳密，登入頁開啟時 Chrome 會自動填入
# ⚠️ 任務中絕對不要寫完整 URL，避免 browser-use 自動把它當成初始 navigate
TASK_A = f"""
You are on 104 login page already. Do NOT navigate to any URL. Just interact with the current page.

VERIFY current DOM before EACH action:
- If you see "招募管理" dashboard text and current URL has "/rms/" path → login complete → call done(success=True) immediately. Do NOT do anything else.
- Otherwise follow steps below based on visible DOM.

Decision tree (check current DOM each step):

1. If you see email input (data-qa-id="loginUserName"):
   - If empty, type: {account}
   - If already filled, skip to step 2

2. If you see password input (data-qa-id="loginPassword"):
   - If empty, type: {password}
   - If already filled, skip to step 3

3. If you see button (data-qa-id="loginButton") with text "立即登入":
   - Click it, wait 3 seconds, re-check page state

4. If you see "請選擇要登入的公司" text on page:
   - Click "確定登入" button with data-company-number="89369020000", wait 3 seconds

5. If "暫不更新" button is visible in CURRENT DOM right now:
   - Click it
   - Otherwise SKIP this step

6. Once login is complete (招募管理 visible), call done(success=True).

⚠️ Do NOT navigate to URLs. Only click buttons and type text.
⚠️ Verify each element is visible in current DOM before clicking.
"""


# ========== Phase B: 填寫表單前段（科系 modal）==========
TASK_B = """
The form has keyword, job categories, locations, age, and 7天內 already filled.

Now do this:

Step 1: Find DIV with id="majorId" (科系 selector). Click it.
Step 2: Wait 2 seconds for modal.
Step 3: In the modal, find checkboxes labeled "電機電子工程相關" and "冷凍空調相關". Check both.
Step 4: Click button with class="category-picker-btn-primary" (text "確定").

When done, call done(success=True).
"""


# ========== Phase C: 填寫表單後段 + 送出 ==========
TASK_C = """
The form has keyword, job categories, location, and majors filled. Continue:

Step 1 - 總年資: Find the 總年資 dropdown (workExpTimeMin), select 3, then change "以下" dropdown to "以上".

Step 2 - 擅長工具:
   Click DIV with id="goodTools" (NOT empty anchor). In modal, select AutoCAD. Click 確定.

Step 3 - 證照:
   Click DIV with id="certificates". In modal, search "室內配線技術士" and select any matching option. Click 確定.

Step 4 - 送出:
   Find button with data-gtm-listsearch="一般查詢 - 符合人數按鈕" (text starts with "符合人數").
   Click that button.

When results appear (URL or page changes), call done(success=True).
"""


async def run_phase_with_retry(name: str, task: str, max_retries: int = 2) -> bool:
    """跑一個 Phase，失敗自動重試。"""
    for attempt in range(1, max_retries + 2):
        print(f"\n🔄 Attempt {attempt}/{max_retries + 1}")
        try:
            agent = make_agent(task)
            history = await agent.run()
            success = history.is_successful() if hasattr(history, 'is_successful') else True
            if success:
                print(f"✅ {name} succeeded on attempt {attempt}")
                return True
            print(f"⚠️ {name} attempt {attempt} did not report success.")
        except Exception as e:
            print(f"⚠️ {name} attempt {attempt} raised: {type(e).__name__}: {e}")

        if attempt < max_retries + 1:
            print(f"⏳ Waiting 5s before retry...")
            await asyncio.sleep(5)

    print(f"❌ {name} failed after {max_retries + 1} attempts.")
    return False


_browser_started = False


async def get_current_url() -> str:
    """取得目前瀏覽器顯示的 URL。"""
    info = await browser.get_current_target_info()
    return info.get('url', '') if info else ''


async def py_run_js(script: str):
    """在當前頁面執行 JavaScript 並回傳結果。
    awaitPromise=True 讓 CDP 等待 async function 的 Promise 解析。"""
    cdp_session = await browser.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': script, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if 'exceptionDetails' in result:
        ex = result['exceptionDetails']
        return {'js_error': ex.get('text', 'unknown'), 'exception': ex.get('exception', {}).get('description')}
    return result.get('result', {}).get('value')


async def py_fill_keyword(keyword_text: str):
    """直接用 JS 填入關鍵字欄位。"""
    import json as _json
    kw = _json.dumps(keyword_text, ensure_ascii=False)
    script = f"""
    (function() {{
        const input = document.querySelector('input[name="keyword"]');
        if (!input) return {{ ok: false, reason: 'not found' }};
        input.value = {kw};
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return {{ ok: true, value: input.value }};
    }})();
    """
    return await py_run_js(script)


async def py_click_selector(title: str):
    """點開以 title 識別的下拉框（會打開 modal）。"""
    import json as _json
    title_js = _json.dumps(title, ensure_ascii=False)
    script = f"""
    (function() {{
        const t = {title_js};
        const sel1 = `[title="${{t}}"] .form-tag-selector`;
        const sel2 = `[title="${{t}}"]`;
        const div = document.querySelector(sel1) || document.querySelector(sel2);
        if (!div) return {{ ok: false, reason: 'not found', selectors: [sel1, sel2] }};
        div.click();
        return {{ ok: true, tag: div.tagName }};
    }})();
    """
    return await py_run_js(script)


async def py_expand_more_conditions():
    """點擊「更多查詢條件」展開隱藏的搜尋欄位（科系、年齡、工具、證照等）。"""
    script = """
    (function() {
        // 找到 gtm-data-listsearch="更多查詢條件" 的連結
        const link = document.querySelector('a[gtm-data-listsearch="更多查詢條件"]');
        if (!link) return { ok: false, reason: 'link not found' };
        link.click();
        return { ok: true };
    })();
    """
    return await py_run_js(script)


async def py_click_radio_by_text(group_name: str, label_text: str):
    """根據 radio 群組名稱和標籤文字勾選 radio。"""
    import json as _json
    g = _json.dumps(group_name, ensure_ascii=False)
    t = _json.dumps(label_text, ensure_ascii=False)
    script = f"""
    (function() {{
        const groupName = {g};
        const labelText = {t};
        const radios = document.querySelectorAll(`input[type="radio"][name="${{groupName}}"]`);
        for (const r of radios) {{
            const lbl = r.closest('label');
            if (lbl && lbl.textContent.trim().includes(labelText)) {{
                r.click();
                return {{ ok: true, value: r.value }};
            }}
        }}
        return {{ ok: false, reason: 'no matching radio', groupName, labelText, count: radios.length }};
    }})();
    """
    return await py_run_js(script)


async def py_set_input_by_name(name: str, value: str):
    """直接用 JS 設定 input value。"""
    import json as _json
    n = _json.dumps(name, ensure_ascii=False)
    v = _json.dumps(value, ensure_ascii=False)
    script = f"""
    (function() {{
        const inputName = {n};
        const inputValue = {v};
        const el = document.querySelector(`input[name="${{inputName}}"]`);
        if (!el) return {{ ok: false, reason: 'not found', inputName }};
        el.value = inputValue;
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return {{ ok: true, inputName, inputValue }};
    }})();
    """
    return await py_run_js(script)


async def py_fill_login_and_submit(account: str, password: str):
    """填帳密並點立即登入。"""
    script = f"""
    (function() {{
        const emailInput = document.querySelector('input[data-qa-id="loginUserName"]') ||
                           document.querySelector('input[name="email"]');
        const passwordInput = document.querySelector('input[data-qa-id="loginPassword"]') ||
                              document.querySelector('input[name="password"]');
        const loginBtn = document.querySelector('button[data-qa-id="loginButton"]');

        if (!emailInput || !passwordInput || !loginBtn) {{
            return {{ ok: false, reason: 'missing fields',
                     hasEmail: !!emailInput, hasPassword: !!passwordInput, hasButton: !!loginBtn }};
        }}

        if (!emailInput.value) {{
            emailInput.value = {account!r};
            emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            emailInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
        if (!passwordInput.value) {{
            passwordInput.value = {password!r};
            passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}

        loginBtn.click();
        return {{ ok: true, clicked: true }};
    }})();
    """
    return await py_run_js(script)


async def py_click_company_confirm(company_number: str = '89369020000'):
    """選擇公司：點擊指定公司的「確定登入」。"""
    script = f"""
    (function() {{
        const btn = document.querySelector('button[data-company-number="{company_number}"]');
        if (btn) {{
            btn.click();
            return {{ ok: true }};
        }}
        return {{ ok: false, reason: 'company button not found' }};
    }})();
    """
    return await py_run_js(script)


async def py_dismiss_network_error():
    """偵測並關閉「網路連線發生錯誤，請檢查網路連線狀態」彈窗。
    104 用 SweetAlert2 / 自訂 modal，按鈕文字是「確定」。"""
    script = """
    (function() {
        // SweetAlert2 confirm button
        var btn = document.querySelector('.swal2-confirm');
        if (btn && document.body.textContent.includes('網路連線')) {
            btn.click();
            return { ok: true, method: 'swal2-confirm' };
        }
        // 通用：找含「網路連線」的 modal/dialog，點「確定」
        var modals = document.querySelectorAll('.modal, .dialog, [role="dialog"], .popup, .swal2-popup, .ant-modal');
        for (var m of modals) {
            if (m.textContent.includes('網路連線') || m.textContent.includes('連線發生錯誤')) {
                var buttons = m.querySelectorAll('button, .btn, a.btn');
                for (var b of buttons) {
                    if (b.textContent.trim() === '確定' || b.textContent.trim() === 'OK') {
                        b.click();
                        return { ok: true, method: 'modal-button' };
                    }
                }
            }
        }
        // fallback：全頁面找含「確定」的按鈕且頁面有網路連線錯誤訊息
        var pageText = document.body.textContent || '';
        if (pageText.includes('網路連線') || pageText.includes('連線發生錯誤')) {
            var allBtns = document.querySelectorAll('button');
            for (var b of allBtns) {
                var t = b.textContent.trim();
                if (t === '確定' || t === 'OK' || t === '確認') {
                    b.click();
                    return { ok: true, method: 'fallback-button' };
                }
            }
        }
        return { ok: false, reason: 'no network error dialog' };
    })();
    """
    return await py_run_js(script)


async def py_auto_dismiss_errors():
    """自動處理常見彈窗：網路錯誤、暫不更新。"""
    net = await py_dismiss_network_error()
    if net and net.get('ok'):
        print(f"   ⚡ 已自動關閉網路錯誤彈窗 ({net.get('method')})")
        await asyncio.sleep(2)
        return True
    upd = await py_dismiss_update_dialog()
    if upd and upd.get('ok'):
        print(f"   ⚡ 已自動關閉「暫不更新」彈窗")
        await asyncio.sleep(1)
        return True
    return False


async def py_dismiss_update_dialog():
    """如有「暫不更新」按鈕就點擊。"""
    script = """
    (function() {
        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {
            const text = btn.textContent.trim();
            if (text === '暫不更新' || text.includes('暫不更新')) {
                btn.click();
                return { ok: true, clicked: true };
            }
        }
        return { ok: false, reason: 'no update dialog visible' };
    })();
    """
    return await py_run_js(script)


async def py_handle_login_timeout(password: str):
    """處理「登入逾時」頁：填密碼並點「繼續使用」。"""
    import json as _json
    pwd_js = _json.dumps(password, ensure_ascii=False)
    script = f"""
    (function() {{
        // 檢查是否在「登入逾時」頁面
        const pageText = document.body.textContent || '';
        const isTimeoutPage = pageText.includes('登入逾時') || pageText.includes('您已逾時操作');
        if (!isTimeoutPage) {{
            return {{ ok: false, reason: 'not on timeout page' }};
        }}

        // 填密碼
        const pwdInput = document.querySelector('input[data-qa-id="loginPassword"]') ||
                         document.querySelector('input[name="password"]');
        if (!pwdInput) {{
            return {{ ok: false, reason: 'password input not found' }};
        }}
        pwdInput.value = {pwd_js};
        pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
        pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

        // 點「繼續使用」
        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {{
            const text = btn.textContent.trim();
            if (text === '繼續使用' || text.includes('繼續使用')) {{
                btn.click();
                return {{ ok: true, clicked: '繼續使用' }};
            }}
        }}
        return {{ ok: false, reason: 'no 繼續使用 button' }};
    }})();
    """
    return await py_run_js(script)


async def py_set_workexp_dropdown(years: int, range_type: str = '以上'):
    """設定總年資 dropdown：年數 + 範圍（以上/以下/至）。"""
    import json as _json
    yrs = _json.dumps(str(years))
    rt = _json.dumps(range_type, ensure_ascii=False)
    script = f"""
    (async function() {{
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const yearsVal = {yrs};
        const rangeText = {rt};

        // 1. 設定 workExpTimeMin (隱藏 input)
        const minInput = document.querySelector('input[name="workExpTimeMin"]');
        if (minInput) {{
            minInput.value = yearsVal;
            minInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}

        // 2. 找年資 dropdown 的 toggle button 並打開
        const minWrappers = document.querySelectorAll('.dropdown-wrapper');
        let yearOk = false, rangeOk = false;
        for (const wrap of minWrappers) {{
            const hidden = wrap.querySelector('input[name="workExpTimeMin"]');
            if (hidden) {{
                const toggle = wrap.querySelector('button.dropdown-toggle');
                if (toggle) {{
                    toggle.click();
                    await sleep(300);
                    const opt = wrap.querySelector(`a.dropdown-item[value="${{yearsVal}}"]`);
                    if (opt) {{ opt.click(); yearOk = true; }}
                }}
                break;
            }}
        }}
        await sleep(300);

        // 3. 設定 workExpRangeType (以上/以下/至)
        const rangeMap = {{ '以下': 'down', '以上': 'up', '至': 'to' }};
        const rangeVal = rangeMap[rangeText] || 'up';
        for (const wrap of minWrappers) {{
            const hidden = wrap.querySelector('input[name="workExpRangeType"]');
            if (hidden) {{
                const toggle = wrap.querySelector('button.dropdown-toggle');
                if (toggle) {{
                    toggle.click();
                    await sleep(300);
                    const opt = wrap.querySelector(`a.dropdown-item[value="${{rangeVal}}"]`);
                    if (opt) {{ opt.click(); rangeOk = true; }}
                }}
                break;
            }}
        }}

        return {{ ok: yearOk && rangeOk, yearOk, rangeOk, yearsVal, rangeText }};
    }})();
    """
    return await py_run_js(script)


def parse_duration_months(text: str) -> int:
    """從 '(1年6個月)' 或 '(7個月)' 解析月份數。"""
    import re as _re
    m = _re.search(r'(\d+)年(?:(\d+)個月)?', text)
    if m:
        years = int(m.group(1))
        months = int(m.group(2) or 0)
        return years * 12 + months
    m = _re.search(r'(\d+)個月', text)
    if m:
        return int(m.group(1))
    if '仍在職' in text:
        # 估計仍在職就抓開始年份算到現在
        m = _re.search(r'(\d{4})/(\d{1,2})', text)
        if m:
            from datetime import datetime
            start = datetime(int(m.group(1)), int(m.group(2)), 1)
            now = datetime.now()
            return (now.year - start.year) * 12 + (now.month - start.month)
    return 0


def score_candidate(c: dict) -> dict:
    """規則型評分。回傳 {score, level, reasons, matches}。"""
    reasons = []
    matches = {
        '建設營造公司': 0,
        '機電水電職稱': 0,
        '長期任職': False,
        '建築工程年資': 0,
    }
    score = 0
    max_score = 100

    # 1. 工作經歷分析（最大 40 分）
    construction_keywords = ['營造', '建設']
    title_keywords = ['機電', '水電', '監工', '主任', '空調', '機水電']

    construction_company_count = 0
    relevant_title_count = 0
    has_long_term = False
    total_construction_months = 0

    for exp in c.get('experiences', []):
        is_construction = any(kw in exp for kw in construction_keywords)
        is_relevant_title = any(kw in exp for kw in title_keywords)

        if is_construction:
            construction_company_count += 1
        if is_relevant_title:
            relevant_title_count += 1

        months = parse_duration_months(exp)
        if months >= 36:  # 3 年以上
            has_long_term = True
        if is_construction or is_relevant_title:
            total_construction_months += months

    matches['建設營造公司'] = construction_company_count
    matches['機電水電職稱'] = relevant_title_count
    matches['長期任職'] = has_long_term
    matches['建築工程年資'] = total_construction_months // 12

    # 計分
    if construction_company_count >= 2:
        score += 25
        reasons.append(f"任職過 {construction_company_count} 間建設/營造公司 (+25)")
    elif construction_company_count == 1:
        score += 15
        reasons.append("任職過 1 間建設/營造公司 (+15)")

    if relevant_title_count >= 3:
        score += 20
        reasons.append(f"{relevant_title_count} 個機電/水電/主任相關職位 (+20)")
    elif relevant_title_count >= 1:
        score += 10
        reasons.append(f"{relevant_title_count} 個機電/水電相關職位 (+10)")

    if has_long_term:
        score += 15
        reasons.append("有單一工作 ≥ 3 年 (+15)")

    # 2. 等級判定
    construction_years = total_construction_months // 12
    if construction_years >= 5 or construction_company_count >= 2:
        level = '主任級'
        score += 30
        reasons.append(f"主任級資格（建築工程 {construction_years} 年）(+30)")
    elif construction_years >= 3 or construction_company_count >= 1:
        level = '副主任級'
        score += 20
        reasons.append(f"副主任級資格（建築工程 {construction_years} 年）(+20)")
    else:
        level = '不符等級'
        reasons.append("年資/經驗不足以判定等級")

    # 3. 居住地加分
    residence = c.get('residence', '')
    if '台南' in residence:
        score += 10
        reasons.append("居住台南 (+10)")

    return {
        'score': min(score, max_score),
        'level': level,
        'matches': matches,
        'reasons': reasons,
    }


async def py_extract_candidate_list():
    """從搜尋結果頁擷取所有候選人卡片的結構化資訊。"""
    script = r"""
    (function() {
        const cards = document.querySelectorAll('[data-qa-id="resumeCard"]');
        const candidates = [];

        for (const card of cards) {
            const resumeId = card.id || '';
            const detailLink = card.querySelector('a.name, a[href*="SearchResumeMaster"]');
            const detailHref = detailLink ? detailLink.href : '';
            const name = (card.querySelector('a.name') || {}).textContent?.trim() || '';
            const age = (card.querySelector('.year') || {}).textContent?.trim() || '';
            const gender = (card.querySelector('.gender') || {}).textContent?.trim() || '';

            // support-info 含 代碼/更新日
            const supportInfo = (card.querySelector('.support-info') || {}).textContent?.trim() || '';

            const getInfoByQa = (qaId) => {
                const el = card.querySelector(`[data-qa-id="${qaId}"]`);
                return el ? el.textContent.trim().replace(/\s+/g, ' ') : '';
            };

            const preferArea = getInfoByQa('cardPreferArea');
            const residence = getInfoByQa('cardResidence');
            const education = getInfoByQa('cardEducation');
            const preferJobTitle = getInfoByQa('cardPreferJobTitle');
            const workExpTotal = getInfoByQa('cardWorkExperience');

            // 工作經歷列表 (.content-list li)
            const expLines = [];
            card.querySelectorAll('.content-list li').forEach(li => {
                expLines.push(li.textContent.trim().replace(/\s+/g, ' '));
            });

            candidates.push({
                resumeId,
                detailHref,
                name,
                age,
                gender,
                supportInfo,
                preferArea,
                residence,
                education,
                preferJobTitle,
                workExpTotal,
                experiences: expLines,
            });
        }

        return {
            ok: true,
            count: candidates.length,
            url: location.href,
            title: document.title,
            candidates,
        };
    })();
    """
    return await py_run_js(script)


async def py_click_submit_search():
    """點擊「符合人數 XXX 人」送出搜尋。"""
    script = """
    (function() {
        // HTML 屬性是 gtm-data-listsearch（沒 data- 前綴）
        let btn = document.querySelector('button[gtm-data-listsearch="一般查詢 - 符合人數按鈕"]');
        if (!btn) {
            // 備援：找文字含「符合人數」的 btn-primary
            const allBtns = document.querySelectorAll('button.btn-primary');
            for (const b of allBtns) {
                if (b.textContent.includes('符合人數')) {
                    btn = b;
                    break;
                }
            }
        }
        if (!btn) return { ok: false, reason: 'submit button not found' };
        btn.click();
        return { ok: true, text: btn.textContent.trim() };
    })();
    """
    return await py_run_js(script)


async def py_click_product_rms():
    """在 bsignin.104.com.tw/product 頁面點擊「招募管理」產品。"""
    script = """
    (function() {
        // 找含 "招募管理" 文字的 link 或 button
        const candidates = document.querySelectorAll('a, button, [role="button"]');
        for (const el of candidates) {
            const text = el.textContent.trim();
            if (text === '招募管理' || (text.includes('招募管理') && text.length < 30)) {
                el.click();
                return { ok: true, clicked: text };
            }
        }
        return { ok: false, reason: 'no 招募管理 button found' };
    })();
    """
    return await py_run_js(script)


async def py_select_modal_options(targets: list[str], confirm: bool = True):
    """在已開啟的 category-picker modal 中勾選選項。
    支援多層樹狀結構：若選項不在當前可見範圍，會自動展開父節點搜尋。"""
    import json as _json
    targets_json = _json.dumps(targets, ensure_ascii=False)
    script = f"""
    (async function() {{
        const targets = {targets_json};
        const matched = [];
        const notFound = [];
        const sleep = ms => new Promise(r => setTimeout(r, ms));

        function findCheckbox(target) {{
            // 先試 value
            const byValue = document.querySelector(
                `.category-picker-checkbox input[type="checkbox"][value="${{target}}"]`
            );
            if (byValue) return {{ cb: byValue, by: 'value' }};
            // 再試 children 文字
            const labels = document.querySelectorAll('.category-picker-checkbox');
            for (const label of labels) {{
                const span = label.querySelector('span.children');
                if (!span) continue;
                if (span.textContent.trim() === target) {{
                    return {{ cb: label.querySelector('input[type="checkbox"]'), by: 'text' }};
                }}
            }}
            return null;
        }}

        function clickCb(cb) {{
            if (cb && !cb.checked) cb.click();
        }}

        async function expandLevelOne(item) {{
            const arrowBtn = item.querySelector('button.arrow') || item;
            arrowBtn.click();
            await sleep(300);
        }}

        async function tryFind(target) {{
            // 1. 直接搜尋當前可見元素
            let result = findCheckbox(target);
            if (result) {{ clickCb(result.cb); return result.by; }}

            // 2. 一級節點逐個展開搜尋
            const levelOnes = document.querySelectorAll('.category-item--level-one');
            for (const one of levelOnes) {{
                await expandLevelOne(one);
                result = findCheckbox(target);
                if (result) {{ clickCb(result.cb); return result.by + '+L1'; }}
            }}

            // 3. 全部一級展開後，再展開所有二級搜尋
            const levelTwos = document.querySelectorAll('.category-item--level-two');
            for (const two of levelTwos) {{
                const arrow2 = two.querySelector('button.arrow.arrow--down');
                if (arrow2) {{
                    arrow2.click();
                    await sleep(150);
                    result = findCheckbox(target);
                    if (result) {{ clickCb(result.cb); return result.by + '+L2'; }}
                }}
            }}
            return null;
        }}

        for (const target of targets) {{
            const by = await tryFind(target);
            if (by) matched.push({{ target, by }});
            else notFound.push(target);
        }}

        await sleep(300);
        if ({str(confirm).lower()}) {{
            const confirmBtn = document.querySelector('.category-picker-btn-primary');
            if (confirmBtn) {{
                confirmBtn.click();
                return {{ ok: true, matched, notFound, confirmed: true }};
            }}
        }}
        return {{ ok: matched.length > 0, matched, notFound, confirmed: false }};
    }})();
    """
    return await py_run_js(script)


async def py_navigate_and_verify(url: str, retries: int = 3, wait_after: float = 5,
                                   url_must_contain: str | None = None):
    """Python 端直接 navigate 並驗證成功。
    每 0.5 秒輪詢 URL，一旦符合就立刻返回，不必等滿 wait_after。"""
    global _browser_started
    if not _browser_started:
        await browser.start()
        _browser_started = True

    expected = url_must_contain or url.split('://', 1)[-1].split('/', 1)[0]

    for attempt in range(1, retries + 1):
        print(f"  🌐 Navigate to {url} (attempt {attempt}/{retries})")
        try:
            try:
                await browser.navigate_to(url)
            except Exception as nav_e:
                print(f"     navigate_to raised: {type(nav_e).__name__}; 仍嘗試 poll URL")

            # Poll URL every 0.5s, return as soon as it matches
            poll_total = 0.0
            poll_step = 0.5
            while poll_total < wait_after:
                await asyncio.sleep(poll_step)
                poll_total += poll_step
                try:
                    current = await get_current_url()
                except Exception:
                    current = ''
                if expected in current:
                    print(f"  ✅ Verified ({poll_total:.1f}s): {current}")
                    return True
                print(f"     [{poll_total:.1f}s] URL: {current[:80] if current else '(none)'}")

            print(f"  ⚠️ URL still not matching after {wait_after}s")
        except Exception as e:
            print(f"  ⚠️ Navigation error: {type(e).__name__}: {e}")
        if attempt < retries:
            await asyncio.sleep(2)
    return False


async def setup_dialog_auto_accept():
    """啟用 CDP Page domain 並自動接受所有原生 JS dialog（alert/confirm/prompt）。
    104 的「網路連線發生錯誤，請檢查網路連線狀態」就是 JS alert()。"""
    try:
        cdp_session = await browser.get_or_create_cdp_session()
        # 啟用 Page events（包括 javascriptDialogOpening）
        await cdp_session.cdp_client.send.Page.enable(
            params={}, session_id=cdp_session.session_id
        )

        # 註冊 dialog 事件處理器
        async def on_dialog(event):
            msg = event.get('message', '')
            dtype = event.get('type', 'alert')
            print(f"\n   ⚡ 偵測到原生 {dtype} 對話框：{msg[:60]}")
            try:
                await cdp_session.cdp_client.send.Page.handleJavaScriptDialog(
                    params={'accept': True}, session_id=cdp_session.session_id
                )
                print(f"   ⚡ 已自動點擊「確定」關閉對話框")
            except Exception as e:
                print(f"   ⚠️ 關閉對話框失敗：{e}")

        cdp_session.cdp_client.on('Page.javascriptDialogOpening', on_dialog)
        print("✅ 已啟用原生 JS dialog 自動接受（網路錯誤彈窗將自動關閉）")
    except Exception as e:
        print(f"⚠️ 設定 dialog 自動接受失敗：{e}（將依賴手動處理）")


async def main():
    init_memory()
    print(f"\n📝 Memory file initialized: {MEMORY_FILE}\n")

    await warmup_model()

    # ===== 第一步：直接 navigate 到 104 登入頁 =====
    print("="*60)
    print("🌐 開啟瀏覽器並連線到 104 登入頁")
    print("="*60)

    nav_ok = await py_navigate_and_verify(
        'https://bsignin.104.com.tw/login',
        url_must_contain='104.com.tw',
        wait_after=5,
    )
    if not nav_ok:
        print("❌ 無法連到 104 網站。停止。\n")
        return

    # 啟用原生 JS dialog 自動接受（必須在瀏覽器連線後）
    await setup_dialog_auto_accept()

    current_url = await get_current_url()

    # 判斷登入狀態：
    # - 已在 vip.104.com.tw/rms → 完全登入，跳過 Phase A
    # - 在 bsignin.104.com.tw/product → 有 session 但未選產品，跳到 product 處理
    # - 在 bsignin.104.com.tw/login → 未登入，要填帳密
    already_logged_in = 'vip.104.com.tw/rms' in current_url
    on_product_page = '/product' in current_url
    on_timeout_page = False  # 等下檢查
    needs_full_login = 'bsignin.104.com.tw/login' in current_url

    if already_logged_in:
        print(f"\n✅ 已登入 (URL: {current_url})，跳過 Phase A")
        update_memory('A', 'done', '已登入，跳過登入流程')
    elif on_product_page:
        print(f"\n✅ 有登入 session，停在產品選擇頁")
        print("\n" + "="*60)
        print("🚀 Phase A: 從產品選擇頁進入後台")
        print("="*60)

        # 直接點擊「招募管理」（如果頁面有的話）
        print("\n📝 [Phase A] 嘗試點擊「招募管理」按鈕")
        rms_click = await py_click_product_rms()
        print(f"   結果: {rms_click}")

        # navigate 到 rms 確保進入後台
        if not rms_click.get('ok'):
            print("\n📝 [Phase A] 直接 navigate 到 vip.104.com.tw/rms/index")
            try:
                await browser.navigate_to('https://vip.104.com.tw/rms/index')
            except Exception as e:
                print(f"   navigate 錯誤（可能已生效）: {e}")
        await asyncio.sleep(5)

        # ⭐ 處理「登入逾時」頁（點完招募管理後可能跳出）
        print("\n📝 [Phase A] 檢查是否出現「登入逾時」頁")
        timeout_result = await py_handle_login_timeout(password)
        print(f"   結果: {timeout_result}")
        if timeout_result.get('ok'):
            print("   等待重新登入跳轉...")
            for i in range(10):
                await asyncio.sleep(1)
                url = await get_current_url()
                if 'vip.104.com.tw' in url:
                    print(f"   [{i+1}s] 跳轉到: {url}")
                    break
                print(f"   [{i+1}s] 仍在: {url}")

        # 處理可能出現的選公司、暫不更新
        print("\n📝 [Phase A] 處理公司選擇（如果出現）")
        company_result = await py_click_company_confirm('89369020000')
        print(f"   結果: {company_result}")
        await asyncio.sleep(3)

        print("\n📝 [Phase A] 處理「暫不更新」彈窗（如果出現）")
        dismiss_result = await py_dismiss_update_dialog()
        print(f"   結果: {dismiss_result}")
        await asyncio.sleep(2)

        final_url = await get_current_url()
        if 'vip.104.com.tw' in final_url:
            print(f"\n✅ Phase A 完成 (URL: {final_url})")
            update_memory('A', 'done', '從 product 頁進入後台')
        else:
            print(f"\n❌ Phase A 失敗，最終 URL: {final_url}")
            update_memory('A', 'failed', f'從 product 進入失敗')
            return
    else:
        print(f"\n⚠️ 未登入 (URL: {current_url})，執行完整 Phase A")
        print("\n" + "="*60)
        print("🚀 Phase A: Login (Python+JS, no LLM)")
        print("="*60)

        # 步驟 1：填帳密並送出
        print(f"\n📝 [Phase A] 填帳密並點擊立即登入")
        login_result = await py_fill_login_and_submit(account, password)
        print(f"   結果: {login_result}")
        if not login_result.get('ok'):
            # 可能是登入逾時頁，檢查並處理
            timeout_check = await py_handle_login_timeout(password)
            print(f"   檢查登入逾時: {timeout_check}")
            if not timeout_check.get('ok'):
                print("❌ 登入失敗：找不到必要欄位。停止。\n")
                return

        # 步驟 2：等待跳轉到 product 頁或 vip 頁
        print("\n📝 [Phase A] 等待登入跳轉...")
        for i in range(10):
            await asyncio.sleep(1)
            url = await get_current_url()
            if 'vip.104.com.tw' in url or '/product' in url:
                print(f"   [{i+1}s] 跳轉到: {url}")
                break
            print(f"   [{i+1}s] 仍在: {url}")

        # 步驟 2.5：處理「登入逾時」頁面（如果出現）
        await asyncio.sleep(1)
        print("\n📝 [Phase A] 檢查是否有「登入逾時」頁面")
        timeout_result = await py_handle_login_timeout(password)
        print(f"   結果: {timeout_result}")
        if timeout_result.get('ok'):
            print("   等待重新登入跳轉...")
            for i in range(10):
                await asyncio.sleep(1)
                url = await get_current_url()
                if '/product' in url or 'vip.104.com.tw' in url:
                    print(f"   [{i+1}s] 跳轉到: {url}")
                    break

        # 步驟 3：若停在 product 頁，點擊「招募管理」
        await asyncio.sleep(2)
        url = await get_current_url()
        if '/product' in url:
            print(f"\n📝 [Phase A] 在產品選擇頁，點擊「招募管理」")
            rms_result = await py_click_product_rms()
            print(f"   結果: {rms_result}")
            # 再次直接 navigate 到 vip 確保進入後台
            if not rms_result.get('ok'):
                print("   點擊失敗，直接 navigate 到 vip.104.com.tw/rms/index")
                await browser.navigate_to('https://vip.104.com.tw/rms/index')
            await asyncio.sleep(5)

        # 步驟 4：若出現公司選擇頁，點擊確定登入
        print("\n📝 [Phase A] 處理公司選擇（如果出現）")
        company_result = await py_click_company_confirm('89369020000')
        print(f"   結果: {company_result}")
        await asyncio.sleep(3)

        # 步驟 5：若出現「暫不更新」彈窗，點擊
        print("\n📝 [Phase A] 處理「暫不更新」彈窗（如果出現）")
        dismiss_result = await py_dismiss_update_dialog()
        print(f"   結果: {dismiss_result}")
        await asyncio.sleep(2)

        # 步驟 6：驗證已登入
        final_url = await get_current_url()
        if 'vip.104.com.tw/rms' in final_url or 'vip.104.com.tw' in final_url:
            print(f"\n✅ Phase A 完成 (URL: {final_url})")
            update_memory('A', 'done', '純 Python+JS 登入完成')
        else:
            print(f"\n❌ Phase A 失敗，最終 URL: {final_url}")
            update_memory('A', 'failed', f'登入失敗，URL: {final_url}')
            return

    print("\n" + "="*60)
    print("🚀 Phase B: 進入查詢人才 + 填寫前段表單")
    print("="*60)
    print("📍 Pre-navigating to search page (Python-side)...")
    nav_ok = await py_navigate_and_verify(
        'https://vip.104.com.tw/search/listSearch',
        url_must_contain='search/listSearch',
    )
    if not nav_ok:
        print("❌ Cannot reach search page. Stopping.\n")
        return

    await py_auto_dismiss_errors()

    kw = SEARCH_CRITERIA.get('keyword', keyword)
    print(f"\n📝 [Python-JS] 填入關鍵字 '{kw}'")
    print(f"   結果: {await py_fill_keyword(kw)}")
    await asyncio.sleep(1)

    last_action = SEARCH_CRITERIA.get('last_action_days', '7天內')
    print(f"\n📝 [Python-JS] 選擇 最近活動日 = {last_action}")
    print(f"   結果: {await py_click_radio_by_text('lastActionDateType', last_action)}")
    await asyncio.sleep(0.5)

    # 開 希望職類 modal → 勾選 → 確定
    job_cats = SEARCH_CRITERIA.get('job_categories', [])
    print("\n📝 [Python-JS] 希望職類: 開 modal")
    print(f"   結果: {await py_click_selector('希望職務選單')}")
    await asyncio.sleep(2)
    print(f"   勾選 {job_cats} + 確定")
    print(f"   結果: {await py_select_modal_options(job_cats)}")
    await asyncio.sleep(2)
    await py_auto_dismiss_errors()

    # 開 希望工作地 modal → 選城市 → 確定
    work_locs = SEARCH_CRITERIA.get('work_locations', [])
    print("\n📝 [Python-JS] 希望工作地: 開 modal")
    print(f"   結果: {await py_click_selector('希望工作地選單')}")
    await asyncio.sleep(2)
    print(f"   選 {work_locs} + 確定")
    print(f"   結果: {await py_select_modal_options(work_locs)}")
    await asyncio.sleep(2)
    await py_auto_dismiss_errors()

    # 開 居住地 modal → 選城市 → 確定
    home_locs = SEARCH_CRITERIA.get('home_locations', [])
    print("\n📝 [Python-JS] 居住地: 開 modal")
    print(f"   結果: {await py_click_selector('居住地選單')}")
    await asyncio.sleep(2)
    print(f"   選 {home_locs} + 確定")
    print(f"   結果: {await py_select_modal_options(home_locs)}")
    await asyncio.sleep(2)
    await py_auto_dismiss_errors()

    # 展開「更多查詢條件」
    print("\n📝 [Python-JS] 展開更多查詢條件")
    print(f"   結果: {await py_expand_more_conditions()}")
    await asyncio.sleep(2)

    # 填年齡
    age_min = SEARCH_CRITERIA.get('age_min', 40)
    age_max = SEARCH_CRITERIA.get('age_max', 57)
    print(f"\n📝 [Python-JS] 填年齡 {age_min} ~ {age_max}")
    await py_set_input_by_name('agemin', str(age_min))
    await py_set_input_by_name('agemax', str(age_max))
    await asyncio.sleep(0.5)

    update_memory('B', 'done', 'Phase B 全部欄位填寫完成')

    # 開 科系 modal → 勾選 → 確定
    majors = SEARCH_CRITERIA.get('majors', [])
    if majors:
        print("\n📝 [Python-JS] 科系: 開 modal")
        # 科系 selector 透過 id="majorId"，沒有 title 屬性，用 JS 直接點擊
        await py_run_js("""
        (function() {
            const div = document.querySelector('#majorId .form-tag-selector') ||
                        document.querySelector('#majorId');
            if (div) div.click();
            return { ok: !!div };
        })();
        """)
        await asyncio.sleep(2)
        print(f"   勾選 {majors} + 確定")
        print(f"   結果: {await py_select_modal_options(majors)}")
        await asyncio.sleep(2)
        await py_auto_dismiss_errors()

    # 設定 總年資
    years = SEARCH_CRITERIA.get('work_exp_years')
    range_type = SEARCH_CRITERIA.get('work_exp_range', '以上')
    if years:
        print(f"\n📝 [Python-JS] 設定 總年資 = {years} 年 {range_type}")
        print(f"   結果: {await py_set_workexp_dropdown(years, range_type)}")
        await asyncio.sleep(1)

    # 開 擅長工具 modal → 勾選 → 確定
    tools = SEARCH_CRITERIA.get('tools', [])
    if tools:
        print("\n📝 [Python-JS] 擅長工具: 開 modal")
        await py_run_js("""
        (function() {
            const div = document.querySelector('#goodTools .form-tag-selector') ||
                        document.querySelector('#goodTools');
            if (div) div.click();
            return { ok: !!div };
        })();
        """)
        await asyncio.sleep(2)
        print(f"   勾選 {tools} + 確定")
        print(f"   結果: {await py_select_modal_options(tools)}")
        await asyncio.sleep(2)
        await py_auto_dismiss_errors()

    # 開 證照 modal → 勾選 → 確定
    certs = SEARCH_CRITERIA.get('certificates', [])
    if certs:
        print("\n📝 [Python-JS] 證照: 開 modal")
        await py_run_js("""
        (function() {
            const div = document.querySelector('#certificates .form-tag-selector') ||
                        document.querySelector('#certificates');
            if (div) div.click();
            return { ok: !!div };
        })();
        """)
        await asyncio.sleep(2)
        print(f"   勾選 {certs} + 確定")
        print(f"   結果: {await py_select_modal_options(certs)}")
        await asyncio.sleep(2)
        await py_auto_dismiss_errors()

    update_memory('C', 'done', 'Phase C 全部欄位填寫完成')

    # 點擊送出
    print("\n📝 [Python-JS] 送出搜尋（點擊「符合人數 XXX 人」）")
    submit_result = await py_click_submit_search()
    print(f"   結果: {submit_result}")
    await asyncio.sleep(5)
    await py_auto_dismiss_errors()

    final_url = await get_current_url()
    print(f"\n✅ 搜尋完成。最終 URL: {final_url}")

    # ===== Phase D: 擷取候選人清單 =====
    print("\n" + "="*60)
    print("🔍 Phase D: 擷取候選人清單")
    print("="*60)

    await asyncio.sleep(3)
    await py_auto_dismiss_errors()

    candidate_data = await py_extract_candidate_list()
    print(f"\n📊 找到 {candidate_data.get('count')} 位候選人")

    for i, c in enumerate(candidate_data.get('candidates', []), 1):
        print(f"\n   --- 候選人 #{i} ---")
        print(f"   姓名: {c.get('name')}  {c.get('age')} {c.get('gender')}")
        print(f"   履歷編號: {c.get('resumeId')}")
        print(f"   {c.get('preferArea', '')}")
        print(f"   {c.get('residence', '')}")
        print(f"   {c.get('education', '')}")
        print(f"   {c.get('preferJobTitle', '')[:80]}")
        print(f"   {c.get('workExpTotal', '')}")
        for exp in c.get('experiences', [])[:3]:
            print(f"     • {exp[:100]}")

    # 完整資料存 JSON
    import json
    candidates_path = os.path.join(SCRIPT_DIR, 'candidates_raw.json')
    with open(candidates_path, 'w', encoding='utf-8') as f:
        json.dump(candidate_data, f, ensure_ascii=False, indent=2)
    print(f"\n💾 完整資料已存到: {candidates_path}")

    print("\n" + "="*60)
    print(f"✅ All phases complete. See {MEMORY_FILE}")
    print("="*60)


asyncio.run(main())
