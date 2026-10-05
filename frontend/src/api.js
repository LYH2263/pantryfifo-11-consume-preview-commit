export async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!r.ok) {
    let detail = r.statusText
    let body = null
    try { const j = await r.json(); body = j; detail = j.detail || JSON.stringify(j) } catch {}
    const msg = typeof detail === 'string' ? detail : JSON.stringify(detail)
    const err = new Error(msg)
    err.status = r.status
    err.body = body && typeof body.detail === 'object' ? body.detail : body
    throw err
  }
  if (r.status === 204) return null
  return r.json()
}
