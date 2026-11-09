// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { Session } from '@supabase/supabase-js';
import PublicVoice from './PublicVoice';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const session = { access_token:'test', user:{id:'alice'} } as Session;

it('requires consent and never requests microphone permission on page load', async () => {
    const mic = vi.fn();
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>[]}));
    vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:mic}});
    render(<PublicVoice session={session} gateway="https://gateway.invalid" />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect((screen.getByRole('button',{name:'Start talking'}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox'));
    expect((screen.getByRole('button',{name:'Start talking'}) as HTMLButtonElement).disabled).toBe(false);
    expect(mic).not.toHaveBeenCalled();
});

it('shows storage failure instead of claiming history was loaded', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,json:async()=>({detail:'Voice is not enabled on this server yet'})}));
    render(<PublicVoice session={session} gateway="https://gateway.invalid" />);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent','Voice is not enabled on this server yet');
});

it('authenticates without URL credentials, flushes on pause, and releases the microphone', async () => {
    const stop = vi.fn();
    const stream = {getTracks:()=>[{stop}]};
    const close = vi.fn().mockResolvedValue(undefined);
    const disconnect = vi.fn();
    const node = () => ({connect:vi.fn(),disconnect});
    class Audio {
        currentTime=0; destination={};
        audioWorklet={addModule:vi.fn().mockResolvedValue(undefined)};
        resume=vi.fn().mockResolvedValue(undefined); close=close;
        createMediaStreamSource=node;
        createGain=()=>({...node(),gain:{value:0}});
    }
    class Worklet {
        static instance: Worklet;
        port: {onmessage?: (e: {data: unknown})=>void; postMessage: (data: string)=>void};
        disconnect=disconnect; connect=vi.fn();
        constructor() { Worklet.instance=this; this.port={postMessage:()=>this.port.onmessage?.({data:{type:'flushed'}})}; }
    }
    class Socket {
        static OPEN=1; static CLOSING=2; static instance: Socket;
        readyState=1; bufferedAmount=0; binaryType='';
        onopen?: ()=>void; onmessage?: (e:{data: string})=>void; onerror?: ()=>void; onclose?: ()=>void;
        send=vi.fn(); close=vi.fn();
        constructor(public url: string) { Socket.instance=this; }
    }
    vi.stubGlobal('AudioContext',Audio); vi.stubGlobal('AudioWorkletNode',Worklet); vi.stubGlobal('WebSocket',Socket);
    vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:vi.fn().mockResolvedValue(stream)}});
    vi.stubGlobal('fetch',vi.fn().mockImplementation((_url,options)=>Promise.resolve({ok:true,json:async()=>options?.method==='POST'?{id:'abc',title:'Voice'}:[]})));
    const view=render(<PublicVoice session={session} gateway="https://gateway.invalid" />);
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button',{name:'Start talking'}));
    await waitFor(()=>expect(Socket.instance).toBeDefined());
    const socket=Socket.instance;
    expect(String(socket.url)).toBe('wss://gateway.invalid/v1/voice/conversations/abc/live');
    socket.onopen?.();
    expect(JSON.parse(socket.send.mock.calls[0][0])).toEqual({type:'auth',token:'test',consent:true});
    const {act}=await import('@testing-library/react');
    act(()=>socket.onmessage?.({data:'{"type":"ready"}'}));
    fireEvent.click(screen.getByRole('button',{name:'Pause and save'}));
    expect(stop).toHaveBeenCalled();
    expect(socket.send).toHaveBeenCalledWith('{"type":"pause"}');
    act(()=>socket.onmessage?.({data:'{"type":"paused"}'}));
    expect(close).toHaveBeenCalled();
    expect((screen.getByRole('button',{name:'Resume conversation'}) as HTMLButtonElement).disabled).toBe(false);
    view.unmount();
});
