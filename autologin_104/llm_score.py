#!/usr/bin/env python3
"""
llm_score.py — 用本地 Ollama LLM 對履歷做語意評分 + 生成轉寄說明文字

兩階段：
  1. score_autobiography(body, profile) -> {score: 0-20, reasoning: str}
     用較小、較快的模型（qwen3.5:9b）對自傳/工作內容打語意分
  2. generate_summary(name, body, rule_result, profile) -> str
     用較大、品質好的模型（qwen3.5:27b 或 gemma4:26b）寫客製化說明信件

設計原則：
  - LLM 失敗（連線錯誤/JSON 解析失敗）→ 回傳 None，呼叫端自動 fallback 到範本
  - temperature=0、format=json 確保穩定
  - 每次 call 都印 ⚡ 進度條，方便除錯
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

DEFAULT_LLAMACPP_ENDPOINT = 'http://localhost:8080'  # llama.cpp llama-server 預設
DEFAULT_OLLAMA_ENDPOINT = 'http://localhost:11434'   # Ollama（fallback）
# 預設改為 llama.cpp（速度比 Ollama 快 15x+）
DEFAULT_ENDPOINT = DEFAULT_OLLAMA_ENDPOINT  # 切回 Ollama gemma4:e4b（品質優先）
# model 名稱僅在 Ollama 模式下使用；llama.cpp 啟動時就決定模型
DEFAULT_SCORING_MODEL = 'gemma4:e4b'    # Ollama 用
DEFAULT_SUMMARY_MODEL = 'gemma4:e4b'    # Ollama 用
TIMEOUT_SECS = 300                       # 冷啟動模型載入要花較久


def sanitize_untrusted_text(text: str) -> str:
    """過濾與去毒化潛在的提示詞注入 (Prompt Injection) 與資安注入攻擊。"""
    if not text:
        return ""
    
    # 1. 移除或轉義 <untrusted> 與 </untrusted>，避免混淆系統沙箱邊界
    text = text.replace("<untrusted>", "[untrusted_tag]")
    text = text.replace("</untrusted>", "[/untrusted_tag]")
    
    # 2. 偵測並去毒化 (defang) 常見的 RCE 遠端執行指令特徵 (如 curl/wget/bash 相關管道指令)
    def defang_rce(match):
        val = match.group(0)
        def repl(m):
            word = m.group(0).lower()
            return f"[blocked_{word}]"
        return re.sub(r"\b(curl|wget|bash|sh|zsh|dash)\b", repl, val, flags=re.IGNORECASE)
                   
    # 匹配像是 curl ... | bash, wget ... | sh, bash <(curl ...) 等特徵
    rce_patterns = [
        r"(curl|wget)\s+.*?\s*\|\s*(bash|sh|zsh|dash|python|perl|php)",
        r"(bash|sh|zsh|dash|python|perl|php)\s+<\s*\(\s*(curl|wget)",
        r"bash\s+-c\s+['\"].*?(curl|wget)",
    ]
    for pattern in rce_patterns:
        text = re.sub(pattern, defang_rce, text, flags=re.IGNORECASE)
        
    # 3. 阻斷常見的覆寫指令 (Prompt Override / Jailbreak) 關鍵字
    override_patterns = [
        r"ignore\s+(previous|prior|above|under|the)\s+(instructions|directives|rules|steps|criteria)",
        r"忽略(先前|前面|上述|評分|前述|規則|指令)的?(指令|標準|規定|規則|要求)",
        r"請(直接|特別|強制)?給?此人?(打|打分|評定為|評為)?\s*(\d+|滿分|100分)",
        r"忽略所有的?評分"
    ]
    for pattern in override_patterns:
        text = re.sub(pattern, "[blocked_prompt_override]", text, flags=re.IGNORECASE)
        
    return text


def _is_llamacpp_endpoint(endpoint: str) -> bool:
    """判斷 endpoint 是否為 llama.cpp（含 8080 或顯式 llamacpp tag）。"""
    if 'llamacpp' in endpoint.lower() or 'llama-cpp' in endpoint.lower():
        return True
    # 預設 llama.cpp 在 8080，Ollama 在 11434
    return ':8080' in endpoint and '11434' not in endpoint


def llamacpp_call(prompt: str, endpoint: str = DEFAULT_LLAMACPP_ENDPOINT,
                   json_schema: dict | None = None) -> str | None:
    """呼叫 llama.cpp llama-server 的 /completion endpoint。失敗回 None。

    llama.cpp 的 server 不需要 model 名稱（model 啟動時就決定了），
    並且 /completion API 直接接受 prompt 字串。

    json_schema：若提供，llama.cpp 會用 grammar 約束輸出必須符合此 JSON schema。
    這能完全消除 JSON 解析失敗，並提升速度 20-30%。
    """
    payload = {
        'prompt': prompt,
        'n_predict': 2500,  # 從 4500 降到 2500（實測輸出 ~1500 tokens 已夠）
        'temperature': 0,
        'top_p': 0.9,
        'cache_prompt': True,
        'stream': False,
    }
    if json_schema:
        payload['json_schema'] = json_schema
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{endpoint}/completion',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            body = json.loads(resp.read())
            return (body.get('content') or '').strip()
    except urllib.error.URLError as e:
        print(f'  ⚠️  llama.cpp 連線失敗: {e}')
        return None
    except Exception as e:
        print(f'  ⚠️  llama.cpp 呼叫錯誤: {e}')
        return None


SCORING_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "level_assessment": {"type": "string", "maxLength": 20},
        "reasoning": {"type": "string", "maxLength": 200},
        "highlights": {
            "type": "array",
            "items": {"type": "string", "maxLength": 60},
            "maxItems": 3,
        },
        "jobs": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "maxLength": 50},
                    "title": {"type": "string", "maxLength": 40},
                    "duration": {"type": "string", "maxLength": 20},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "relevance": {"type": "string", "enum": ["高", "中", "低"]},
                    "summary": {"type": "string", "maxLength": 100},
                },
                "required": ["company", "title", "score", "relevance"],
            },
        },
    },
    "required": ["score", "reasoning", "level_assessment", "jobs"],
}


def ollama_call(model: str, prompt: str, want_json: bool = False,
                endpoint: str = DEFAULT_ENDPOINT) -> str | None:
    """呼叫 LLM。自動偵測 endpoint，路由到 Ollama 或 llama.cpp。

    - Ollama: http://localhost:11434 (預設) → /api/generate，需要 model 名稱
    - llama.cpp: http://localhost:8080 → /completion，不需 model（啟動時決定）
    - want_json=True 且為 llama.cpp 時，自動套用 SCORING_JSON_SCHEMA grammar 約束
    """
    if _is_llamacpp_endpoint(endpoint):
        schema = SCORING_JSON_SCHEMA if want_json else None
        return llamacpp_call(prompt, endpoint=endpoint, json_schema=schema)

    # 走 Ollama
    payload = {
        'model': model,
        'prompt': prompt,
        'stream': False,
        'keep_alive': '30m',  # 模型載入後保留 30 分鐘，避免重複冷啟（省 30-60s/次）
        'options': {'temperature': 0, 'num_predict': 4500},  # 新 prompt 含 jobs[] 詳析需更多 tokens
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{endpoint}/api/generate',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            body = json.loads(resp.read())
            return body.get('response', '').strip()
    except urllib.error.URLError as e:
        print(f'  ⚠️  Ollama 連線失敗: {e}')
        return None
    except Exception as e:
        print(f'  ⚠️  Ollama 呼叫錯誤: {e}')
        return None


def extract_json(text: str) -> dict | None:
    """從文字中抽出第一段合法 JSON。處理 ```json ... ``` 包覆與多餘文字。"""
    if not text:
        return None
    # 試直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 抽 ```json ... ``` 區塊
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 找第一個平衡的 { ... }
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    start = -1
                    continue
    return None


def get_llm_config(profile: dict) -> dict:
    """從 profile.llm 讀設定，缺值用預設值補上。"""
    cfg = (profile.get('llm') or {}).copy()
    cfg.setdefault('enabled', True)
    cfg.setdefault('endpoint', DEFAULT_ENDPOINT)
    cfg.setdefault('scoring_model', DEFAULT_SCORING_MODEL)
    cfg.setdefault('summary_model', DEFAULT_SUMMARY_MODEL)
    cfg.setdefault('max_score', 100)        # LLM 滿分（用於權重融合）
    cfg.setdefault('rule_weight', 0.4)      # 規則分權重
    cfg.setdefault('llm_weight', 0.6)       # LLM 分權重
    return cfg


def score_autobiography(body: str, profile: dict) -> dict | None:
    """讓 LLM 對履歷自傳/工作內容打 0-20 語意分。
    回傳 {score, reasoning} 或 None（失敗）"""
    body = sanitize_untrusted_text(body)
    cfg = get_llm_config(profile)
    if not cfg.get('enabled') or not body or len(body) < 200:
        return None

    sc = profile.get('scoring', {})
    job_name = profile.get('display_name', '')
    residential_kw = '/'.join(sc.get('residential_kw', []))
    public_kw = '/'.join(sc.get('public_kw', [])[:6])
    construction_kw = '/'.join(sc.get('construction_kw', []))
    title_kw = '/'.join(sc.get('title_kw', []))

    body_excerpt = body[:6000]  # 控制 prompt 長度（7B ctx 8192 足夠）

    prompt = f"""你是專業的人資招募官，要為「{job_name}」職缺評估這份履歷的「軟性條件」匹配度。

