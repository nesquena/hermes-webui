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

DEFAULT_ENDPOINT = 'http://localhost:11434'
# 實測：qwen3.5:9b 在長中文 prompt 上會回空，故改用 gemma4
DEFAULT_SCORING_MODEL = 'gemma4:e4b'    # 9.6GB，~40s/履歷
DEFAULT_SUMMARY_MODEL = 'gemma4:26b'    # 17GB，~80s/封信件，品質佳
TIMEOUT_SECS = 300                       # 冷啟動模型載入要花較久


def ollama_call(model: str, prompt: str, want_json: bool = False,
                endpoint: str = DEFAULT_ENDPOINT) -> str | None:
    """呼叫 Ollama /api/generate。失敗回 None。
    注意：want_json=True 時不依賴 Ollama 的 format=json（部分模型如 qwen3.5:9b 會回空），
    改用 prompt 指示 + 從輸出抽 JSON 區塊。"""
    payload = {
        'model': model,
        'prompt': prompt,
        'stream': False,
        'options': {'temperature': 0, 'num_predict': 2000},
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
    cfg.setdefault('rule_weight', 0.6)      # 規則分權重
    cfg.setdefault('llm_weight', 0.4)       # LLM 分權重
    return cfg


def score_autobiography(body: str, profile: dict) -> dict | None:
    """讓 LLM 對履歷自傳/工作內容打 0-20 語意分。
    回傳 {score, reasoning} 或 None（失敗）"""
    cfg = get_llm_config(profile)
    if not cfg.get('enabled') or not body or len(body) < 200:
        return None

    sc = profile.get('scoring', {})
    job_name = profile.get('display_name', '')
    residential_kw = '/'.join(sc.get('residential_kw', []))
    public_kw = '/'.join(sc.get('public_kw', [])[:6])
    construction_kw = '/'.join(sc.get('construction_kw', []))
    title_kw = '/'.join(sc.get('title_kw', []))

    body_excerpt = body[:6000]  # 控制 prompt 長度

    prompt = f"""你是專業的人資招募官，要為「{job_name}」職缺評估這份履歷的「軟性條件」匹配度。

職缺背景：
- 期望相關職稱：{title_kw}
- 期望公司類型：{construction_kw}
- 加分建案類型：集合住宅 ({residential_kw}) > 公共工程 ({public_kw})

評估重點（不要評估年資/年齡/地點，這些已經有規則處理）：
1. 自傳是否展現積極正向工作態度（認真、負責、主動承擔、解決問題、樂於溝通協調）
2. 工作經歷描述是否具體、能看出他完整走過建案生命週期
3. 集合住宅／建築工程經驗是否豐富紮實（多個建案優於單一建案）
4. 是否誠信穩定（強烈正面：明確的責任感、操守表述；負面：頻繁跳槽抱怨）

評分尺度（0-100）：
- 90-100：自傳精彩、多個完整建案週期、態度極佳、操守誠信突出
- 75-89：自傳清楚、有建案經驗、態度正向、能看出責任感
- 60-74：自傳合格、經驗夠用但偏短或細節不足
- 40-59：自傳薄弱或經驗零散、軟性訊號普通
- 20-39：自傳不利或工作經驗離職務需求遠
- 0-19：完全不符或無自傳

履歷內容：
---
{body_excerpt}
---

請輸出 JSON，格式：
{{
  "score": <0-100 整數>,
  "level_assessment": "<主任級候選 / 副主任級候選 / 不符等級>",
  "reasoning": "<繁體中文 80-150 字，列出加分點與扣分點>",
  "highlights": ["<亮點1>", "<亮點2>", "<亮點3>"]
}}"""

    raw = ollama_call(cfg['scoring_model'], prompt, want_json=False, endpoint=cfg['endpoint'])
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
        }
    except Exception as e:
        print(f'  ⚠️  LLM 評分擷取失敗: {e}')
        return None


def generate_summary(name: str, body: str, rule_result: dict,
                     llm_score_result: dict | None, profile: dict,
                     max_chars: int = 980) -> str | None:
    """讓 LLM 為達門檻人選生成「說明」欄客製化文字。失敗回 None（呼叫端 fallback 範本）"""
    cfg = get_llm_config(profile)
    if not cfg.get('enabled'):
        return None

    # 嚴格用 HR 原始檔名作為職缺名稱（不可用 LLM 解析出來的 display_name）
    import re as _re
    source = profile.get('_source_brief', '')
    if not source:
        return None  # 無 _source_brief 則拒絕產生 summary（呼叫端 fallback 範本）
    job_name = _re.sub(r'\.(docx|doc|txt|md)$', '', source)
    job_name = _re.sub(r'_\d{8}$', '', job_name)
    if not job_name:
        return None
    residence_kw = profile.get('scoring', {}).get('residence_bonus_keyword', '')

    body_excerpt = body[:5000]
    rule_reasons = '；'.join(rule_result.get('reasons', [])[:8])
    llm_block = ''
    if llm_score_result:
        llm_block = (
            f"\nLLM 軟性評分：{llm_score_result['score']}/20\n"
            f"LLM 評語：{llm_score_result.get('reasoning','')}\n"
            f"LLM 亮點：{', '.join(llm_score_result.get('highlights', []))}\n"
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

■建議行動（1 句：是否優先邀約面試）

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
    """回傳 {ok: bool, models: list, error: str}"""
    try:
        with urllib.request.urlopen(f'{endpoint}/api/tags', timeout=3) as resp:
            data = json.loads(resp.read())
            return {'ok': True, 'models': [m['name'] for m in data.get('models', [])]}
    except Exception as e:
        return {'ok': False, 'models': [], 'error': str(e)}


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
