from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from open_webui.utils import chatgpt


@pytest.mark.asyncio
async def test_start_chatgpt_login_uses_loopback_pkce_and_plan_scope(monkeypatch):
    pending = SimpleNamespace(id='state-value', expires_at=123456)

    async def config_get(key):
        return None

    async def config_upsert(values):
        return None

    async def create(**values):
        return pending

    monkeypatch.setattr(chatgpt.Config, 'get', config_get)
    monkeypatch.setattr(chatgpt.Config, 'upsert', config_upsert)
    monkeypatch.setattr(chatgpt.AuthSessions, 'create', create)
    monkeypatch.setattr(chatgpt.OAuthSessions, 'get_session_by_provider_and_user_id', lambda *args, **kwargs: _none())

    result = await chatgpt.start_chatgpt_login('user-1')
    query = parse_qs(urlparse(result['authorization_url']).query)

    assert query['client_id'] == [chatgpt.DYNAMIC_CLIENT_ID]
    assert query['redirect_uri'] == [chatgpt.OPENAI_CHATGPT_REDIRECT_URI]
    assert query['code_challenge_method'] == ['S256']
    assert query['state'] == ['state-value']
    assert chatgpt.PLAN_USAGE_SCOPE in query['scope'][0].split()
    assert query['resource'] == [chatgpt.OPENAI_RESOURCE]


@pytest.mark.asyncio
async def test_reauthentication_reuses_issued_client_without_putting_tokens_in_url(monkeypatch):
    pending = SimpleNamespace(id='reauth-state', expires_at=123456)
    existing = SimpleNamespace(
        token={
            'client_id': 'oaiapp_issued',
            'email': 'user@example.com',
            'id_token': 'do-not-put-this-in-the-url',
        }
    )

    async def config_get(key):
        return 'urn:uuid:host'

    async def create(**values):
        return pending

    async def get_existing(*args, **kwargs):
        return existing

    monkeypatch.setattr(chatgpt.Config, 'get', config_get)
    monkeypatch.setattr(chatgpt.AuthSessions, 'create', create)
    monkeypatch.setattr(chatgpt.OAuthSessions, 'get_session_by_provider_and_user_id', get_existing)

    result = await chatgpt.start_chatgpt_login('user-1')
    query = parse_qs(urlparse(result['authorization_url']).query)

    assert query['client_id'] == ['oaiapp_issued']
    assert query['login_hint'] == ['user@example.com']
    assert 'id_token_hint' not in query
    assert 'do-not-put-this-in-the-url' not in result['authorization_url']


@pytest.mark.asyncio
async def test_model_catalog_keeps_only_visible_models(monkeypatch):
    async def access_token(user_id):
        return 'oauth-token'

    async def models_request(*args, **kwargs):
        return {
            'models': [
                {'slug': 'hidden-model', 'display_name': 'Hidden', 'visibility': 'hidden'},
                {'slug': 'visible-model', 'display_name': 'Visible', 'visibility': 'list'},
                {'display_name': 'Missing slug', 'visibility': 'list'},
            ]
        }

    monkeypatch.setattr(chatgpt, 'get_chatgpt_access_token', access_token)
    monkeypatch.setattr(chatgpt, '_request_json', models_request)

    result = await chatgpt.list_chatgpt_models('user-1')

    assert [model['id'] for model in result] == ['chatgpt.visible-model']
    assert result[0]['provider'] == chatgpt.CHATGPT_MODEL_PROVIDER
    assert result[0]['info']['user_id'] == 'user-1'


def test_chatgpt_payload_forces_safe_responses_options():
    payload = {
        'model': 'chatgpt.visible-model',
        'messages': [{'role': 'user', 'content': 'Hello'}],
        'stream': False,
        'store': True,
        'temperature': 0.4,
        'max_tokens': 100,
        'metadata': {'private': 'value'},
        'tools': [
            {
                'type': 'function',
                'function': {'name': 'lookup', 'parameters': {'type': 'object'}},
            }
        ],
    }

    converted = chatgpt._chatgpt_responses_payload(
        payload,
        {'id': 'chatgpt.visible-model', 'chatgpt': {'id': 'visible-model'}},
    )

    assert converted['model'] == 'visible-model'
    assert converted['stream'] is True
    assert converted['store'] is False
    assert converted['input'][0]['role'] == 'user'
    assert converted['tools'][0]['type'] == 'function'
    assert 'temperature' not in converted
    assert 'max_output_tokens' not in converted
    assert 'metadata' not in converted


@pytest.mark.asyncio
async def test_responses_stream_is_translated_to_chat_completions():
    async def response_events():
        events = [
            {
                'type': 'response.created',
                'response': {'id': 'resp_1', 'created_at': 123, 'model': 'visible-model'},
            },
            {'type': 'response.output_text.delta', 'delta': 'Hello'},
            {
                'type': 'response.completed',
                'response': {
                    'id': 'resp_1',
                    'created_at': 123,
                    'model': 'visible-model',
                    'status': 'completed',
                    'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3},
                },
            },
        ]
        for event in events:
            yield f"data: {chatgpt.JSONCodec.dumps(event)}\n\n"

    chunks = [
        chunk
        async for chunk in chatgpt.convert_responses_stream_to_chat_completions(response_events())
    ]
    joined = ''.join(chunks)

    assert '"content":"Hello"' in joined
    assert '"finish_reason":"stop"' in joined
    assert '"prompt_tokens":2' in joined
    assert joined.endswith('data: [DONE]\n\n')


def test_usage_limit_stream_error_gets_usage_link():
    chunk = (
        'data: {"error":{"code":"subscription_sharing_usage_limit_exceeded",'
        '"message":"limit reached"}}\n\n'
    )

    rewritten = chatgpt._rewrite_stream_error(chunk)

    assert chatgpt.USAGE_SETTINGS_URL in rewritten


async def _none():
    return None