職缺背景：
- 期望相關職稱：{title_kw}
- 期望公司類型：{construction_kw}
- 加分建案類型：集合住宅 ({residential_kw}) > 公共工程 ({public_kw})

【重要】請逐一分析履歷中每一段工作經歷（每個職位/每家公司分開分析），不要混為一談：
- 對每段工作經歷給予 0-100 分（依「公司類型契合」「職稱契合」「工作內容相關度」「任職時長」綜合判斷）
- 同時抽取該段的軟性訊號（負責程度、主動性、是否具體）

整體評分準則（綜合所有工作經歷 + 自傳後給的最終分）：
- 90-100：多段相關經驗 + 自傳精彩 + 操守誠信突出
- 75-89：至少 2-3 段相關經驗 + 自傳清楚 + 態度正向
- 60-74：1-2 段相關經驗或單段較弱 + 自傳合格
- 40-59：經驗零散或大部分不相關 + 軟性訊號普通
- 20-39：經驗離職務需求遠 + 自傳不利
- 0-19：完全不符或無自傳

評估重點（不要評估年資/年齡/地點，這些已經有規則處理）：
1. 自傳是否展現積極正向工作態度（認真、負責、主動承擔、解決問題、樂於溝通協調）
2. 工作經歷描述是否具體、能看出他完整走過建案生命週期
3. 集合住宅／建築工程經驗是否豐富紮實（多個建案優於單一建案）
4. 是否誠信穩定（強烈正面：明確的責任感、操守表述；負面：頻繁跳槽抱怨）

