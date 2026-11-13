import { WEBUI_API_BASE_URL } from '$lib/constants';

export type ChatGPTStatus = {
	connected: boolean;
	plan_usage_enabled: boolean;
	email?: string | null;
	name?: string | null;
	expires_at?: number | null;
	expired?: boolean;
};

const request = async (token: string, path: string, method = 'GET') => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/chatgpt${path}`, {
		method,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	});

	if (!response.ok) {
		const body = await response.json().catch(() => ({}));
		throw new Error(body?.detail?.error?.message ?? body?.detail ?? 'ChatGPT connection failed');
	}

	return response.json();
};

export const getChatGPTStatus = async (token: string): Promise<ChatGPTStatus> => {
	return request(token, '/status');
};

export const startChatGPTLogin = async (token: string): Promise<{ authorization_url: string; expires_at: number }> => {
	return request(token, '/start', 'POST');
};

export const disconnectChatGPT = async (token: string) => {
	return request(token, '/disconnect', 'POST');
};
