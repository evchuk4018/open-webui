"""Sign in with ChatGPT and ChatGPT-plan Responses API support.

The ChatGPT-plan flow is deliberately kept separate from OpenAI-compatible API
keys.  The OAuth credentials stay in the encrypted OAuth-session table and are
only used by the backend when a user selects a ChatGPT-plan model.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import secrets
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import aiohttp
import jwt
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    OPENAI_CHATGPT_REDIRECT_URI,
)
from open_webui.models.auth_sessions import AuthSessions
from open_webui.models.config import Config
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.response import convert_responses_stream_to_chat_completions
from open_webui.utils.session_pool import cleanup_response, get_client_timeout, get_session, stream_wrapper

log = logging.getLogger(__name__)

CHATGPT_PROVIDER = 'openai_chatgpt'
CHATGPT_MODEL_PROVIDER = 'openai_chatgpt_plan'
CHATGPT_MODEL_PREFIX = 'chatgpt.'

AUTHORIZATION_ENDPOINT = 'https://auth.openai.com/api/accounts/authorize'
TOKEN_ENDPOINT = 'https://auth.openai.com/api/accounts/oauth/token'
OPENAI_RESOURCE = 'https://api.openai.com/v1'
OPENAI_MODELS_ENDPOINT = f'{OPENAI_RESOURCE}/models'
OPENAI_RESPONSES_ENDPOINT = f'{OPENAI_RESOURCE}/responses'
OPENID_CONFIGURATION_ENDPOINT = 'https://auth.openai.com/.well-known/openid-configuration'
ISSUER = 'https://auth.openai.com'
DYNAMIC_CLIENT_ID = 'dynamic_agent_client'
AGENT_NAME = 'Open WebUI'
PLAN_USAGE_SCOPE = 'chatgpt.tokens.use.direct'
OAUTH_SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
USAGE_LIMIT_CODES = {
    'subscription_sharing_usage_limit_exceeded',
    'subscription_sharing_usage_unavailable',
}
USAGE_SETTINGS_URL = 'https://chatgpt.com/settings/usage'
PENDING_LOGIN_TTL = 10 * 60
TOKEN_REFRESH_SKEW = 60

_refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class ChatGPTError(HTTPException):
    """Safe, structured errors for ChatGPT-plan operations."""

    def __init__(self, message: str, status_code: int = 503, code: str = 'chatgpt_error'):
        self.message = message
        self.code = code
        super().__init__(
            status_code=status_code,
            detail={'error': {'message': message, 'code': code}},
        )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _pkce_pair() -> tuple[str, str]:
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge


def _validate_redirect_uri() -> str:
    parsed = urlparse(OPENAI_CHATGPT_REDIRECT_URI)
    if (
        parsed.scheme != 'http'
        or parsed.hostname != '127.0.0.1'
        or not parsed.path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ChatGPTError(
            'The ChatGPT callback must be an HTTP loopback URL on 127.0.0.1.',
            status_code=500,
            code='chatgpt_invalid_redirect_uri',
        )
    return OPENAI_CHATGPT_REDIRECT_URI


def _safe_error_payload(payload) -> tuple[str, str]:
    if isinstance(payload, dict):
        error = payload.get('error', payload)
        if isinstance(error, dict):
            code = str(error.get('code') or error.get('type') or 'chatgpt_upstream_error')
            message = str(error.get('message') or 'ChatGPT request failed')
            return message[:500], code
        return str(error)[:500], 'chatgpt_upstream_error'
    return 'ChatGPT request failed', 'chatgpt_upstream_error'


def _usage_limit_message(message: str) -> str:
    return f'{message} Manage ChatGPT plan usage at {USAGE_SETTINGS_URL}.'


async def _read_response_payload(response: aiohttp.ClientResponse):
    body = await response.text()
    if not body:
        return {}
    try:
        return JSONCodec.loads(body)
    except Exception:
        return body


async def _request_json(method: str, url: str, **kwargs):
    session = await get_session()
    response = await session.request(
        method=method,
        url=url,
        ssl=AIOHTTP_CLIENT_SESSION_SSL,
        timeout=get_client_timeout(),
        **kwargs,
    )
    try:
        payload = await _read_response_payload(response)
        if response.status >= 400:
            message, code = _safe_error_payload(payload)
            if code in USAGE_LIMIT_CODES:
                message = _usage_limit_message(message)
            raise ChatGPTError(message, status_code=response.status, code=code)
        return payload
    finally:
        await cleanup_response(response)


async def _get_openid_configuration() -> dict:
    metadata = await _request_json('GET', OPENID_CONFIGURATION_ENDPOINT)
    if not isinstance(metadata, dict) or metadata.get('issuer', '').rstrip('/') != ISSUER:
        raise ChatGPTError('OpenAI identity metadata could not be verified.', code='chatgpt_invalid_metadata')
    jwks_uri = metadata.get('jwks_uri')
    parsed_jwks_uri = urlparse(jwks_uri or '')
    if (
        parsed_jwks_uri.scheme != 'https'
        or parsed_jwks_uri.hostname != 'auth.openai.com'
        or not parsed_jwks_uri.path
    ):
        raise ChatGPTError('OpenAI identity metadata could not be verified.', code='chatgpt_invalid_metadata')
    return metadata


async def _get_host_id() -> str:
    key = 'openai.chatgpt.host_id'
    host_id = await Config.get(key)
    if host_id:
        return str(host_id)

    host_id = f'urn:uuid:{uuid4()}'
    await Config.upsert({key: host_id})
    return host_id


async def start_chatgpt_login(user_id: str) -> dict:
    redirect_uri = _validate_redirect_uri()
    host_id = await _get_host_id()
    verifier, challenge = _pkce_pair()
    nonce = secrets.token_urlsafe(32)

    existing_session = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, user_id)
    existing_token = existing_session.token if existing_session else {}
    client_id = existing_token.get('client_id') or DYNAMIC_CLIENT_ID

    pending = await AuthSessions.create(
        user_id=user_id,
        refresh_token_id=None,
        data={
            'kind': CHATGPT_PROVIDER,
            'code_verifier': verifier,
            'nonce': nonce,
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'host_id': host_id,
            'previous_subject': existing_token.get('subject'),
        },
        expires_at=int(time.time()) + PENDING_LOGIN_TTL,
    )

    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': OAUTH_SCOPES,
        'resource': OPENAI_RESOURCE,
        'state': pending.id,
        'nonce': nonce,
        'code_challenge_method': 'S256',
        'code_challenge': challenge,
        'ext_agent_host_id': host_id,
    }
    if client_id == DYNAMIC_CLIENT_ID:
        params['agent_name_hint'] = AGENT_NAME
    elif existing_token.get('email'):
        # Avoid sending the retained ID token through the browser; the email
        # hint is sufficient and keeps all stored bearer credentials backend-only.
        params['login_hint'] = existing_token['email']

    return {
        'authorization_url': f'{AUTHORIZATION_ENDPOINT}?{urlencode(params)}',
        'expires_at': pending.expires_at,
    }


async def _validate_id_token(id_token: str, client_id: str, nonce: str) -> dict:
    if not id_token:
        raise ChatGPTError('OpenAI did not return an ID token.', code='chatgpt_missing_id_token')

    metadata = await _get_openid_configuration()
    try:
        header = jwt.get_unverified_header(id_token)
        algorithm_name = header.get('alg')
        key_id = header.get('kid')
        if not algorithm_name or algorithm_name == 'none' or not key_id:
            raise ValueError('missing signing metadata')

        jwks = await _request_json('GET', metadata['jwks_uri'])
        keys = jwks.get('keys', []) if isinstance(jwks, dict) else []
        jwk = next((key for key in keys if key.get('kid') == key_id), None)
        if jwk is None:
            raise ValueError('signing key not found')

        algorithm = jwt.algorithms.get_default_algorithms().get(algorithm_name)
        if algorithm is None:
            raise ValueError('unsupported signing algorithm')
        signing_key = algorithm.from_jwk(JSONCodec.dumps(jwk))
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=[algorithm_name],
            audience=client_id,
            issuer=metadata['issuer'].rstrip('/'),
            options={'require': ['exp', 'iss', 'sub']},
        )
    except ChatGPTError:
        raise
    except Exception as exc:
        log.warning('ChatGPT ID token validation failed: %s', type(exc).__name__)
        raise ChatGPTError('The ChatGPT sign-in could not be verified.', code='chatgpt_invalid_id_token') from exc

    if claims.get('nonce') != nonce:
        raise ChatGPTError('The ChatGPT sign-in nonce did not match.', code='chatgpt_invalid_nonce')
    return claims


async def _exchange_code(client_id: str, code: str, verifier: str, redirect_uri: str) -> dict:
    session = await get_session()
    response = await session.post(
        TOKEN_ENDPOINT,
        data={
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'code': code,
            'code_verifier': verifier,
            'redirect_uri': redirect_uri,
            'resource': OPENAI_RESOURCE,
        },
        ssl=AIOHTTP_CLIENT_SESSION_SSL,
        timeout=get_client_timeout(),
    )
    try:
        payload = await _read_response_payload(response)
        if response.status >= 400:
            message, code_value = _safe_error_payload(payload)
            if code_value == 'invalid_grant':
                message = 'The ChatGPT sign-in expired. Start a new sign-in attempt.'
                code_value = 'chatgpt_invalid_grant'
            raise ChatGPTError(message, status_code=response.status, code=code_value)
        if not isinstance(payload, dict):
            raise ChatGPTError('OpenAI returned an invalid token response.', code='chatgpt_invalid_token_response')
        return payload
    finally:
        await cleanup_response(response)


async def complete_chatgpt_login(state: str, query) -> dict:
    if not state:
        raise ChatGPTError('The ChatGPT sign-in state is missing.', status_code=400, code='chatgpt_missing_state')

    pending = await AuthSessions.consume(state)
    if pending is None or pending.data.get('kind') != CHATGPT_PROVIDER:
        raise ChatGPTError(
            'The ChatGPT sign-in expired or was already used.',
            status_code=400,
            code='chatgpt_invalid_state',
        )

    error_code = query.get('error')
    if error_code:
        if error_code == 'access_denied':
            raise ChatGPTError(
                'ChatGPT plan usage was not enabled. You can authorize it by signing in again.',
                status_code=403,
                code='chatgpt_consent_denied',
            )
        raise ChatGPTError('ChatGPT sign-in was not completed.', status_code=400, code='chatgpt_authorization_failed')

    code = query.get('code')
    if not code:
        raise ChatGPTError('The ChatGPT authorization code is missing.', status_code=400, code='chatgpt_missing_code')

    callback_issuer = query.get('iss')
    if callback_issuer and callback_issuer.rstrip('/') != ISSUER:
        raise ChatGPTError('The ChatGPT sign-in issuer did not match.', code='chatgpt_issuer_mismatch')

    pending_data = pending.data
    selected_client_id = pending_data.get('client_id')
    callback_client_id = query.get('client_id')
    if selected_client_id == DYNAMIC_CLIENT_ID:
        if not callback_client_id or callback_client_id == DYNAMIC_CLIENT_ID:
            raise ChatGPTError('OpenAI did not return an issued client ID.', code='chatgpt_missing_client_id')
        client_id = callback_client_id
    else:
        if callback_client_id and callback_client_id != selected_client_id:
            raise ChatGPTError('The ChatGPT account registration did not match.', code='chatgpt_client_mismatch')
        client_id = selected_client_id

    token_response = await _exchange_code(
        client_id,
        code,
        pending_data['code_verifier'],
        pending_data['redirect_uri'],
    )
    issued_token_client_id = token_response.get('client_id')
    if issued_token_client_id and issued_token_client_id != client_id:
        raise ChatGPTError('The ChatGPT token registration did not match.', code='chatgpt_client_mismatch')
    claims = await _validate_id_token(token_response.get('id_token'), client_id, pending_data['nonce'])

    previous_subject = pending_data.get('previous_subject')
    if previous_subject and claims.get('sub') != previous_subject:
        raise ChatGPTError(
            'Choose the same ChatGPT account to reauthorize this connection.',
            code='chatgpt_account_mismatch',
        )

    # Only token-endpoint scopes are authoritative. The callback scope is not
    # sufficient to prove that the token grant includes plan usage.
    scopes = set(str(token_response.get('scope', '')).split())
    expires_in = int(token_response.get('expires_in') or 3600)
    token = {
        'access_token': token_response.get('access_token'),
        'refresh_token': token_response.get('refresh_token'),
        'id_token': token_response.get('id_token'),
        'token_type': token_response.get('token_type', 'Bearer'),
        'expires_at': int(time.time()) + expires_in,
        'client_id': client_id,
        'host_id': pending_data['host_id'],
        'issuer': claims.get('iss', ISSUER),
        'subject': claims.get('sub'),
        'email': claims.get('email'),
        'name': claims.get('name'),
        'scopes': sorted(scopes),
        'saved_at': int(time.time()),
    }
    if not token['access_token'] or not token['refresh_token']:
        raise ChatGPTError('OpenAI did not return renewable ChatGPT credentials.', code='chatgpt_missing_credentials')

    existing_session = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, pending.user_id)
    if existing_session:
        saved = await OAuthSessions.update_session_by_id(existing_session.id, token)
    else:
        saved = await OAuthSessions.create_session(pending.user_id, CHATGPT_PROVIDER, token)
    if saved is None:
        raise ChatGPTError('The ChatGPT credentials could not be stored securely.', code='chatgpt_storage_failed')

    return {
        'email': token.get('email'),
        'name': token.get('name'),
        'plan_usage_enabled': PLAN_USAGE_SCOPE in scopes,
    }


async def get_chatgpt_status(user_id: str) -> dict:
    session = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, user_id)
    if session is None:
        return {
            'connected': False,
            'plan_usage_enabled': False,
        }

    token = session.token
    scopes = set(token.get('scopes') or [])
    return {
        'connected': True,
        'plan_usage_enabled': PLAN_USAGE_SCOPE in scopes,
        'email': token.get('email'),
        'name': token.get('name'),
        'expires_at': token.get('expires_at'),
        'expired': int(token.get('expires_at') or 0) <= int(time.time()),
    }


async def _refresh_chatgpt_session(session) -> dict:
    token = session.token
    refresh_token = token.get('refresh_token')
    client_id = token.get('client_id')
    if not refresh_token or not client_id:
        raise ChatGPTError(
            'Reconnect ChatGPT to continue using the plan.',
            status_code=401,
            code='chatgpt_reauth_required',
        )

    session_client = await get_session()
    response = await session_client.post(
        TOKEN_ENDPOINT,
        data={
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'refresh_token': refresh_token,
            'resource': OPENAI_RESOURCE,
        },
        ssl=AIOHTTP_CLIENT_SESSION_SSL,
        timeout=get_client_timeout(),
    )
    try:
        payload = await _read_response_payload(response)
        if response.status >= 400:
            message, code = _safe_error_payload(payload)
            if code == 'invalid_grant':
                await OAuthSessions.delete_sessions_by_user_id_and_provider(session.user_id, CHATGPT_PROVIDER)
                raise ChatGPTError(
                    'Reconnect ChatGPT to continue using the plan.',
                    status_code=401,
                    code='chatgpt_reauth_required',
                )
            raise ChatGPTError(message, status_code=response.status, code=code)
    finally:
        await cleanup_response(response)

    if not isinstance(payload, dict) or not payload.get('access_token'):
        raise ChatGPTError('OpenAI returned an invalid refreshed credential.', code='chatgpt_invalid_refresh_response')

    updated = {
        **token,
        'access_token': payload['access_token'],
        'refresh_token': payload.get('refresh_token') or refresh_token,
        'token_type': payload.get('token_type', token.get('token_type', 'Bearer')),
        'expires_at': int(time.time()) + int(payload.get('expires_in') or 3600),
        'scopes': sorted(set(str(payload.get('scope', '')).split()) or set(token.get('scopes') or [])),
        'saved_at': int(time.time()),
    }
    if payload.get('id_token'):
        updated['id_token'] = payload['id_token']

    saved = await OAuthSessions.update_session_by_id(session.id, updated)
    if saved is None:
        raise ChatGPTError('The refreshed ChatGPT credential could not be stored.', code='chatgpt_storage_failed')
    return updated


async def get_chatgpt_access_token(user_id: str) -> str:
    session = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, user_id)
    if session is None:
        raise ChatGPTError(
            'Connect ChatGPT in Settings before selecting a ChatGPT plan model.',
            status_code=401,
            code='chatgpt_not_connected',
        )

    token = session.token
    scopes = set(token.get('scopes') or [])
    if PLAN_USAGE_SCOPE not in scopes:
        raise ChatGPTError(
            'ChatGPT plan usage is not enabled for this connection. Sign in again and approve plan usage.',
            status_code=403,
            code='chatgpt_plan_usage_disabled',
        )

    if token.get('access_token') and int(token.get('expires_at') or 0) > int(time.time()) + TOKEN_REFRESH_SKEW:
        return token['access_token']

    lock = _refresh_locks[session.id]
    async with lock:
        latest = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, user_id)
        if latest is None:
            raise ChatGPTError(
                'Reconnect ChatGPT to continue using the plan.',
                status_code=401,
                code='chatgpt_reauth_required',
            )
        if int(latest.token.get('expires_at') or 0) > int(time.time()) + TOKEN_REFRESH_SKEW:
            return latest.token['access_token']
        refreshed = await _refresh_chatgpt_session(latest)
        return refreshed['access_token']


async def revoke_chatgpt_session(user_id: str) -> dict:
    session = await OAuthSessions.get_session_by_provider_and_user_id(CHATGPT_PROVIDER, user_id)
    if session is None:
        return {'disconnected': True, 'remote_revocation_confirmed': True}

    remote_confirmed = False
    refresh_token = session.token.get('refresh_token')
    client_id = session.token.get('client_id')
    try:
        metadata = await _get_openid_configuration()
        endpoint = metadata.get('revocation_endpoint')
        if endpoint and refresh_token and client_id:
            response = await (await get_session()).post(
                endpoint,
                data={
                    'token': refresh_token,
                    'token_type_hint': 'refresh_token',
                    'client_id': client_id,
                },
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=get_client_timeout(),
            )
            try:
                remote_confirmed = response.status == 200
            finally:
                await cleanup_response(response)
    except Exception as exc:
        log.warning('ChatGPT remote revocation was not confirmed: %s', type(exc).__name__)

    await OAuthSessions.delete_sessions_by_user_id_and_provider(user_id, CHATGPT_PROVIDER)
    return {
        'disconnected': True,
        'remote_revocation_confirmed': remote_confirmed,
    }


async def list_chatgpt_models(user_id: str) -> list[dict]:
    try:
        access_token = await get_chatgpt_access_token(user_id)
    except ChatGPTError as exc:
        if exc.status_code in (401, 403):
            return []
        raise

    payload = await _request_json(
        'GET',
        OPENAI_MODELS_ENDPOINT,
        headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'},
    )
    raw_models = payload.get('models', []) if isinstance(payload, dict) else []
    result = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict) or raw_model.get('visibility') != 'list':
            continue
        slug = raw_model.get('slug')
        if not slug:
            continue
        result.append(
            {
                'id': f'{CHATGPT_MODEL_PREFIX}{slug}',
                'name': raw_model.get('display_name') or slug,
                'object': 'model',
                'created': 0,
                'owned_by': 'openai',
                'openai': {'id': slug},
                'chatgpt': {'id': slug},
                'provider': CHATGPT_MODEL_PROVIDER,
                'connection_type': 'external',
                'chatgpt_plan': True,
                'info': {'user_id': user_id, 'meta': {'chatgpt_plan': True}},
            }
        )
    return result


def _chatgpt_responses_payload(payload: dict, model: dict) -> dict:
    # Keep the existing OpenAI-compatible conversion in one place, but apply
    # the stricter ChatGPT-plan Responses allowlist afterward.
    from open_webui.routers.openai import convert_to_responses_payload

    model_data = model.get('chatgpt') or {}
    model_id = model_data.get('id') or model.get('id', '').removeprefix(CHATGPT_MODEL_PREFIX)
    request_payload = {**payload, 'model': model_id, 'stream': True, 'store': False}
    request_payload.pop('metadata', None)
    request_payload.pop('reasoning_effort', None)
    converted = convert_to_responses_payload(request_payload)

    allowed = {
        'model',
        'input',
        'instructions',
        'tools',
        'tool_choice',
        'text',
        'reasoning',
        'stream',
        'store',
    }
    converted = {key: value for key, value in converted.items() if key in allowed}
    if isinstance(converted.get('tools'), list):
        converted['tools'] = [
            tool for tool in converted['tools'] if isinstance(tool, dict) and tool.get('type') == 'function'
        ]
        if not converted['tools']:
            converted.pop('tools', None)
    converted['model'] = model_id
    converted['stream'] = True
    converted['store'] = False
    return converted


def _rewrite_stream_error(chunk):
    text = chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else chunk
    if not text.startswith('data:'):
        return chunk
    data_text = text.removeprefix('data:').strip()
    if data_text == '[DONE]':
        return chunk
    try:
        data = JSONCodec.loads(data_text)
    except Exception:
        return chunk
    error = data.get('error') if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get('code') in USAGE_LIMIT_CODES:
        error['message'] = _usage_limit_message(error.get('message') or 'ChatGPT plan usage is unavailable.')
        return f'data: {JSONCodec.dumps(data)}\n\n'
    return chunk


async def _chatgpt_stream(response):
    try:
        async for chunk in convert_responses_stream_to_chat_completions(stream_wrapper(response)):
            yield _rewrite_stream_error(chunk)
    except Exception:
        await cleanup_response(response)
        raise


async def _collect_chatgpt_response(response) -> dict:
    completed = None
    failed = None
    async for line in stream_wrapper(response):
        text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else line
        if not text.startswith('data:'):
            continue
        data_text = text.removeprefix('data:').strip()
        if data_text == '[DONE]':
            continue
        try:
            event = JSONCodec.loads(data_text)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get('type')
        if event_type == 'response.completed':
            completed = event.get('response')
        elif event_type == 'response.failed':
            failed = (event.get('response') or {}).get('error', {})
        elif event_type in ('error', 'response.incomplete'):
            failed = event.get('error') or (event.get('response') or {}).get('error', {})

    if failed:
        message, code = _safe_error_payload({'error': failed})
        if code in USAGE_LIMIT_CODES:
            message = _usage_limit_message(message)
        raise ChatGPTError(message, status_code=429 if code in USAGE_LIMIT_CODES else 502, code=code)
    if not isinstance(completed, dict):
        raise ChatGPTError('ChatGPT ended without a completed response.', code='chatgpt_incomplete_response')
    return completed


async def generate_chatgpt_completion(request, payload: dict, user, model: dict):
    from open_webui.routers.openai import convert_responses_result

    original_stream = bool(payload.get('stream', False))
    responses_payload = _chatgpt_responses_payload(payload, model)
    access_token = await get_chatgpt_access_token(user.id)
    session = await get_session()
    response = await session.post(
        OPENAI_RESPONSES_ENDPOINT,
        json=responses_payload,
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
        },
        ssl=AIOHTTP_CLIENT_SESSION_SSL,
        timeout=get_client_timeout(stream=True),
    )

    if response.status >= 400:
        payload_data = await _read_response_payload(response)
        await cleanup_response(response)
        message, code = _safe_error_payload(payload_data)
        if code in USAGE_LIMIT_CODES:
            message = _usage_limit_message(message)
        raise ChatGPTError(message, status_code=response.status, code=code)

    if original_stream:
        return StreamingResponse(_chatgpt_stream(response), media_type='text/event-stream')

    completed = await _collect_chatgpt_response(response)
    return convert_responses_result(completed)


def chatgpt_callback_html(success: bool, message: str) -> HTMLResponse:
    title = 'ChatGPT connected' if success else 'ChatGPT sign-in'
    body = html.escape(message)
    script = (
        "if (window.opener) window.opener.postMessage({source:'open-webui-chatgpt',success:"
        f"{'true' if success else 'false'}"
        "}, '*');"
        + ('window.close();' if success else '')
    )
    return HTMLResponse(
        f'<!doctype html><html><head><title>{title}</title></head>'
        f'<body style="font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem">'
        f'<h1>{html.escape(title)}</h1><p>{body}</p>'
        f'<script>{script}</script></body></html>'
    )
