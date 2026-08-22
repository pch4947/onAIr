const DEFAULT_BUFFER_TARGET_SECONDS = Number(process.env.STREAM_BUFFER_TARGET_SECONDS ?? 45);
const DEFAULT_RESPONSE_WINDOW_SECONDS = Number(process.env.REQUEST_RESPONSE_WINDOW_SECONDS ?? 90);

export function decideNextSegment(input) {
  const bufferSeconds = numberOrZero(input.bufferSeconds);
  const estimatedGenerationSeconds = Math.max(1, numberOrZero(input.estimatedGenerationSeconds));
  const oldestRequestAgeSeconds = Math.max(0, numberOrZero(input.oldestRequestAgeSeconds));
  const pendingRequestCount = Math.max(0, Number(input.pendingRequestCount ?? 0));
  const bufferTargetSeconds = Math.max(1, Number(input.bufferTargetSeconds ?? DEFAULT_BUFFER_TARGET_SECONDS));
  const responseWindowSeconds = Math.max(1, Number(input.responseWindowSeconds ?? DEFAULT_RESPONSE_WINDOW_SECONDS));

  const underrunRisk = estimatedGenerationSeconds / Math.max(1, bufferSeconds);
  const bufferDeficit = Math.max(0, bufferTargetSeconds - bufferSeconds) / bufferTargetSeconds;
  const requestPressure = pendingRequestCount === 0
    ? 0
    : Math.min(1, oldestRequestAgeSeconds / responseWindowSeconds);

  if (pendingRequestCount > 0 && requestPressure >= 0.85 && underrunRisk < 0.9) {
    return decision('serve_request', 'oldest request is close to the response target and buffer risk is acceptable', {
      priority: score(requestPressure + (1 - underrunRisk)),
      underrunRisk,
      bufferDeficit,
      requestPressure
    });
  }

  if (bufferSeconds < estimatedGenerationSeconds || bufferDeficit > requestPressure) {
    return decision('build_buffer', 'continuity buffer needs protection before reacting to more requests', {
      priority: score(bufferDeficit + underrunRisk),
      underrunRisk,
      bufferDeficit,
      requestPressure
    });
  }

  if (pendingRequestCount > 0) {
    return decision('serve_request', 'buffer is stable enough to respond to the request queue', {
      priority: score(requestPressure + 0.5),
      underrunRisk,
      bufferDeficit,
      requestPressure
    });
  }

  return decision('idle', 'no pending requests and buffer is within the target range', {
    priority: 0,
    underrunRisk,
    bufferDeficit,
    requestPressure
  });
}

function decision(action, reason, metrics) {
  return {
    action,
    reason,
    metrics: {
      priority: metrics.priority,
      underrunRisk: score(metrics.underrunRisk),
      bufferDeficit: score(metrics.bufferDeficit),
      requestPressure: score(metrics.requestPressure)
    }
  };
}

function numberOrZero(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function score(value) {
  return Math.round(Math.max(0, Math.min(1, value)) * 100) / 100;
}
