import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test, afterEach } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/routes/listen/trackClient.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { createTrackClient, normalizeYoutubeLink } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })
const client = createTrackClient('http://backend', () => 'login-token')

test('accepts supported YouTube links and rejects unrelated or ambiguous URLs', () => {
  for (const url of ['https://youtu.be/BaW_jenozKc?t=5', 'https://music.youtube.com/watch?v=BaW_jenozKc',
    'https://www.youtube.com/shorts/BaW_jenozKc']) {
    assert.equal(normalizeYoutubeLink(url), 'https://www.youtube.com/watch?v=BaW_jenozKc')
  }
  for (const url of ['http://youtu.be/BaW_jenozKc', 'https://youtube.com.evil.test/watch?v=BaW_jenozKc',
    'https://youtube.com/playlist?list=abc', 'https://youtu.be/BaW_jenozKc/extra',
    'https://youtube.com/watch?v=BaW_jenozKc&v=other', '노래 제목']) assert.equal(normalizeYoutubeLink(url), null)
})

test('uses owner authentication and preserves retry key for repeated submissions', async () => {
  let calls = 0
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, 'http://backend/api/stations/st_test/tracks')
    assert.equal(options.headers.Authorization, 'Bearer login-token')
    assert.equal(options.headers['Idempotency-Key'], 'same-key')
    assert.equal(options.method, 'POST')
    assert.deepEqual(JSON.parse(options.body), { url: 'https://youtu.be/BaW_jenozKc', source: 'request' })
    return Response.json({ track: { title: 'test song' } }, { status: 202 })
  }
  for (let i = 0; i < 2; i++) assert.equal((await client.enqueue('st_test', 'https://youtu.be/BaW_jenozKc', 'same-key')).track.title, 'test song')
  assert.equal(calls, 2)
})

test('rejects anonymous calls before network access', async () => {
  globalThis.fetch = () => assert.fail('must not send anonymous request')
  await assert.rejects(createTrackClient('', () => null).checkStation('st_test'), /로그인/)
})

test('checks station readiness and reports owner and media errors without raw server details', async () => {
  globalThis.fetch = async () => Response.json({ station: { status: 'preparing' } })
  assert.equal(await client.checkStation('st_test'), false)
  globalThis.fetch = async () => Response.json({ station: { status: 'started' } })
  assert.equal(await client.checkStation('st_test'), true)
  for (const [status, message] of [[403, /방송을 만든 계정/], [404, /내 방송/], [502, /다운로드/], [504, /같은 링크/]]) {
    globalThis.fetch = async () => Response.json({ detail: 'private backend detail' }, { status })
    await assert.rejects(client.enqueue('st_test', 'url', 'key'), message)
  }
})
