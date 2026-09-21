import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { Session } from '@supabase/supabase-js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PublicMeetings } from './PublicApp';

const session = { access_token: 'test-session-token' } as Session;

describe('PublicMeetings', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('requires consent before an upload', () => {
        render(<PublicMeetings session={session} gateway="https://gateway.test" />);
        expect(screen.getByRole('button', { name: 'Transcribe recording' })).toBeDisabled();
    });

    it('sends a consented hosted upload through the gateway contract', async () => {
        const fetchMock = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ mode: 'hosted', language: 'en', model: 'test-model', segments: [{ start_ms: 0, end_ms: 1000, text: 'Hello team.' }] }),
        });
        vi.stubGlobal('fetch', fetchMock);
        render(<PublicMeetings session={session} gateway="https://gateway.test" />);

        const file = new File(['wav'], 'meeting.wav', { type: 'audio/wav' });
        fireEvent.change(screen.getByLabelText('Recording'), { target: { files: [file] } });
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: 'Transcribe recording' }));

        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        const [, request] = fetchMock.mock.calls[0] as [string, RequestInit];
        expect(request.headers).toMatchObject({
            Authorization: 'Bearer test-session-token',
            'X-Saksham-Consent-Confirmed': 'true',
        });
        expect(request.body).toBeInstanceOf(FormData);
        expect((request.body as FormData).get('mode')).toBe('hosted');
        expect(await screen.findByText('Hello team.')).toBeInTheDocument();
    });
});
