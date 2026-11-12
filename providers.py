"""Các provider cùng một interface. Không chứa agent loop."""
import json
import math
import os
from pathlib import Path
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def _load_env():
    """Đọc .env cạnh file này; biến đã đặt trong terminal được ưu tiên."""
    path = Path(__file__).resolve().parent / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].strip()
            name, separator, value = line.partition('=')
            if separator:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                os.environ.setdefault(name.strip(), value)
    if os.getenv('API_MODEL'):
        os.environ.setdefault('MODEL', os.environ['API_MODEL'])


_load_env()

CONFIG = {
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = {}


def _pace_request(url, model):
    """Giãn cách request theo endpoint/model, dùng chung cho agent và judge."""
    interval = float(os.getenv('API_MIN_INTERVAL_SECONDS', '0'))
    if not math.isfinite(interval) or interval < 0:
        raise ValueError('API_MIN_INTERVAL_SECONDS phải là số hữu hạn >= 0.')
    if interval == 0:
        return
    key = (url, model)
    with _REQUEST_LOCK:
        now = time.monotonic()
        delay = _LAST_REQUEST.get(key, now - interval) + interval - now
        if delay > 0:
            time.sleep(delay)
        _LAST_REQUEST[key] = time.monotonic()


def _retry_delay(attempt, retry_after=None):
    """Tôn trọng Retry-After; không thử sớm nếu server yêu cầu chờ quá budget."""
    delay = min(60, 2 ** (attempt + 1) + random.uniform(0, 1))
    if retry_after is not None:
        try:
            requested = float(retry_after)
        except (ValueError, TypeError):
            try:
                requested = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return delay
        if not math.isfinite(requested) or requested > 60:
            return None
        delay = max(delay, requested)
    return delay


def post_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'StatusAgentLab/1.0', **headers})
    for attempt in range(4):
        _pace_request(url, payload.get('model'))
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            exc.close()
            if code in (408, 429, 500, 502, 503, 504) and attempt < 3:
                delay = _retry_delay(attempt, retry_after)
                if delay is not None:
                    time.sleep(delay)
                    continue
            # Do not print response bodies: they may contain account or request data.
            if code == 401:
                raise RuntimeError('Provider trả HTTP 401: không xác thực được API key; kiểm tra key và workspace của endpoint.') from exc
            raise RuntimeError(f'Provider trả HTTP {code}; kiểm tra model, key và quota. Đã dừng retry trong budget cho phép.') from exc
        except (urllib.error.URLError, OSError, HTTPException) as exc:
            if attempt < 3:
                time.sleep(_retry_delay(attempt))
                continue
            raise RuntimeError(f'Không kết nối được provider sau 4 lần thử ({type(exc).__name__}).') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        self.session_id = str(uuid.uuid4())
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if name in ('openai', 'gemini', 'groq'):
            self.key = self.key or os.getenv('API_KEY')
        if name == 'openai':
            if os.getenv('API_ENDPOINT'):
                self.url = os.environ['API_ENDPOINT'].rstrip('/')
                if not self.url.endswith('/chat/completions'):
                    self.url += '/chat/completions'
            if urlsplit(self.url).hostname == 'opencode.ai':
                self.key = os.getenv('OPENCODE_GO_API_KEY') or self.key
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        if self.name == 'groq' and self.model == 'openai/gpt-oss-20b':
            payload.update(reasoning_effort='low', include_reasoning=False, max_completion_tokens=2048)
        headers = {'Authorization': f'Bearer {self.key}'}
        if self.name == 'gemini':
            headers['x-goog-api-key'] = self.key
            if self.model == 'gemini-2.5-flash':
                payload['reasoning_effort'] = 'none'
        if urlsplit(self.url).hostname == 'opencode.ai':
            headers['x-opencode-session'] = self.session_id
        data = post_json(self.url, headers, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
