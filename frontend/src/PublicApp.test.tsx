import { fireEvent, render, screen } from '@testing-library/react';
import type { Session } from '@supabase/supabase-js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PublicMeetings } from './PublicApp';

const session = { access_token: 'test-session-token' } as Session;

describe('PublicMeetings', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('requires consent before live microphone capture', () => {
        render(<PublicMeetings session={session} gateway="https://gateway.test" />);
        expect(screen.getByRole('button', { name: 'Start recording' })).toBeDisabled();
        expect(screen.queryByLabelText('Recording')).not.toBeInTheDocument();
    });

    it('enables capture only after consent and keeps BYOK fields session-scoped', () => {
        render(<PublicMeetings session={session} gateway="https://gateway.test" />);
        fireEvent.click(screen.getByRole('checkbox'));
        expect(screen.getByRole('button', { name: 'Start recording' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Use my API key' }));
        expect(screen.getByLabelText('Transcription provider API key')).toHaveAttribute('type', 'password');
        expect(screen.getByText('Your key is sent only with this request and is never stored. The initial adapter supports allowlisted OpenAI-compatible transcription providers.')).toBeInTheDocument();
    });
});
