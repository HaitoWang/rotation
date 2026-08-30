import http from './request'

export const listTeamMothers = () => http.get('/api/v1/team/mothers')
export const createTeamMother = (payload) => http.post('/api/v1/team/mothers', payload)
export const updateTeamMother = (id, payload) => http.patch(`/api/v1/team/mothers/${id}`, payload)
export const deleteTeamMother = (id) => http.delete(`/api/v1/team/mothers/${id}`)
export const getTeamMotherDetail = (id) => http.get(`/api/v1/team/mothers/${id}`)
export const triggerTeamMotherRotation = (motherId = '') =>
  http.post('/api/v1/team/check', null, motherId ? { params: { mother_id: motherId } } : undefined)
export const removeTeamMember = (motherId, memberId) =>
  http.delete(`/api/v1/team/members/${motherId}/${memberId}`)

export const getTeamRotationStatus = () => http.get('/api/v1/team/status')
export const startTeamRotation = (payload) => http.post('/api/v1/team/rotation/start', payload)
export const pauseTeamRotation = () => http.post('/api/v1/team/rotation/pause')
export const resumeTeamRotation = () => http.post('/api/v1/team/rotation/resume')
export const stopTeamRotation = () => http.post('/api/v1/team/rotation/stop')
export const checkTeamRotationNow = () => http.post('/api/v1/team/rotation/check-now')
