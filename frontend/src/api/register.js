import http from './request'

// ──────────────── 单个注册 ────────────────
export const startRegister = (payload) => http.post('/api/v1/runs', payload)

// ──────────────── 运行记录 ────────────────
export const listRuns = (limit = 50) => http.get('/api/v1/runs', { params: { limit } })

// ──────────────── 账号池 registered ────────────────
export const listRegistered = (params) =>
  http.get('/api/v1/credentials', { params }) // { limit, offset }

export const getRegistered = (email) =>
  http.get(`/api/v1/credentials/${encodeURIComponent(email)}`)

export const deleteRegistered = (email) =>
  http.delete(`/api/v1/credentials/${encodeURIComponent(email)}`)

// 手填凭证：不传的字段后端不动，传空串才是清空
export const updateCredentials = (payload) =>
  http.patch(`/api/v1/credentials/${encodeURIComponent(payload.email)}`, payload)

export const reauthorizeRegistered = (email, proxy = '') =>
  http.post(
    `/api/v1/credentials/${encodeURIComponent(email)}/reauthorize`,
    { email, proxy },
    { timeout: 15 * 60 * 1000 },
  )

export const bulkReauthorizeRegistered = (emails, proxy = '', concurrency = 2) =>
  http.post(
    '/api/v1/credentials/bulk-reauthorize',
    { emails, proxy, concurrency },
    { timeout: 60 * 60 * 1000 },
  )

export const bulkDeleteRegistered = (payload) =>
  http.post('/api/v1/credentials/bulk-delete', payload) // { emails } 或 { all: true }

export const deleteBannedRegistered = () =>
  http.post('/api/v1/credentials/delete-banned', { confirm: true })

// 批量导出：格式清单由后端 export_formats.py 提供，加格式前端不用改
export const listExportFormats = () => http.get('/api/v1/exports/formats')
export const exportRegistered = (payload) => http.post('/api/v1/exports', payload)

export const checkPlus = (emails = [], proxy = '', all = false) =>
  http.post('/api/v1/credentials/check-plus', { emails, proxy, all })

export const exportToPanel = (email, targets) =>
  http.post('/api/v1/exports/panel', { email, targets })

// ──────────────── 自动跑号 auto-loop ────────────────
export const autoStart = (payload) => http.post('/api/v1/auto/start', payload)
export const autoPause = () => http.post('/api/v1/auto/pause')
export const autoResume = () => http.post('/api/v1/auto/resume')
export const autoStop = () => http.post('/api/v1/auto/stop')
export const autoStatus = () => http.get('/api/v1/auto/status')
