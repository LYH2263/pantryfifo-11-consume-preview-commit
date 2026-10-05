// Tiny cross-page signal: fired only after a consume ticket is *confirmed*,
// never after a preview, so shelf columns / layer pages / top bar stay still
// during preview and re-fetch together once quantities actually land.
const handlers = new Set()
export const bus = {
  on(fn) { handlers.add(fn); return () => handlers.delete(fn) },
  emit(payload) { handlers.forEach((fn) => fn(payload)) },
}
