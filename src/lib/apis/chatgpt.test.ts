import { beforeEach, describe, expect, it, vi } from 'vitest';
import { disconnectChatGPT, getChatGPTStatus, startChatGPTLogin } from './chatgpt';

describe('ChatGPT provider API', () => {
	beforeEach(() => {
		vi.restoreAllMocks();
	});

	it('uses backend status and never receives a credential payload', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ connected: true, plan_usage_enabled: true, email: 'user@example.com' })
		});
		vi.stubGlobal('fetch', fetchMock);

		const status = await getChatGPTStatus('webui-token');

		expect(status).toMatchObject({ connected: true, plan_usage_enabled: true });
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/v1/chatgpt/status',
			expect.objectContaining({ headers: expect.objectContaining({ authorization: 'Bearer webui-token' }) })
		);
		expect(JSON.stringify(fetchMock.mock.calls)).not.toContain('access_token');
		expect(JSON.stringify(fetchMock.mock.calls)).not.toContain('refresh_token');
	});

	it('starts and disconnects the backend session without browser-side tokens', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce({
				ok: true,
				json: async () => ({ authorization_url: 'https://auth.openai.com/example', expires_at: 1 })
			})
			.mockResolvedValueOnce({ ok: true, json: async () => ({ disconnected: true }) });
		vi.stubGlobal('fetch', fetchMock);

		const start = await startChatGPTLogin('webui-token');
		const disconnect = await disconnectChatGPT('webui-token');

		expect(start.authorization_url).toContain('auth.openai.com');
		expect(disconnect).toEqual({ disconnected: true });
		expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
			'/api/v1/chatgpt/start',
			'/api/v1/chatgpt/disconnect'
		]);
	});
});
