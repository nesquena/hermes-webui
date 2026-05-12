import requests

def call_ollama_api(prompt: str, model: str, max_tokens: int = 512) -> str:
    payload={
        'model': model,
        'messages': [{'role':'user','content':prompt}],
        'stream': False,
        'think': False,
        'options': {'num_predict': max_tokens},
    }
    resp=requests.post('http://127.0.0.1:11434/api/chat', json=payload, timeout=120)
    resp.raise_for_status()
    data=resp.json()
    if isinstance(data, dict):
        if isinstance(data.get('message'), dict) and data['message'].get('content') is not None:
            return data['message']['content']
        if data.get('response') is not None: return data['response']
    return ''
