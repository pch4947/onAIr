import { existsSync, statSync } from 'node:fs';
import { basename, extname, resolve, sep } from 'node:path';

const SEGMENT_ID_PATTERN = /^seg_[a-f0-9]{12}$/;
const SEGMENT_KINDS = new Set(['speech', 'music', 'ack', 'filler']);

export function parseSegmentSubmission(payload, audioRoot, receivedAt = new Date().toISOString()) {
  if (!isObject(payload)) return invalid('payload must be a JSON object');
  if (payload.contract_version !== 1) return invalid('contract_version must be 1');
  if (!SEGMENT_ID_PATTERN.test(payload.id ?? '')) return invalid('id must match seg_ followed by 12 hexadecimal characters');
  if (!isNonEmptyString(payload.station_id)) return invalid('station_id is required');
  if (!isSafeAudioReference(payload.audio_ref)) return invalid('audio_ref must be a relative POSIX path inside ONAIR_AUDIO_ROOT');
  if (!Number.isInteger(payload.duration_ms) || payload.duration_ms < 1) return invalid('duration_ms must be a positive integer');
  if (!isNonEmptyString(payload.corner_type)) return invalid('corner_type is required');
  if (!SEGMENT_KINDS.has(payload.kind)) return invalid('kind must be speech, music, ack, or filler');
  if (!Number.isInteger(payload.priority)) return invalid('priority must be an integer');
  if (typeof payload.reorderable !== 'boolean') return invalid('reorderable must be a boolean');
  if (!isIsoTimestamp(payload.created_at)) return invalid('created_at must be an ISO-8601 timestamp');
  if (!isOptionalString(payload.request_ref) || !isOptionalString(payload.music_ref)) {
    return invalid('request_ref and music_ref must be strings or null');
  }

  const audioPath = resolve(audioRoot, ...payload.audio_ref.split('/'));
  if (!isInside(audioRoot, audioPath)) return invalid('audio_ref must stay inside ONAIR_AUDIO_ROOT');
  if (extname(audioPath).toLowerCase() !== '.wav') return invalid('audio_ref must point to a .wav file');
  if (!existsSync(audioPath) || !statSync(audioPath).isFile()) return invalid('audio file does not exist');

  return {
    ok: true,
    value: {
      id: payload.id,
      stationId: payload.station_id,
      audioRef: payload.audio_ref,
      audioFileName: basename(audioPath),
      durationMs: payload.duration_ms,
      cornerType: payload.corner_type,
      kind: payload.kind,
      priority: payload.priority,
      reorderable: payload.reorderable,
      requestRef: payload.request_ref ?? null,
      musicRef: payload.music_ref ?? null,
      createdAt: payload.created_at,
      receivedAt
    }
  };
}

export function enqueueSegment(queue, segment) {
  const nextQueue = [...queue, segment];
  nextQueue.sort(compareSegments);
  return nextQueue;
}

export function getBufferSeconds(queue) {
  return Math.round(queue.reduce((total, segment) => total + segment.durationMs, 0) / 10) / 100;
}

function compareSegments(left, right) {
  if (left.priority !== right.priority) return left.priority - right.priority;
  return left.receivedAt.localeCompare(right.receivedAt);
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function isOptionalString(value) {
  return value === null || value === undefined || typeof value === 'string';
}

function isIsoTimestamp(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

function isSafeAudioReference(value) {
  return isNonEmptyString(value)
    && !value.includes('\\')
    && !value.startsWith('/')
    && !value.split('/').includes('..');
}

function isInside(root, target) {
  const resolvedRoot = resolve(root);
  return target === resolvedRoot || target.startsWith(`${resolvedRoot}${sep}`);
}

function invalid(error) {
  return { ok: false, error };
}
