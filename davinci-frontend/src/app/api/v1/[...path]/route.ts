import { type NextRequest, NextResponse } from 'next/server';

const DJANGO = process.env.DJANGO_INTERNAL_URL ?? 'http://localhost:8001';

async function proxy(req: NextRequest): Promise<NextResponse> {
  // Build the Django URL — Next.js strips trailing slashes from pathname,
  // but Django's DRF router requires them; re-add unconditionally.
  const rawPath = req.nextUrl.pathname;
  const path = rawPath.endsWith('/') ? rawPath : rawPath + '/';
  const url = path + req.nextUrl.search;
  const djangoUrl = `${DJANGO}${url}`;

  const headers = new Headers();
  // Forward only safe headers — skip host, which would confuse Django
  const forward = ['authorization', 'content-type', 'accept', 'cookie'];
  forward.forEach((h) => {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  });

  let body: BodyInit | null = null;
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    body = await req.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(djangoUrl, {
      method: req.method,
      headers,
      body: body ?? undefined,
      // @ts-expect-error — Node.js fetch requires duplex for streaming bodies
      duplex: 'half',
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { detail: `Django unreachable at ${DJANGO}: ${message}` },
      { status: 502 },
    );
  }

  const resHeaders = new Headers(upstream.headers);
  // Remove hop-by-hop headers
  resHeaders.delete('transfer-encoding');

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: resHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
