import os
import socket
import json
from urllib.parse import urlparse

# Prefer the OpenAI client if available; otherwise fall back to a tiny builtin HTTP client
try:
    from openai import OpenAI  # type: ignore
    _OPENAI_AVAILABLE = True
except Exception:
    _OPENAI_AVAILABLE = False

if not _OPENAI_AVAILABLE:
    import urllib.request
    import urllib.error


class QAgent:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("ROQ_MODEL", "meta-llama/llama-3.3-70b-versatile")
        self.base_url = base_url or os.environ.get("ROQ_BASE_URL", "https://api.roq.com/openai/v1")
        self.api_key = api_key or os.environ.get("ROQ_API_KEY")
        if _OPENAI_AVAILABLE:
            # The OpenAI client expects base_url to point to the OpenAI-compatible root
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            self.client = None
        self._check_dns()

    def _check_dns(self) -> None:
        parsed = urlparse(self.base_url)
        host = parsed.hostname
        if not host:
            return
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise RuntimeError(
                f"Unable to resolve the ROQ hostname '{host}'. Check your network/DNS and the ROQ base URL."
            ) from exc

    def ask(self, question: str) -> str:
        if _OPENAI_AVAILABLE and self.client is not None:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You answer directly and concisely."},
                    {"role": "user", "content": question},
                ],
                temperature=0.2,
                max_tokens=512,
            )
            # The OpenAI client returns an object; access content accordingly
            try:
                return response.choices[0].message.content.strip()
            except Exception:
                # Best-effort extraction
                data = getattr(response, 'to_dict', lambda: response)()
                return data['choices'][0]['message']['content'].strip()

        # Fallback path: use urllib to POST to the mock / real endpoint
        url = self.base_url.rstrip('/') + '/chat/completions'
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': 'You answer directly and concisely.'},
                {'role': 'user', 'content': question},
            ],
            'temperature': 0.2,
            'max_tokens': 512,
        }
        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode('utf-8')
                data = json.loads(resp_body)
        except urllib.error.HTTPError as he:
            # Try to return a helpful message
            try:
                error_body = he.read().decode('utf-8')
                return f"Mock server HTTPError {he.code}: {error_body}"
            except Exception:
                return f"Mock server HTTPError {he.code}"
        except Exception as exc:
            raise

        # Support both chat completion shape and older text completion shape
        try:
            # chat shape
            return data['choices'][0]['message']['content'].strip()
        except Exception:
            try:
                # legacy completions with 'text'
                return data['choices'][0].get('text', '').strip()
            except Exception:
                return json.dumps(data)[:100]


if __name__ == "__main__":
    agent = QAgent()
    print(agent.ask("Give me a one-sentence summary of the ROQ OpenAI-compatible endpoint."))
