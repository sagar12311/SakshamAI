import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@supabase/supabase-js';
import SavedChat from './SavedChat';

const session = { access_token: 'test', user: { id: 'alice' } } as Session;
const chat = { id: 'chat-1', title: 'Planning', pinned: true, project_id: null, mode: 'byok', provider_url: 'https://api.openai.com/v1', model: 'example' };

describe('saved conversations', () => {
    beforeEach(() => { sessionStorage.clear(); Element.prototype.scrollIntoView = vi.fn(); });
    afterEach(() => vi.unstubAllGlobals());

    it('restores a saved chat without restoring its provider key', async () => {
        sessionStorage.setItem('saksham.active.alice', 'chat-1');
        vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.endsWith('/projects') ? [] : url.includes('?offset') ? { items: [chat], next_offset: null } : { conversation: chat, turns: [{ id: 1, request_id: 'req', user_content: 'Plan my work', assistant_content: 'Here is a plan', status: 'completed' }], next_before: null, context_limited: true } })));
        render(<SavedChat session={session} gateway="https://gateway.test" />);
        expect(await screen.findByText('Here is a plan')).toBeInTheDocument();
        expect(screen.getByLabelText('API key · session only')).toHaveValue('');
        expect(screen.getByText(/latest 14 completed exchanges/)).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Unpin' })).toBeEnabled();
        expect(screen.queryByLabelText('Chat project')).not.toBeInTheDocument();
        expect(screen.getByLabelText('Chat provider')).toBeInTheDocument();
    });

    it('reuses the request and conversation IDs after a lost connection', async () => {
        const attempts: { url: string; body: { request_id: string; content: string } }[] = [];
        vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
            if (init.method === 'POST') { attempts.push({ url, body: JSON.parse(init.body as string) }); throw new Error('offline'); }
            return { ok: !url.includes('/conversations/'), json: async () => url.endsWith('/projects') ? [] : url.includes('?offset') ? { items: [], next_offset: null } : { detail: 'Conversation not found' } };
        }));
        render(<SavedChat session={session} gateway="https://gateway.test" />);
        fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'Hello' } });
        fireEvent.click(screen.getByRole('button', { name: 'Send' }));
        fireEvent.click(await screen.findByRole('button', { name: 'Retry same message' }));
        await waitFor(() => expect(attempts).toHaveLength(2));
        expect(attempts[0].url).toBe(attempts[1].url);
        expect(attempts[0].body.request_id).toBe(attempts[1].body.request_id);
        expect(attempts[1].body.content).toBe('Hello');
    });

    it('keeps workspace navigation available on the meetings page', async () => {
        vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.endsWith('/projects') ? [] : { items: [], next_offset: null } })));
        const change = vi.fn();
        render(<SavedChat session={session} gateway="https://gateway.test" workspace="meetings" onWorkspaceChange={change}><h2>Meeting content</h2></SavedChat>);
        expect(screen.getByText('Meeting content')).toBeVisible();
        expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Meeting Intelligence' })).toHaveAttribute('aria-current', 'page');
        fireEvent.click(screen.getByRole('button', { name: 'Chat', exact: true }));
        expect(change).toHaveBeenCalledWith('chat');
        await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    });
});
