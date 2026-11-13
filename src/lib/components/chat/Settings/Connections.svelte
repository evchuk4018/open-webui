<script lang="ts">
	import { onDestroy, onMount, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	const i18n = getContext('i18n');

	import { models, settings } from '$lib/stores';
	import { getModels as getAllModels } from '$lib/apis';
	import {
		disconnectChatGPT,
		getChatGPTStatus,
		startChatGPTLogin,
		type ChatGPTStatus
	} from '$lib/apis/chatgpt';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Connection from './Connections/Connection.svelte';

	import AddConnectionModal from '$lib/components/AddConnectionModal.svelte';

	export let saveSettings: Function;

	let config = null;
	let chatgptStatus: ChatGPTStatus | null = null;
	let chatgptBusy = false;
	let chatgptAuthUrl = '';
	let chatgptPollTimer: ReturnType<typeof setInterval> | null = null;
	let chatgptPollDeadline = 0;

	let showConnectionModal = false;

	const refreshModels = async () => {
		await models.set(await getAllModels(localStorage.token, $settings?.directConnections ?? null, false, true));
	};

	const loadChatGPTStatus = async () => {
		try {
			chatgptStatus = await getChatGPTStatus(localStorage.token);
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	const stopChatGPTPolling = () => {
		if (chatgptPollTimer) {
			clearInterval(chatgptPollTimer);
			chatgptPollTimer = null;
		}
	};

	const pollChatGPTStatus = () => {
		stopChatGPTPolling();
		chatgptPollTimer = setInterval(async () => {
			if (Date.now() >= chatgptPollDeadline) {
				stopChatGPTPolling();
				chatgptBusy = false;
				toast.error('The ChatGPT sign-in window expired. Start again to retry.');
				return;
			}
			try {
				chatgptStatus = await getChatGPTStatus(localStorage.token);
				if (chatgptStatus.connected) {
					stopChatGPTPolling();
					chatgptBusy = false;
					chatgptAuthUrl = '';
					if (chatgptStatus.plan_usage_enabled) {
						await refreshModels();
					}
				}
			} catch (error) {
				stopChatGPTPolling();
				chatgptBusy = false;
				toast.error(`${error}`);
			}
		}, 1500);
	};

	const connectChatGPT = async () => {
		chatgptBusy = true;
		try {
			const result = await startChatGPTLogin(localStorage.token);
			chatgptAuthUrl = result.authorization_url;
			chatgptPollDeadline = result.expires_at * 1000;
			const popup = window.open(result.authorization_url, '_blank');
			if (!popup) {
				toast.info('Your browser blocked the sign-in window. Use the link shown below to continue.');
			}
			pollChatGPTStatus();
		} catch (error) {
			chatgptBusy = false;
			toast.error(`${error}`);
		}
	};

	const disconnectChatGPTConnection = async () => {
		chatgptBusy = true;
		try {
			await disconnectChatGPT(localStorage.token);
			chatgptStatus = { connected: false, plan_usage_enabled: false };
			chatgptAuthUrl = '';
			await refreshModels();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			chatgptBusy = false;
		}
	};

	const handleChatGPTMessage = async (event: MessageEvent) => {
		if (event.data?.source !== 'open-webui-chatgpt') return;
		stopChatGPTPolling();
		chatgptBusy = false;
		await loadChatGPTStatus();
		if (event.data.success && chatgptStatus?.plan_usage_enabled) {
			chatgptAuthUrl = '';
			await refreshModels();
		} else if (!event.data.success) {
			toast.error('ChatGPT sign-in was not completed.');
		}
	};

	const addConnectionHandler = async (connection) => {
		config.OPENAI_API_BASE_URLS.push(connection.url);
		config.OPENAI_API_KEYS.push(connection.key);
		config.OPENAI_API_CONFIGS[config.OPENAI_API_BASE_URLS.length - 1] = connection.config;

		await updateHandler();
	};

	const updateHandler = async () => {
		// Remove trailing slashes
		config.OPENAI_API_BASE_URLS = config.OPENAI_API_BASE_URLS.map((url) => url.replace(/\/$/, ''));

		// Check if API KEYS length is same than API URLS length
		if (config.OPENAI_API_KEYS.length !== config.OPENAI_API_BASE_URLS.length) {
			// if there are more keys than urls, remove the extra keys
			if (config.OPENAI_API_KEYS.length > config.OPENAI_API_BASE_URLS.length) {
				config.OPENAI_API_KEYS = config.OPENAI_API_KEYS.slice(
					0,
					config.OPENAI_API_BASE_URLS.length
				);
			}

			// if there are more urls than keys, add empty keys
			if (config.OPENAI_API_KEYS.length < config.OPENAI_API_BASE_URLS.length) {
				const diff = config.OPENAI_API_BASE_URLS.length - config.OPENAI_API_KEYS.length;
				for (let i = 0; i < diff; i++) {
					config.OPENAI_API_KEYS.push('');
				}
			}
		}

		await saveSettings({
			directConnections: config
		});
	};

	onMount(async () => {
		window.addEventListener('message', handleChatGPTMessage);
		config = $settings?.directConnections ?? {
			OPENAI_API_BASE_URLS: [],
			OPENAI_API_KEYS: [],
			OPENAI_API_CONFIGS: {}
		};
		await loadChatGPTStatus();
	});

	onDestroy(() => {
		stopChatGPTPolling();
		window.removeEventListener('message', handleChatGPTMessage);
	});
</script>

<AddConnectionModal direct bind:show={showConnectionModal} onSubmit={addConnectionHandler} />

<form
	id="tab-connections"
	class="flex flex-col h-full justify-between text-sm"
	on:submit|preventDefault={() => {
		updateHandler();
	}}
>
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">
		{$i18n.t('settings.personal.connections.title')}
	</h2>

	<div class="flex flex-1 min-h-0 flex-col overflow-y-auto scrollbar-hover pr-1.5">
		{#if config !== null}
			<section
				class="mb-6 space-y-3 rounded-xl border border-gray-100/70 bg-gray-50/60 p-3 dark:border-white/[0.06] dark:bg-white/[0.02]"
				aria-labelledby="chatgpt-plan-heading"
			>
				<div class="flex items-start justify-between gap-3">
					<div class="min-w-0">
						<h3 id="chatgpt-plan-heading" class="text-xs font-medium text-gray-800 dark:text-gray-200">
							ChatGPT plan
						</h3>
						<p class="mt-1 text-[0.6875rem] leading-relaxed text-gray-500 dark:text-gray-400">
							Sign in with ChatGPT to use eligible ChatGPT/Codex plan allowance. Your credentials stay on this server.
						</p>
					</div>
					{#if chatgptStatus?.connected && chatgptStatus.plan_usage_enabled && !chatgptStatus.expired}
						<span class="rounded-full bg-green-100 px-2 py-0.5 text-[0.625rem] text-green-700 dark:bg-green-900/30 dark:text-green-300">
							Connected
						</span>
					{:else if chatgptStatus?.connected && chatgptStatus.expired}
						<span class="rounded-full bg-amber-100 px-2 py-0.5 text-[0.625rem] text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
							Reconnect needed
						</span>
					{/if}
				</div>

				{#if chatgptStatus?.connected}
					<div class="rounded-lg bg-white/70 px-2.5 py-2 text-[0.6875rem] dark:bg-white/[0.04]">
						<div class="text-gray-700 dark:text-gray-200">
							{chatgptStatus.email ?? chatgptStatus.name ?? 'ChatGPT account'}
						</div>
						<div class="mt-1 text-gray-500 dark:text-gray-400">
							{#if chatgptStatus.expired}
								The ChatGPT session expired. Reconnect to continue using the plan.
							{:else if chatgptStatus.plan_usage_enabled}
								Using ChatGPT plan allowance.
							{:else}
								Connected, but plan usage permission is not enabled. Reconnect to approve it.
							{/if}
						</div>
					</div>
				{:else}
					<p class="text-[0.6875rem] leading-relaxed text-gray-500 dark:text-gray-400">
						For the homelab deployment, keep this tunnel open while signing in:
						<code class="mt-1 block break-all rounded bg-black/[0.04] px-2 py-1 text-[0.625rem] dark:bg-white/[0.05]"
							>ssh -N -L 1455:127.0.0.1:3001 evanh@homelab</code
						>
					</p>
				{/if}

				<div class="flex flex-wrap items-center gap-2">
					{#if !chatgptStatus?.connected || !chatgptStatus.plan_usage_enabled || chatgptStatus.expired}
						<button
							class="rounded-lg bg-gray-900 px-2.5 py-1.5 text-[0.6875rem] text-white transition hover:bg-black disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
							type="button"
							on:click={connectChatGPT}
							disabled={chatgptBusy}
						>
							{chatgptBusy ? 'Waiting for ChatGPT…' : 'Continue with ChatGPT'}
						</button>
					{/if}
					{#if chatgptStatus?.connected}
						<button
							class="rounded-lg border border-gray-200 px-2.5 py-1.5 text-[0.6875rem] text-gray-600 transition hover:bg-black/[0.03] disabled:opacity-50 dark:border-white/[0.08] dark:text-gray-300 dark:hover:bg-white/[0.04]"
							type="button"
							on:click={disconnectChatGPTConnection}
							disabled={chatgptBusy}
						>
							Disconnect
						</button>
					{/if}
					<a
						class="text-[0.6875rem] text-gray-500 underline hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
						href="https://chatgpt.com/settings/usage"
						target="_blank"
						rel="noreferrer"
					>
						Manage usage
					</a>
				</div>

				{#if chatgptAuthUrl}
					<a
						class="block break-all text-[0.625rem] text-gray-500 underline dark:text-gray-400"
						href={chatgptAuthUrl}
						target="_blank"
						rel="noreferrer"
					>
						Open the ChatGPT sign-in window
					</a>
				{/if}
			</section>

			<section class="space-y-3" aria-labelledby="direct-connections-heading">
				<div class="flex items-start justify-between gap-3">
					<div class="min-w-0">
						<h3 id="direct-connections-heading" class="text-xs text-gray-600 dark:text-gray-400">
							{$i18n.t('settings.personal.connections.sections.directConnections.title')}
						</h3>
						<p class="mt-1 text-[0.6875rem] leading-relaxed text-gray-400 dark:text-gray-600">
							{$i18n.t('settings.personal.connections.addConnection.description')}
						</p>
					</div>

					<Tooltip content={$i18n.t('settings.personal.connections.addConnection.label')}>
						<button
							class="flex size-7 shrink-0 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-black/5 hover:text-gray-900 dark:text-gray-600 dark:hover:bg-white/5 dark:hover:text-white"
							aria-label={$i18n.t('settings.personal.connections.addConnection.label')}
							on:click={() => {
								showConnectionModal = true;
							}}
							type="button"
						>
							<Plus />
						</button>
					</Tooltip>
				</div>

				<div class="flex flex-col gap-2">
					{#each config?.OPENAI_API_BASE_URLS ?? [] as url, idx}
						<Connection
							bind:url
							bind:key={config.OPENAI_API_KEYS[idx]}
							bind:config={config.OPENAI_API_CONFIGS[idx]}
							onSubmit={() => {
								updateHandler();
							}}
							onDelete={() => {
								config.OPENAI_API_BASE_URLS = config.OPENAI_API_BASE_URLS.filter(
									(url, urlIdx) => idx !== urlIdx
								);
								config.OPENAI_API_KEYS = config.OPENAI_API_KEYS.filter(
									(key, keyIdx) => idx !== keyIdx
								);

								let newConfig = {};
								config.OPENAI_API_BASE_URLS.forEach((url, newIdx) => {
									newConfig[newIdx] = config.OPENAI_API_CONFIGS[newIdx < idx ? newIdx : newIdx + 1];
								});
								config.OPENAI_API_CONFIGS = newConfig;
								updateHandler();
							}}
						/>
					{/each}
				</div>
			</section>

			<div class="mt-6">
				<div
					class="space-y-3 border-t border-gray-100/50 pt-4 text-[0.6875rem] leading-relaxed text-gray-400 dark:border-white/[0.04] dark:text-gray-600"
				>
					<p>
						{$i18n.t(
							'Connections managed through the Admin Panel are recommended for everyday use. Direct connections provide convenient access to your own endpoints, but rely on your browser session to keep requests running, making them better suited to testing and temporary use.'
						)}
					</p>
					<p>
						<!-- LICENSE covers this Open WebUI wordmark.
						Do not alter, remove, obscure, or replace it except as LICENSE permits:
						https://docs.openwebui.com/license. -->
						{$i18n.t(
							'CORS must be properly configured by the provider to allow requests from Open WebUI.'
						)}
					</p>
				</div>
			</div>
		{:else}
			<div class="flex h-full justify-center">
				<div class="my-auto">
					<Spinner className="size-6" />
				</div>
			</div>
		{/if}
	</div>

	<div class="shrink-0 flex justify-end pt-3 text-sm font-normal">
		<button
			class="flex h-7 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-gray-900 px-2.5 text-xs font-normal text-white transition hover:bg-black disabled:opacity-60 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
