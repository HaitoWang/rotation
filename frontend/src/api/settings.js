import http from './request'

// ──────────────── 邮箱来源配置 ────────────────
// 已注册的 provider 清单（含能力声明和配置项声明）。
// 页面据此动态渲染，后端加一种邮箱，这里和页面都不用改。
export const getMailProviders = (pooledOnly = false) =>
  http.get('/api/v1/settings/mail/providers', { params: { pooled_only: pooledOnly } })

export const getMailConfig = () => http.get('/api/v1/settings/mail')
export const saveMailConfig = (payload) => http.put('/api/v1/settings/mail', payload)
export const testMail = () => http.post('/api/v1/settings/mail/test')

// ──────────────── SMS 接码配置 ────────────────
export const getSmsConfig = () => http.get('/api/v1/settings/sms')
export const saveSmsConfig = (payload) => http.put('/api/v1/settings/sms', payload)
export const testSms = () => http.post('/api/v1/settings/sms/test')
export const getSmsTopCountries = () => http.get('/api/v1/settings/sms/countries')
export const getSmsAllCountries = (provider = '') =>
  http.get('/api/v1/settings/sms/all-countries', { params: { provider } })

// ──────────────── 自动导出配置 (CPA / SUB2API) ────────────────
export const getExportConfig = () => http.get('/api/v1/settings/export')
export const saveExportConfig = (payload) => http.put('/api/v1/settings/export', payload)
export const getSub2ApiGroups = (payload) => http.post('/api/v1/settings/export/sub2api/groups', payload)
export const testExport = (target) => http.post('/api/v1/settings/export/test', { target })