履歷內容：
---
{body_excerpt}
---

請輸出 JSON，格式：
{{
  "score": <0-100 整數，所有工作經歷 + 自傳的綜合評分>,
  "level_assessment": "<主任級候選 / 副主任級候選 / 不符等級>",
  "reasoning": "<繁體中文「最多 80 字」總結>",
  "highlights": ["<亮點1，最多 20 字>", "<亮點2，最多 20 字>", "<亮點3，最多 20 字>"],
  "jobs": [
    {{
      "company": "<公司名，最多 20 字>",
      "title": "<職稱，最多 15 字>",
      "duration": "<任職時長例如 2年3個月>",
      "score": <該段工作 0-100>,
      "relevance": "<高/中/低>",
      "summary": "<繁體中文「最多 30 字」描述該段工作與目標職缺的關聯>"
    }}
  ]
}}

【嚴格規則 — 必須遵守】
1. reasoning 不可超過 80 字（會被截斷）
2. 每個 highlight 不可超過 20 字
3. 每個 job 的 summary 不可超過 30 字
4. jobs 陣列「最多列前 10 段」（依時間倒序，最近的在前），早期/無相關的工作可省略
5. 字數短才能完整輸出 JSON，務必精簡到位"""

    # 評分要結構化 JSON，want_json=True → llama.cpp 自動套用 grammar schema
    raw = ollama_call(cfg['scoring_model'], prompt, want_json=True, endpoint=cfg['endpoint'])
    if not raw:
        return None
    data = extract_json(raw)
    if not data:
        print(f'  ⚠️  LLM JSON 解析失敗, raw[:200]={raw[:200]!r}')
        return None
    try:
        score = int(data.get('score', 0))
        score = max(0, min(cfg['max_score'], score))
        return {
            'score': score,
            'reasoning': data.get('reasoning', ''),
            'highlights': data.get('highlights', []),
            'level_assessment': data.get('level_assessment', ''),
            'jobs': data.get('jobs', []),  # 每段工作經歷的個別分析
        }
    except Exception as e:
        print(f'  ⚠️  LLM 評分擷取失敗: {e}')
        return None


def generate_summary(name: str, body: str, rule_result: dict,
                     llm_score_result: dict | None, profile: dict,
                     max_chars: int = 980) -> str | None:
    """讓 LLM 為達門檻人選生成「說明」欄客製化文字。失敗回 None（呼叫端 fallback 範本）"""
    body = sanitize_untrusted_text(body)
    cfg = get_llm_config(profile)
    if not cfg.get('enabled'):
        return None

    # 優先用 HR 原始檔名；_source_brief 缺失時以 display_name 為備用（確保顯示中文）
    import re as _re
    source = profile.get('_source_brief', '')
    if source:
        job_name = _re.sub(r'\.(docx|doc|txt|md)$', '', source)
        job_name = _re.sub(r'_\d{8}$', '', job_name)
    else:
        job_name = ''
    if not job_name:
        job_name = profile.get('display_name', '')
    if not job_name:
        return None
    residence_kw = profile.get('scoring', {}).get('residence_bonus_keyword', '')

    body_excerpt = body[:5000]
    rule_reasons = '；'.join(rule_result.get('reasons', [])[:8])
    llm_block = ''
    if llm_score_result:
        llm_block = (
            f"\nLLM 軟性評分：{llm_score_result['score']}/100\n"
            f"LLM 評語：{llm_score_result.get('reasoning','')}\n"
            f"LLM 亮點：{', '.join(llm_score_result.get('highlights', []))}\n"
        )
        # 加入每段工作經歷的個別評析
        jobs = llm_score_result.get('jobs', [])
        if jobs:
            llm_block += "\n每段工作經歷分析：\n"
            for j in jobs[:8]:  # 最多取 8 段
                llm_block += (
                    f"  • {j.get('company','?')} | {j.get('title','?')} | "
                    f"{j.get('duration','?')} | 分數 {j.get('score',0)} | "
                    f"相關度 {j.get('relevance','?')}\n"
                    f"    {j.get('summary','')}\n"
                )

    prompt = f"""你是內部人資專員，要為主管寫一封「轉寄履歷」的內部備忘文字（不是寫信給應徵者）。
