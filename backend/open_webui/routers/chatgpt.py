"""User-facing Sign in with ChatGPT endpoints."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from open_webui.utils.auth import get_verified_user
from open_webui.utils.chatgpt import (
    ChatGPTError,
    chatgpt_callback_html,
    complete_chatgpt_login,
    get_chatgpt_status,
    revoke_chatgpt_session,
    start_chatgpt_login,
)

router = APIRouter()


@router.post('/start')
async def start(user=Depends(get_verified_user)):
    return await start_chatgpt_login(user.id)


@router.get('/callback', response_class=HTMLResponse)
async def callback(request: Request):
    try:
        result = await complete_chatgpt_login(request.query_params.get('state'), request.query_params)
        account = result.get('email') or result.get('name') or 'your ChatGPT account'
        if result.get('plan_usage_enabled'):
            return chatgpt_callback_html(True, f'Connected as {account}. You can close this window.')
        return chatgpt_callback_html(
            False,
            'The account connected, but ChatGPT plan usage was not enabled. '
            'Return to Open WebUI and sign in again to approve it.',
        )
    except ChatGPTError as exc:
        return chatgpt_callback_html(False, exc.message)


@router.get('/status')
async def status(user=Depends(get_verified_user)):
    return await get_chatgpt_status(user.id)


@router.post('/disconnect')
async def disconnect(user=Depends(get_verified_user)):
    return await revoke_chatgpt_session(user.id)
