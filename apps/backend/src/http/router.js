import { decideNextSegment } from '../domain/scheduler.js';
import { enqueueSegment, getBufferSeconds, parseSegmentSubmission } from '../domain/segment-queue.js';
import { resolve } from 'node:path';

const audioRoot = resolve(process.env.ONAIR_AUDIO_ROOT ?? '../../var/onair-audio');

const state = {
  stream: {
    status: 'bootstrapping',
    bufferSeconds: 0,
    currentSegment: null,
    updatedAt: new Date().toISOString()
  },
  requests: [],
  segments: []
};

export async function route(request, response) {
  const url = new URL(request.url, `http://${request.headers.host}`);

  if (request.method === 'OPTIONS') {
    return sendNoContent(response);
  }

  if (request.method === 'GET' && url.pathname === '/health') {
    return sendJson(response, 200, { ok: true, service: 'onAIr backend' });
  }

  if (request.method === 'GET' && url.pathname === '/api/stream/state') {
    return sendJson(response, 200, {
      stream: state.stream,
      pendingRequestCount: state.requests.length,
      queuedSegmentCount: state.segments.length,
      oldestRequestAgeSeconds: getOldestRequestAgeSeconds()
    });
  }

  if (request.method === 'GET' && url.pathname === '/api/segments') {
    return sendJson(response, 200, {
      audioRoot,
      bufferSeconds: getBufferSeconds(state.segments),
      segments: state.segments
    });
  }

  if (request.method === 'POST' && url.pathname === '/api/segments') {
    const body = await readJson(request);
    if (!body) return sendJson(response, 400, { error: 'invalid JSON' });

    const parsed = parseSegmentSubmission(body, audioRoot);
    if (!parsed.ok) return sendJson(response, 400, { error: parsed.error });
    if (state.segments.some((segment) => segment.id === parsed.value.id)) {
      return sendJson(response, 409, { error: 'segment id already exists' });
    }

    state.segments = enqueueSegment(state.segments, parsed.value);
    updateStreamFromQueue();
    const position = state.segments.findIndex((segment) => segment.id === parsed.value.id) + 1;

    return sendJson(response, 201, {
      segment: parsed.value,
      position,
      bufferSeconds: state.stream.bufferSeconds
    });
  }

  if (request.method === 'GET' && url.pathname === '/api/requests') {
    return sendJson(response, 200, { requests: state.requests });
  }

  if (request.method === 'POST' && url.pathname === '/api/requests') {
    const body = await readJson(request);
    if (!body || !body.prompt || typeof body.prompt !== 'string') {
      return sendJson(response, 400, { error: 'prompt is required' });
    }

    const listenerRequest = {
      id: crypto.randomUUID(),
      listenerId: body.listenerId ?? 'anonymous',
      prompt: body.prompt,
      status: 'queued',
      createdAt: new Date().toISOString()
    };

    state.requests.push(listenerRequest);
    return sendJson(response, 202, { request: listenerRequest });
  }

  if (request.method === 'POST' && url.pathname === '/api/scheduler/tick') {
    const body = await readJson(request);
    if (!body) {
      return sendJson(response, 400, { error: 'invalid JSON' });
    }
    const bufferSeconds = Number(body.bufferSeconds ?? state.stream.bufferSeconds);
    const oldestRequestAgeSeconds = getOldestRequestAgeSeconds();
    const decision = decideNextSegment({
      bufferSeconds,
      estimatedGenerationSeconds: body.estimatedGenerationSeconds ?? 30,
      oldestRequestAgeSeconds,
      pendingRequestCount: state.requests.length,
      bufferTargetSeconds: body.bufferTargetSeconds,
      responseWindowSeconds: body.responseWindowSeconds
    });

    state.stream = {
      ...state.stream,
      status: decision.action === 'idle' ? 'ready' : 'planning',
      bufferSeconds,
      updatedAt: new Date().toISOString()
    };

    return sendJson(response, 200, {
      decision,
      stream: state.stream,
      pendingRequestCount: state.requests.length,
      oldestRequestAgeSeconds
    });
  }

  return sendJson(response, 404, { error: 'not found' });
}

function getOldestRequestAgeSeconds() {
  if (state.requests.length === 0) return 0;
  const oldest = state.requests[0];
  return Math.round((Date.now() - Date.parse(oldest.createdAt)) / 1000);
}

function updateStreamFromQueue() {
  const bufferSeconds = getBufferSeconds(state.segments);
  state.stream = {
    ...state.stream,
    status: state.segments.length > 0 ? 'buffered' : 'ready',
    bufferSeconds,
    updatedAt: new Date().toISOString()
  };
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Origin': '*',
    'Content-Type': 'application/json; charset=utf-8'
  });
  response.end(JSON.stringify(payload, null, 2));
}

function sendNoContent(response) {
  response.writeHead(204, {
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Origin': '*'
  });
  response.end();
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (chunks.length === 0) return {};

  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    return null;
  }
}