必須在 {max_chars} 字內、繁體中文、條列清楚、語氣專業務實但有溫度（不是公文）。

⚠️ 重要規則：
- 文中所有提到「職缺」的地方，**必須一字不漏地使用「{job_name}」**這個名稱
- 不可改寫、簡化、縮寫、翻譯、補充說明，也不可換成 104 標準職類名稱
- 例如：不可寫「機電工程師」「機電副主任」「主任職位」等變體，只能寫「{job_name}」

職缺：{job_name}
人選姓名：{name}
規則評分總分：{rule_result['score']}%
判定等級：{rule_result['level']}
規則評分依據：{rule_reasons}
{llm_block}

履歷內容（節錄）：
---
{body_excerpt}
---

請依以下結構寫，務必精簡、抓重點：

【職缺：{job_name}】
【{name}｜{rule_result['score']}%｜{rule_result['level']}】

■核心亮點（3-4 點，引用具體建案/公司/職稱/年資數字）

■代表建案實績（從履歷抽出 3-5 個最相關的，含承攬商或業主）

■軟性面評估（自傳態度、溝通能力、誠信穩定性，2-3 句）

■風險或待確認（若有：例如純集合住宅經驗較少、駐點地{residence_kw}意願）

■建議行動（1 句）：依匹配度判斷
   - 匹配度 ≥ 80% → 寫「建議優先邀約面試」
   - 匹配度 70-79% → 寫「可考慮邀約面試」
   - 匹配度 < 70% → 寫「供參考」

