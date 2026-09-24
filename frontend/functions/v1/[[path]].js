// Keep browser requests on the Pages origin. The upstream is configured only
// in Cloudflare, while the gateway remains responsible for authentication.
export async function onRequest({ request, env }) {
    const origin = env.SAKSHAM_GATEWAY_ORIGIN;
    let upstream;
    try {
        upstream = new URL(origin);
        if (upstream.protocol !== 'https:' || upstream.username || upstream.password || upstream.search || upstream.hash || upstream.pathname !== '/') throw new Error('Invalid gateway origin');
    } catch {
        return new Response('Gateway is not configured', { status: 503 });
    }

    const incoming = new URL(request.url);
    upstream.pathname = incoming.pathname;
    upstream.search = incoming.search;
    const headers = new Headers(request.headers);
    headers.delete('cookie');
    headers.delete('host');
    headers.delete('x-forwarded-for');
    headers.delete('x-real-ip');
    // The free ngrok browser interstitial is not an API response. This header
    // bypasses only that notice; gateway authentication remains mandatory.
    headers.delete('ngrok-skip-browser-warning');
    if (upstream.hostname.endsWith('.ngrok-free.dev') || upstream.hostname.endsWith('.ngrok-free.app')) {
        headers.set('ngrok-skip-browser-warning', '1');
    }

    try {
        const response = await fetch(new Request(upstream, { method: request.method, headers, body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body, redirect: 'manual' }));
        if (response.status >= 300 && response.status < 400) return new Response('Gateway redirect refused', { status: 502 });
        const responseHeaders = new Headers(response.headers);
        responseHeaders.delete('access-control-allow-origin');
        responseHeaders.delete('access-control-allow-credentials');
        responseHeaders.delete('set-cookie');
        responseHeaders.set('cache-control', 'no-store');
        return new Response(response.body, { status: response.status, headers: responseHeaders });
    } catch {
        return new Response('Gateway is temporarily unavailable', { status: 502 });
    }
}
