import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

// Execute the real component with controlled hook state and query responses.
const source = (await readFile(new URL('../src/routes/mypage/ReplayCollection.tsx', import.meta.url), 'utf8'))
  .replace('import.meta.env.VITE_API_BASE_URL', '"http://test"')
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
} }).outputText
const require = createRequire(import.meta.url)
function harness(initialPage, initialData, mode) {
  const state = [initialPage, null], mutations = [], invalidations = []
  let cursor = 0, effects = [], data = initialData
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (state[index] === undefined) state[index] = initial
      return [state[index], (value) => { state[index] = typeof value === 'function' ? value(state[index]) : value }]
    },
    useEffect(effect) { effects.push(effect) },
  }
  const cache = {
    invalidateQueries(options) { invalidations.push(options.queryKey); return Promise.resolve() },
    setQueryData(_, updater) { data = updater(data) },
  }
  const exports = {}
  vm.runInNewContext(code, { exports, require(name) {
    if (name === 'react') return hooks
    if (name === '@tanstack/react-query') return {
      useQueryClient: () => cache,
      useQuery: () => ({ data, isLoading: false }),
      useMutation: (options) => { mutations.push(options); return {} },
    }
    if (name === '@/shared/api') return { deleteReplay() {}, unbookmarkReplay() {} }
    if (name.includes('ReplayListItem')) return { ReplayListItem: () => null }
    return require(name)
  } })
  return { state, mutations, invalidations, render(nextData = data) {
    data = nextData; cursor = 0; effects = []; mutations.length = 0
    const tree = exports.ReplayCollection({ queryKey: mode === 'destroy' ? 'my-station-replays' : 'bookmarked-replays',
      fetcher() {}, emptyText: 'empty', deleteMode: mode })
    effects.forEach(effect => effect())
    return tree
  } }
}
for (const mode of ['destroy', 'unbookmark']) {
  test(`${mode}: deleting the last item on page 2 returns to page 1 and refreshes both collections`, () => {
    const h = harness(2, { replays: [{ id: 'last' }], total: 11 }, mode)
    h.render()
    h.mutations[mode === 'destroy' ? 1 : 0].onSuccess(undefined, { id: 'last' })
    assert.equal(h.state[0], 1)
    assert.deepEqual(h.invalidations.map(key => key[0]).sort(), ['bookmarked-replays', 'my-station-replays'])
  })
}
test('a refetch after external deletions clamps a now invalid page', () => {
  const h = harness(3, { replays: [], total: 10 }, 'destroy')
  h.render()
  assert.equal(h.state[0], 1)
})
test('deleting an item from a populated page keeps the current page', () => {
  const h = harness(2, { replays: [{ id: 'a' }, { id: 'b' }], total: 12 }, 'destroy')
  h.render(); h.mutations[1].onSuccess(undefined, { id: 'a' })
  assert.equal(h.state[0], 2)
})