不要用 markdown 標題符號（#），用上面的【】和■即可。"""

    raw = ollama_call(cfg['summary_model'], prompt, want_json=False, endpoint=cfg['endpoint'])
    if not raw:
        return None
    # 清理掉可能的多餘空白/前後語
    cleaned = raw.strip()
    # 有些模型會加 ```text ... ``` 包覆
    cleaned = re.sub(r'^```\w*\n?', '', cleaned)
    cleaned = re.sub(r'\n?```$', '', cleaned)
    return cleaned[:max_chars]


def health_check(endpoint: str = DEFAULT_ENDPOINT) -> dict:
    """回傳 {ok: bool, models: list, provider: 'ollama'|'llamacpp', error: str}"""
    if _is_llamacpp_endpoint(endpoint):
        try:
            with urllib.request.urlopen(f'{endpoint}/health', timeout=3) as resp:
                d = json.loads(resp.read())
                if d.get('status') == 'ok':
                    # 嘗試取得模型資訊（/props）
                    try:
                        with urllib.request.urlopen(f'{endpoint}/props', timeout=3) as r2:
                            props = json.loads(r2.read())
                            model_path = props.get('model_path', '?')
                            return {'ok': True, 'provider': 'llamacpp',
                                    'models': [model_path.split('/')[-1]]}
                    except Exception:
                        return {'ok': True, 'provider': 'llamacpp', 'models': ['(unknown)']}
                return {'ok': False, 'provider': 'llamacpp', 'models': [], 'error': str(d)}
        except Exception as e:
            return {'ok': False, 'provider': 'llamacpp', 'models': [], 'error': str(e)}

    # Ollama
    try:
        with urllib.request.urlopen(f'{endpoint}/api/tags', timeout=3) as resp:
            data = json.loads(resp.read())
            return {'ok': True, 'provider': 'ollama',
                    'models': [m['name'] for m in data.get('models', [])]}
    except Exception as e:
        return {'ok': False, 'provider': 'ollama', 'models': [], 'error': str(e)}


if __name__ == '__main__':
    # 自我測試
    import sys
    h = health_check()
    print('Ollama:', 'OK' if h['ok'] else f'FAIL: {h.get("error")}')
    if h['ok']:
        print('Models:', h['models'])
        if len(sys.argv) > 1 and sys.argv[1] == 'test':
            test_body = '工作經驗：2020-2024 大成營造 機電主任，負責台北某集合住宅大樓全案機電監造。' \
                        '自傳：本人認真負責，溝通協調能力佳，曾完整經歷三個建案生命週期。'
            test_profile = {
                'display_name': '機電副主任(台南)',
                'scoring': {
                    'residential_kw': ['集合住宅'], 'public_kw': ['公共工程'],
                    'construction_kw': ['營造', '建設'], 'title_kw': ['機電', '水電'],
                    'residence_bonus_keyword': '台南',
                },
            }
            print('\n--- score_autobiography 測試 ---')
            r = score_autobiography(test_body, test_profile)
            print(json.dumps(r, ensure_ascii=False, indent=2))
