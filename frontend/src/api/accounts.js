import http from './request'

// ──────────────── 统计 ────────────────
export const getStats = () => http.get('/api/v1/accounts/stats')

// ──────────────── 号池 accounts ────────────────
// kind = 邮箱来源（outlook / ...）。留空后端会按段数猜，
// 但 Outlook 和 Gmail 都是 4 段猜不出来，所以页面上必选。
export const importAccounts = (text, kind = '') =>
  http.post('/api/v1/accounts/import/text', { text, kind })

export const listAccounts = (params) =>
  http.get('/api/v1/accounts', { params }) // { status, limit, offset, kind }

export const deleteAccount = (email) =>
  http.delete(`/api/v1/accounts/${encodeURIComponent(email)}`)

export const bulkDeleteAccounts = (payload) =>
  http.post('/api/v1/accounts/bulk-delete', payload) // { status } 或 { emails }

export const resetFailed = () => http.post('/api/v1/accounts/reset-failed')

export const resetAccount = (email) =>
  http.post(`/api/v1/accounts/${encodeURIComponent(email)}/reset`)

export const bulkResetAccounts = (emails) =>
  http.post('/api/v1/accounts/bulk-reset', { emails })

export const releaseStale = () => http.post('/api/v1/accounts/release-stale')
