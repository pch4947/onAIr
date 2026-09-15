import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { enqueueSegment, getBufferSeconds, parseSegmentSubmission } from '../src/domain/segment-queue.js';

function createPayload(overrides = {}) {
  return {
    contract_version: 1,
    id: 'seg_123456789abc',
    station_id: 'st_local_dev',
    audio_ref: 'st_local_dev/seg_123456789abc.wav',
    duration_ms: 4590,
    corner_type: 'opening',
    kind: 'speech',
    priority: 50,
    reorderable: true,
    request_ref: null,
    music_ref: null,
    created_at: '2026-09-08T00:00:00.000Z',
    ...overrides
  };
}

function createAudioRoot() {
  const root = mkdtempSync(join(tmpdir(), 'onair-audio-'));
  mkdirSync(join(root, 'st_local_dev'));
  writeFileSync(join(root, 'st_local_dev', 'seg_123456789abc.wav'), 'wav');
  return root;
}

test('accepts a contract v1 segment stored in the shared audio root', () => {
  const parsed = parseSegmentSubmission(createPayload(), createAudioRoot());
  assert.equal(parsed.ok, true);
  assert.equal(parsed.value.durationMs, 4590);
});

test('rejects audio paths outside the shared audio root', () => {
  const parsed = parseSegmentSubmission(createPayload({ audio_ref: '../secret.wav' }), createAudioRoot());
  assert.equal(parsed.ok, false);
});

test('orders segments by ascending priority and totals the buffer duration', () => {
  const lowPriority = { id: 'seg_low', priority: 50, durationMs: 4000, receivedAt: '2026-09-08T00:00:01.000Z' };
  const highPriority = { id: 'seg_high', priority: 10, durationMs: 2500, receivedAt: '2026-09-08T00:00:02.000Z' };
  const queue = enqueueSegment([lowPriority], highPriority);

  assert.deepEqual(queue.map((segment) => segment.id), ['seg_high', 'seg_low']);
  assert.equal(getBufferSeconds(queue), 6.5);
});
