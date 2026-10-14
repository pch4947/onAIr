import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test, afterEach } from 'node:test'
import ts from 'typescript'

// Load the actual typed client without adding a second frontend bundler/test framework.
const source = await readFile(new URL('../src/routes/station-create/client.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { createStationClient, StationApiError } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })
const client = createStationClient('http://backend', () => 'login-token')
const persona = { style: { formality: 'polite', energy: 'low', humor: 'rare' }, musicTaste: ['팝'],
  voice: 'dummy-feminine', djName: '새벽', concept: '스터디', tone: '차분하게',
  examples: ['하나', '둘', '셋'], signaturePhrases: [], forbidden: [] }

test('drafts use flat engine form, login token, and convert snake case', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'http://backend/api/personas/drafts')
    assert.equal(options.headers.Authorization, 'Bearer login-token')
    const body = JSON.parse(options.body)
    assert.equal(body.form.formality, 'polite')
    assert.equal(body.form.style, undefined)
    assert.equal(body.form.dj_name, null)
    assert.equal(body.count, 3)
    return Response.json({ drafts: [{ style: persona.style, music_taste: ['팝'], voice: persona.voice,
      dj_name: '새벽', concept: persona.concept, tone: persona.tone, examples: persona.examples,
      signature_phrases: [], forbidden: [] }] })
  }
  assert.deepEqual(await client.generatePersonaDrafts({ style: persona.style, voice: persona.voice, musicTaste: ['팝'] }), [persona])
})

test('both 422 shapes point to the editable field', async () => {
  for (const body of [{ errors: [{ field: 'persona.examples.1', reason: 'invalid' }] },
    { detail: [{ loc: ['body', 'persona', 'examples', 1], msg: 'invalid' }] }]) {
    globalThis.fetch = async () => Response.json(body, { status: 422 })
    assert.deepEqual(await client.checkPersonaDraft(persona), {
      ok: false, errors: [{ field: 'persona.examples.1', reason: 'invalid' }],
    })
  }
})

test('creation keeps caller retry key and returns preparing separately from ready', async () => {
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers['Idempotency-Key'], 'same-retry-key')
    const body = JSON.parse(options.body)
    assert.equal(body.broadcast_minutes, 30)
    assert.equal(body.persona.dj_name, '새벽')
    assert.equal(body.first_song, undefined)
    assert.equal(body.persona.persona_id, undefined)
    return Response.json({ station: { station_id: 'st_test', status: 'preparing', hls_ready: true } }, { status: 202 })
  }
  for (let i = 0; i < 2; i++) {
    assert.equal((await client.createStation({ persona, broadcastMinutes: 30 }, 'same-retry-key')).status, 'preparing')
  }
  await assert.rejects(client.createStation({ persona, broadcastMinutes: 30, firstSongUrl: 'https://example.com' }, 'key'))
})

test('missing login, service failure, and network failure are explicit', async () => {
  await assert.rejects(createStationClient('', () => null).getVoices(), (error) => error.status === 401)
  globalThis.fetch = async () => Response.json({ detail: 'internal secrets' }, { status: 503 })
  await assert.rejects(client.getVoices(), (error) => error instanceof StationApiError && error.status === 503 && !error.message.includes('secrets'))
  globalThis.fetch = async () => { throw new TypeError('offline') }
  await assert.rejects(client.getVoices(), (error) => error.status === 503)
})

test('owner stop uses authentication and the displayed broadcast creation time', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'http://backend/api/stations/st_test/stop')
    assert.equal(options.method, 'POST')
    assert.equal(options.headers.Authorization, 'Bearer login-token')
    assert.deepEqual(JSON.parse(options.body), { created_at: 123.456 })
    return Response.json({ station: { station_id: 'st_test', status: 'stopping' } }, { status: 202 })
  }
  assert.equal((await client.stopStation('st_test', 123.456)).status, 'stopping')
})
