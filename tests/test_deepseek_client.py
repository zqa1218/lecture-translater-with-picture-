"""DeepSeek 客户端：跳过规则、批量翻译、模型回退、推理模型空正文兜底。

全程用本地假 HTTP 服务，不花 API 费用、不依赖外网。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deepseek_client import DeepSeekClient, TranslationError, should_translate


class _Handler(BaseHTTPRequestHandler):
    responses: list = []
    requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(body)
        payload = type(self).responses.pop(0) if type(self).responses else {"choices": []}
        status = 200
        if isinstance(payload, tuple):
            status, payload = payload
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):  # noqa: D102
        pass


@pytest.fixture()
def fake_api():
    _Handler.responses = []
    _Handler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", _Handler
    server.shutdown()


def _reply(content: str, *, model="deepseek-flash", finish="stop"):
    return {
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
    }


def test_should_translate_filters_noise():
    assert should_translate("Transcranial magnetic stimulation") is True
    assert should_translate("1,360results") is True
    assert should_translate("   ") is False
    assert should_translate("12.5") is False
    assert should_translate("----") is False
    assert should_translate("（），。") is False


def test_translate_single(fake_api):
    url, handler = fake_api
    handler.responses.append(_reply("经颅磁刺激"))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url)
    assert client.translate("TMS", src_lang="English", tgt_lang="Chinese") == "经颅磁刺激"


def test_translate_batch_keeps_order(fake_api):
    url, handler = fake_api
    handler.responses.append(_reply('["阳极", "阴极", "2.0mA"]'))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url)
    assert client.translate_batch(["Anode", "Cathode", "2.0mA"]) == ["阳极", "阴极", "2.0mA"]


def test_translate_batch_falls_back_to_single_calls(fake_api):
    url, handler = fake_api
    # 第一次批量返回条数不符 -> 触发退化为逐条
    handler.responses.append(_reply('["只有一个"]'))
    handler.responses.append(_reply("阳极"))
    handler.responses.append(_reply("阴极"))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url, retries=1)
    assert client.translate_batch(["Anode", "Cathode"]) == ["阳极", "阴极"]


def test_batch_strips_code_fence(fake_api):
    url, handler = fake_api
    handler.responses.append(_reply('```json\n["阳极", "阴极"]\n```'))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url)
    assert client.translate_batch(["Anode", "Cathode"]) == ["阳极", "阴极"]


def test_model_fallback_on_unknown_model(fake_api):
    url, handler = fake_api
    handler.responses.append((400, {"error": {"message": "Model not found: bogus-model"}}))
    handler.responses.append(_reply("回退成功", model="deepseek-flash"))
    client = DeepSeekClient("sk-bogus", "bogus-model", api_base=url, retries=1)
    assert client.translate("hello") == "回退成功"
    assert handler.requests[-1]["model"] == "deepseek-flash"


def test_reasoning_model_empty_content_retries_without_max_tokens(fake_api):
    """推理模型会把 max_tokens 全花在思考上、正文为空 —— 客户端必须去掉上限重试。"""
    url, handler = fake_api
    handler.responses.append(_reply("", finish="length"))
    handler.responses.append(_reply("正文"))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url, retries=1)
    assert client.chat("写点什么", max_tokens=6000) == "正文"
    assert "max_tokens" in handler.requests[0]
    assert "max_tokens" not in handler.requests[1]


def test_missing_api_key_raises():
    with pytest.raises(TranslationError):
        DeepSeekClient("", "deepseek-flash")


def test_json_chat_parses_object(fake_api):
    url, handler = fake_api
    handler.responses.append(_reply('{"order": [1, 0], "reason": "承接"}'))
    client = DeepSeekClient("sk-test", "deepseek-flash", api_base=url)
    assert client.json_chat("排序") == {"order": [1, 0], "reason": "承接"}
