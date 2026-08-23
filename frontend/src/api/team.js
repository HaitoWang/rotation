import http from './request'

export const listTeamMothers = () => http.get('/api/team/mothers')
export const createTeamMother = (payload) => http.post('/api/team/mothers', payload)
export const updateTeamMother = (id, payload) => http.put(`/api/team/mothers/${id}`, payload)
export const deleteTeamMother = (id) => http.delete(`/api/team/mothers/${id}`)
export const getTeamMotherDetail = (id) => http.get(`/api/team/mothers/${id}/detail`)
export const removeTeamMember = (motherId, memberId) =>
  http.delete(`/api/team/mothers/${motherId}/members/${memberId}`)

export const getTeamRotationStatus = () => http.get('/api/team/rotation/status')
export const startTeamRotation = (payload) => http.post('/api/team/rotation/start', payload)
export const pauseTeamRotation = () => http.post('/api/team/rotation/pause')
export const resumeTeamRotation = () => http.post('/api/team/rotation/resume')
export const stopTeamRotation = () => http.post('/api/team/rotation/stop')
export const checkTeamRotationNow = () => http.post('/api/team/rotation/check-now')
