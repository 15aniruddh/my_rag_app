// All backend calls live here, so switching from the local uvicorn server to a
// Lambda Function URL is one env var (VITE_API_BASE) and no component changes.
const BASE = import.meta.env.VITE_API_BASE ?? ''

// Sent only when the backend has APP_ACCESS_KEY set. This gates casual access,
// not determined attackers: anything shipped to a browser is readable by the
// user. Real multi-user auth needs a login flow and server-side sessions.
const ACCESS_KEY = import.meta.env.VITE_ACCESS_KEY ?? ''
const authHeaders = ACCESS_KEY ? { 'X-Access-Key': ACCESS_KEY } : {}

async function unwrap(res) {
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // non-JSON error body; keep the status-based message
    }
    throw new Error(detail)
  }
  return res.json()
}

export const getLibrary = () => fetch(`${BASE}/api/library`, { headers: authHeaders }).then(unwrap)

export const askQuestion = (question) =>
  fetch(`${BASE}/api/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify({ question }),
  }).then(unwrap)

export function uploadPdf(file) {
  const form = new FormData()
  form.append('file', file)
  return fetch(`${BASE}/api/ingest`, { method: 'POST', body: form, headers: authHeaders }).then(unwrap)
}

// encodeURIComponent matters: filenames routinely contain spaces.
export const deleteDocument = (source) =>
  fetch(`${BASE}/api/documents/${encodeURIComponent(source)}`, {
    method: 'DELETE', headers: authHeaders,
  }).then(unwrap)

export const clearLibrary = () =>
  fetch(`${BASE}/api/documents`, { method: 'DELETE', headers: authHeaders }).then(unwrap)
