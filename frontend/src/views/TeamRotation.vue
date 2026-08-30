<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Check, Close, Delete, Edit, Plus, Refresh, SwitchButton, VideoPause,
  VideoPlay, View,
} from '@element-plus/icons-vue'
import {
  checkTeamRotationNow,
  createTeamMother,
  deleteTeamMother,
  getTeamMotherDetail,
  getTeamRotationStatus,
  pauseTeamRotation,
  removeTeamMember,
  resumeTeamRotation,
  startTeamRotation,
  triggerTeamMotherRotation,
  stopTeamRotation,
  updateTeamMother,
} from '@/api/team'
import { fmtTime } from '@/api/request'

const loading = ref(false)
const actionLoading = ref('')
const status = ref({
  state: 'stopped',
  config: { interval_seconds: 300, quota_threshold: 100, quota_concurrency: 8, mother_concurrency: 10, join_concurrency: 4, hub_concurrency: 8, seat_cache_ttl: 300, member_refresh_interval: 900, operation_lease_seconds: 240, retry_max_seconds: 1800, proxy: '' },
  counts: {},
  mothers: [],
  members: [],
  events: [],
})
const config = reactive({ interval_seconds: 300, quota_threshold: 100, quota_concurrency: 8, mother_concurrency: 10, join_concurrency: 4, hub_concurrency: 8, seat_cache_ttl: 300, member_refresh_interval: 900, operation_lease_seconds: 240, retry_max_seconds: 1800, proxy: '' })
const configHydrated = ref(false)
let pollTimer = 0

const MOTHER_PAGE_SIZE = 8
const MEMBER_PAGE_SIZE = 10
const DETAIL_PAGE_SIZE = 8
const motherPage = ref(1)
const memberPage = ref(1)
const detailPage = ref(1)
const memberFilter = ref('all')
const activeView = ref('run')
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailMother = ref(null)
const detail = ref({ seats: {}, members: [] })

const stateMeta = computed(() => ({
  running: { label: '运行中', type: 'success' },
  paused: { label: '已暂停', type: 'warning' },
  stopped: { label: '已停止', type: 'info' },
}[status.value.state] || { label: status.value.state, type: 'info' }))

const memberFilterOptions = computed(() => [
  { value: 'all', label: '全部', count: status.value.members.length },
  { value: 'active', label: '使用中', count: status.value.members.filter((item) => item.status === 'active').length },
  { value: 'cooldown', label: '临时冷却', count: status.value.members.filter((item) => item.status === 'cooldown').length },
  { value: 'pending', label: '待加入', count: status.value.members.filter((item) => item.status === 'pending').length },
  { value: 'auth_required', label: '待重授权', count: status.value.members.filter((item) => item.status === 'auth_required').length },
  { value: 'exhausted', label: '已轮出', count: status.value.members.filter((item) => item.status === 'exhausted').length },
  { value: 'weekly_exhausted', label: '周额度耗尽', count: status.value.members.filter((item) => item.status === 'weekly_exhausted').length },
  { value: 'failed', label: '异常', count: status.value.members.filter((item) => item.status === 'failed').length },
].filter((item) => item.value === 'all' || item.count > 0))

const filteredMembers = computed(() => {
  if (memberFilter.value === 'all') return status.value.members
  return status.value.members.filter((item) => item.status === memberFilter.value)
})

const paginatedMothers = computed(() => {
  const start = (motherPage.value - 1) * MOTHER_PAGE_SIZE
  return status.value.mothers.slice(start, start + MOTHER_PAGE_SIZE)
})
const paginatedMembers = computed(() => {
  const start = (memberPage.value - 1) * MEMBER_PAGE_SIZE
  return filteredMembers.value.slice(start, start + MEMBER_PAGE_SIZE)
})
const paginatedDetailMembers = computed(() => {
  const start = (detailPage.value - 1) * DETAIL_PAGE_SIZE
  return detail.value.members.slice(start, start + DETAIL_PAGE_SIZE)
})
const motherTotalPages = computed(() => Math.max(1, Math.ceil(status.value.mothers.length / MOTHER_PAGE_SIZE)))
const memberTotalPages = computed(() => Math.max(1, Math.ceil(filteredMembers.value.length / MEMBER_PAGE_SIZE)))
const detailTotalPages = computed(() => Math.max(1, Math.ceil(detail.value.members.length / DETAIL_PAGE_SIZE)))
const MEMBER_STATE_META = {
  candidate: { label: '待补位', tone: 'info', symbol: '·' },
  joining: { label: '拉入中', tone: 'warning', symbol: '…' },
  hub_push: { label: '推送中', tone: 'warning', symbol: '…' },
  active: { label: '运行中', tone: 'success', symbol: '✓' },
  removing: { label: '移出中', tone: 'danger', symbol: '×' },
  awaiting_auth: { label: '待重授权', tone: 'warning', symbol: '!' },
  done: { label: '已完成', tone: 'info', symbol: '·' },
}
const ROTATION_STAGE_META = {
  queued: { label: '排队中', tone: 'warning' },
  running: { label: '运行中', tone: 'success' },
  idle: { label: '待机', tone: 'info' },
  error: { label: '异常', tone: 'danger' },
}
const stateMachineCounts = computed(() => {
  const members = status.value.members
  const count = (predicate) => members.filter(predicate).length
  return {
    candidate: count((item) => item.stage === 'candidate' || item.status === 'failed'),
    joining: count((item) => item.stage === 'joining'),
    hub_push: count((item) => item.stage === 'hub_push'),
    active: count((item) => item.status === 'active' && item.stage === 'active'),
    removing: count((item) => item.stage === 'removing'),
    awaiting_auth: count((item) => item.stage === 'awaiting_auth' || item.status === 'auth_required'),
    exhausted: count((item) => ['exhausted', 'weekly_exhausted', 'cooldown'].includes(item.status)),
  }
})
const executionRows = computed(() => status.value.members.slice().sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0)).slice(0, 6))
const activeMemberCount = computed(() => status.value.members.filter((item) => item.status === 'active').length || Number(status.value.counts.active || 0))
const activeMother = computed(() => status.value.mothers.find((item) => item.rotation_stage === 'running') || status.value.mothers.find((item) => item.rotation_stage === 'queued') || status.value.mothers[0] || null)
const activeMotherStage = computed(() => ROTATION_STAGE_META[activeMother.value?.rotation_stage] || ROTATION_STAGE_META.idle)
const rotationStep = computed(() => {
  if (status.value.state === 'stopped') return 0
  if (stateMachineCounts.value.removing || stateMachineCounts.value.exhausted) return 5
  if (stateMachineCounts.value.active) return 4
  if (stateMachineCounts.value.joining || stateMachineCounts.value.hub_push) return 3
  if (stateMachineCounts.value.candidate) return 1
  return activeMother.value?.rotation_stage === 'running' ? 2 : 0
})
const quotaProgress = computed(() => {
  const total = Math.max(1, status.value.mothers.length * 5)
  return Math.min(100, Math.round((activeMemberCount.value / total) * 100))
})
const radialNodes = computed(() => [
  { key: 'claim', label: '领取子号', detail: `${stateMachineCounts.value.candidate} 个候选`, icon: 'User', tone: 'green', position: 'north', count: stateMachineCounts.value.candidate },
  { key: 'match', label: '匹配母号', detail: `${status.value.mothers.length} 个母号`, icon: 'Connection', tone: 'green', position: 'east', count: status.value.mothers.length },
  { key: 'monitor', label: '额度监控', detail: `${activeMemberCount.value} 个使用中`, icon: 'Loading', tone: 'green', position: 'south-east', count: activeMemberCount.value },
  { key: 'join', label: '加入团队', detail: `${stateMachineCounts.value.joining + stateMachineCounts.value.hub_push} 个处理中`, icon: 'Files', tone: 'green', position: 'south-west', count: stateMachineCounts.value.joining },
  { key: 'refill', label: '满额补位', detail: `${stateMachineCounts.value.removing + stateMachineCounts.value.exhausted} 个待处理`, icon: 'Refresh', tone: 'blue', position: 'west', count: stateMachineCounts.value.removing + stateMachineCounts.value.exhausted },
])

watch(() => status.value.mothers.length, () => {
  if (motherPage.value > motherTotalPages.value) motherPage.value = motherTotalPages.value
})
watch(() => filteredMembers.value.length, () => {
  if (memberPage.value > memberTotalPages.value) memberPage.value = memberTotalPages.value
})
watch(memberFilter, () => { memberPage.value = 1 })
watch(() => detail.value.members.length, () => {
  if (detailPage.value > detailTotalPages.value) detailPage.value = detailTotalPages.value
})

function statusTag(value) {
  return {
    pending: ['待加入', 'warning'],
    auth_required: ['待人工重授权', 'warning'],
    active: ['使用中', 'success'],
    cooldown: ['临时冷却', 'warning'],
    exhausted: ['限流移出', 'danger'],
    weekly_exhausted: ['周额度耗尽', 'danger'],
    removed: ['已移出', 'info'],
    failed: ['加入失败', 'danger'],
  }[value] || [value || '-', 'info']
}

function hubStatusTag(value) {
  return {
    pushing: ['推送中', 'warning'],
    success: ['已推送', 'success'],
    failed: ['推送失败', 'danger'],
    paused: ['轮转暂停', 'warning'],
    pause_failed: ['暂停失败', 'danger'],
    disabled: ['Hub 未启用', 'info'],
    pending: ['待推送', 'warning'],
  }[value] || [value || '待推送', 'warning']
}

function stageLabel(value) {
  return {
    candidate: '待补位',
    joining: '拉入中',
    hub_push: '推送中',
    active: '运行中',
    removing: '移出中',
    awaiting_auth: '等待重授权',
    done: '已完成',
  }[value] || value || '-'
}

function memberStageMeta(row) {
  if (row?.stage && MEMBER_STATE_META[row.stage]) return MEMBER_STATE_META[row.stage]
  if (row?.status === 'auth_required') return MEMBER_STATE_META.awaiting_auth
  if (['exhausted', 'weekly_exhausted', 'cooldown'].includes(row?.status)) return { label: statusTag(row.status)[0], tone: row.status === 'cooldown' ? 'warning' : 'danger', symbol: '×' }
  return MEMBER_STATE_META.done
}

function joinModeLabel(value) {
  return value === 'auto_accept_request' ? '无需审核' : '主动邀请'
}

async function loadStatus(silent = false) {
  if (!silent) loading.value = true
  try {
    const data = await getTeamRotationStatus()
    status.value = {
      ...status.value,
      ...data,
      mothers: Array.isArray(data?.mothers) ? data.mothers : [],
      members: Array.isArray(data?.members) ? data.members : [],
      events: Array.isArray(data?.events) ? data.events : [],
      counts: data?.counts || {},
    }
    if (!configHydrated.value) {
      Object.assign(config, data.config || {})
      configHydrated.value = true
    }
  } catch (error) {
    if (!silent) ElMessage.error(error.message)
  } finally {
    if (!silent) loading.value = false
  }
}

async function runAction(name, action, success) {
  actionLoading.value = name
  try {
    await action()
    ElMessage.success(success)
    await loadStatus(true)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    actionLoading.value = ''
  }
}

function start() {
  runAction('start', () => startTeamRotation({ ...config }), 'Team 轮转已启动')
}
function pause() {
  runAction('pause', pauseTeamRotation, '已请求暂停')
}
function resume() {
  runAction('resume', resumeTeamRotation, 'Team 轮转已恢复')
}
async function stop() {
  try {
    await ElMessageBox.confirm('停止后不会继续检查 Hub 状态或自动补位。', '停止 Team 轮转', {
      type: 'warning', confirmButtonText: '停止', cancelButtonText: '取消',
    })
  } catch { return }
  runAction('stop', stopTeamRotation, '已请求停止')
}
function checkNow() {
  runAction('check', checkTeamRotationNow, '已触发立即检查')
}

function toggleLive(enabled) {
  if (enabled && status.value.state === 'stopped') start()
  else if (!enabled && status.value.state === 'running') pause()
}

const motherDialog = ref(false)
const motherSaving = ref(false)
const editingId = ref('')
const motherForm = reactive({ name: '', workspace_id: '', session: '', enabled: true, join_mode: 'invite_accept', preferred_seat_type: 'standard' })

function openCreate() {
  editingId.value = ''
  Object.assign(motherForm, { name: '', workspace_id: '', session: '', enabled: true, join_mode: 'invite_accept', preferred_seat_type: 'standard' })
  motherDialog.value = true
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(motherForm, {
    name: row.name,
    workspace_id: row.workspace_id,
    session: '',
    enabled: row.enabled,
    join_mode: row.join_mode || 'invite_accept',
    preferred_seat_type: row.preferred_seat_type || 'standard',
  })
  motherDialog.value = true
}

async function saveMother() {
  if (!motherForm.name.trim()) {
    ElMessage.warning('请输入母号名称')
    return
  }
  if (!editingId.value && !motherForm.session.trim()) {
    ElMessage.warning('请输入母号 Session 或 Access Token')
    return
  }
  motherSaving.value = true
  try {
    const payload = {
      name: motherForm.name.trim(),
      workspace_id: motherForm.workspace_id.trim(),
      enabled: motherForm.enabled,
      join_mode: motherForm.preferred_seat_type === 'advanced' ? 'auto_accept_request' : motherForm.join_mode,
      preferred_seat_type: motherForm.preferred_seat_type,
    }
    if (motherForm.session.trim()) payload.session = motherForm.session.trim()
    if (editingId.value) await updateTeamMother(editingId.value, payload)
    else await createTeamMother(payload)
    motherDialog.value = false
    ElMessage.success(editingId.value ? '母号已更新' : '母号已添加')
    await loadStatus(true)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    motherSaving.value = false
  }
}

function setAdvancedSeat(enabled) {
  motherForm.preferred_seat_type = enabled ? 'advanced' : 'standard'
  if (enabled) motherForm.join_mode = 'auto_accept_request'
}

function seatPool(row, type) {
  const pools = row?.seat_capacity?.pools || row?.seat_capacity || {}
  const pool = pools[type]
  return pool && typeof pool === 'object' ? pool : {}
}

function seatTypeLabel(value) {
  const normalized = String(value || '').toLowerCase()
  if (['advanced', 'prolite', 'premium'].includes(normalized)) return '高级席位'
  if (['standard', 'default', 'regular'].includes(normalized)) return '普通席位'
  return '待识别'
}

async function toggleMother(row, enabled) {
  try {
    await updateTeamMother(row.id, { enabled })
    row.enabled = enabled
    ElMessage.success(enabled ? '母号已启用' : '母号已停用')
  } catch (error) {
    row.enabled = !enabled
    ElMessage.error(error.message)
  }
}

function triggerMotherRotation(row) {
  runAction(`rotate-mother-${row.id}`, () => triggerTeamMotherRotation(row.id), `${row.name} 轮转任务已提交`)
}

async function removeMother(row) {
  try {
    await ElMessageBox.confirm(`删除母号 ${row.name}？`, '删除母号', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
    await deleteTeamMother(row.id)
    ElMessage.success('母号已删除')
    await loadStatus(true)
  } catch (error) {
    if (error?.message) ElMessage.error(error.message)
  }
}

async function inspectMother(row) {
  detailVisible.value = true
  detailLoading.value = true
  detailMother.value = row
  detailPage.value = 1
  detail.value = { seats: {}, members: [] }
  try {
    const response = await getTeamMotherDetail(row.id)
    detail.value = { seats: response.seats || {}, members: response.members || [] }
    await loadStatus(true)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

async function kickMember(member) {
  try {
    await ElMessageBox.confirm(`将 ${member.email || member.name || member.id} 移出 Team？`, '移出成员', {
      type: 'warning', confirmButtonText: '移出', cancelButtonText: '取消',
    })
    await removeTeamMember(detailMother.value.id, member.id)
    ElMessage.success('成员已移出')
    await inspectMother(detailMother.value)
  } catch (error) {
    if (error?.message) ElMessage.error(error.message)
  }
}

onMounted(() => {
  loadStatus()
  pollTimer = window.setInterval(() => loadStatus(true), 5000)
})
onActivated(() => loadStatus(true))
onBeforeUnmount(() => window.clearInterval(pollTimer))
</script>

<template>
  <div class="page team-page" v-loading="loading">
    <div class="team-studio">
      <nav class="studio-tabs" aria-label="Team 轮转工作区视图">
        <button :class="{ active: activeView === 'run' }" @click="activeView = 'run'"><el-icon><VideoPlay /></el-icon>运行 <b>{{ status.members.length }}</b></button>
        <button :class="{ active: activeView === 'history' }" @click="activeView = 'history'"><el-icon><Document /></el-icon>踢拉历史 <b>{{ status.events.length }}</b></button>
        <button :class="{ active: activeView === 'mothers' }" @click="activeView = 'mothers'"><el-icon><Files /></el-icon>母号维护 <b>{{ status.mothers.length }}</b></button>
        <button :class="{ active: activeView === 'settings' }" @click="activeView = 'settings'"><el-icon><Connection /></el-icon>设置</button>
        <span />
        <div class="studio-live-controls">
          <el-switch :model-value="status.state === 'running'" active-text="Live" @change="toggleLive" />
          <el-button v-if="status.state !== 'stopped'" class="stop-button" :icon="SwitchButton" :loading="actionLoading === 'stop'" @click="stop">停止</el-button>
          <el-button class="sync-button" :icon="Refresh" :loading="actionLoading === 'check'" @click="checkNow">同步进度</el-button>
        </div>
      </nav>

      <div v-if="activeView === 'run'" class="team-run-layout">
        <section class="team-canvas">
          <div class="team-runbar">
            <span class="live-dot" :class="{ on: status.state === 'running' }" />
            <strong>定额炸弹{{ status.state === 'running' ? '运行中' : '已停止' }}</strong>
            <span>{{ activeMother?.name || '等待母号' }}</span>
            <span class="run-stage" :class="activeMotherStage.tone">{{ activeMotherStage.label }}</span>
            <i />
            <el-icon><Connection /></el-icon>
          </div>
          <div class="team-radial">
            <div class="radial-ring ring-outer" />
            <div class="radial-ring ring-inner" />
            <div class="radial-center-glow" />
            <article class="team-center-node">
              <span class="node-eyebrow">QUOTA BOMB</span>
              <h2>定额炸弹</h2>
              <p>第 {{ rotationStep }} / 5 步 · {{ activeMotherStage.label }}</p>
              <div class="center-stat-grid">
                <div><span>推送设置</span><strong>{{ status.current_mother || '未配置重点' }}</strong></div>
                <div><span>满额阈值</span><strong>{{ config.quota_threshold }}%</strong></div>
                <div><span>检查周期</span><strong>{{ config.interval_seconds }} 秒</strong></div>
                <div><span>自动补位</span><strong>{{ status.state === 'running' ? '已开启' : '已关闭' }}</strong></div>
              </div>
              <button class="adjust-button" @click="activeView = 'settings'"><el-icon><Connection /></el-icon>调整策略</button>
            </article>
            <article v-for="node in radialNodes" :key="node.key" class="radial-node" :class="[node.position, node.tone, { 'is-active': node.count > 0 }]">
              <span class="radial-node-icon"><el-icon :size="21"><component :is="node.icon" /></el-icon></span>
              <strong>{{ node.label }}</strong>
              <small>{{ node.detail }}</small>
            </article>
          </div>
        </section>

        <aside class="execution-rail">
          <header class="rail-header"><span>EXECUTION</span><div><h2>执行轨迹</h2><b>{{ executionRows.length }}</b></div></header>
          <div class="execution-list">
            <div v-for="row in executionRows" :key="row.id" class="execution-item">
              <span class="execution-mark" :class="memberStageMeta(row).tone">{{ memberStageMeta(row).symbol }}</span>
              <div><strong>{{ memberStageMeta(row).label }}</strong><small>{{ row.email || row.id }}</small></div>
              <span>{{ row.hub_status === 'success' ? 'Hub 已同步' : row.quota_status || stageLabel(row.stage) }}</span>
            </div>
            <el-empty v-if="!executionRows.length" :image-size="48" description="暂无执行轨迹" />
          </div>
          <section class="queue-section">
            <header><span>队列</span><div><h2>待拉入队列</h2><b>{{ status.counts.pending || 0 }}</b></div></header>
            <div v-if="status.counts.pending" class="queue-row"><span>{{ status.counts.pending }}</span><strong>等待中的子号</strong><small>优先级 10</small></div>
            <el-empty v-else :image-size="42" description="队列为空" />
          </section>
        </aside>
      </div>

      <div v-else-if="activeView === 'settings'" class="team-settings-view">
        <el-card class="control-panel">
          <template #header>
            <div class="panel-header"><div class="panel-title-group"><div class="title-line"><h2 class="section-title">轮转设置</h2><el-tag :type="stateMeta.type" effect="light">{{ stateMeta.label }}</el-tag></div><p class="section-subtitle">动态按母号调度</p></div><el-button :icon="Refresh" circle :loading="actionLoading === 'refresh'" @click="loadStatus()" /></div>
          </template>
          <div class="control-grid">
            <el-form-item class="control-field" label="轮转检查间隔（秒）" label-position="top"><el-input-number v-model="config.interval_seconds" :min="5" :max="86400" :step="5" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="状态并发数" label-position="top"><el-input-number v-model="config.quota_concurrency" :min="1" :max="32" :step="1" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="母号并发数" label-position="top"><el-input-number v-model="config.mother_concurrency" :min="1" :max="16" :step="1" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="单母号拉入并发数" label-position="top"><el-input-number v-model="config.join_concurrency" :min="1" :max="32" :step="1" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="单母号推送并发数" label-position="top"><el-input-number v-model="config.hub_concurrency" :min="1" :max="32" :step="1" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="席位缓存 TTL（秒）" label-position="top"><el-input-number v-model="config.seat_cache_ttl" :min="30" :max="86400" :step="30" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="成员复核间隔（秒）" label-position="top"><el-input-number v-model="config.member_refresh_interval" :min="60" :max="86400" :step="60" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="额度阈值（%）" label-position="top"><el-input-number v-model="config.quota_threshold" :min="1" :max="100" :step="1" controls-position="right" /></el-form-item>
            <el-form-item class="control-field" label="Team 请求代理" label-position="top"><el-input v-model="config.proxy" clearable placeholder="留空直连" /></el-form-item>
            <div class="control-actions"><el-button v-if="status.state === 'stopped'" type="primary" :icon="VideoPlay" :loading="actionLoading === 'start'" @click="start">启动</el-button><el-button v-if="status.state === 'running'" :icon="VideoPause" :loading="actionLoading === 'pause'" @click="pause">暂停</el-button><el-button v-if="status.state === 'paused'" type="primary" :icon="VideoPlay" :loading="actionLoading === 'resume'" @click="resume">恢复</el-button><el-button v-if="status.state === 'running'" :icon="Refresh" :loading="actionLoading === 'check'" @click="checkNow">立即检查</el-button><el-button v-if="status.state !== 'stopped'" type="danger" plain :icon="SwitchButton" :loading="actionLoading === 'stop'" @click="stop">停止</el-button></div>
          </div>
          <el-alert v-if="status.last_error" class="runtime-error" type="error" :closable="false" :title="status.last_error" />
        </el-card>
      </div>

      <div v-else-if="activeView === 'mothers'" class="team-list-view">
        <div class="stats-grid"><el-card class="stat-card"><div class="stat-value">{{ status.mothers.length }}</div><div class="stat-label">母号</div></el-card><el-card class="stat-card"><div class="stat-value">{{ status.counts.active || 0 }}</div><div class="stat-label">使用中子号</div></el-card><el-card class="stat-card"><div class="stat-value">{{ status.counts.exhausted || 0 }}</div><div class="stat-label">已轮出</div></el-card></div>
        <el-card class="table-panel"><template #header><div class="panel-header"><div class="panel-title-group"><div class="title-line"><h2 class="section-title">母号与席位</h2><span class="header-count">{{ status.mothers.length }} 个母号</span></div><p class="section-subtitle">管理轮转入口，查看席位占用和最近一次检查结果</p></div><el-button type="primary" :icon="Plus" @click="openCreate">添加母号</el-button></div></template>
          <el-table :data="paginatedMothers" stripe empty-text="暂无母号"><el-table-column label="母号" min-width="190"><template #default="{ row }"><div class="primary-cell"><strong>{{ row.name }}</strong><span>{{ row.email || '-' }}</span></div></template></el-table-column><el-table-column prop="workspace_id" label="Workspace ID" min-width="220" show-overflow-tooltip /><el-table-column label="加入方式" width="110"><template #default="{ row }"><el-tag :type="row.join_mode === 'auto_accept_request' ? 'warning' : 'info'" effect="light">{{ joinModeLabel(row.join_mode) }}</el-tag></template></el-table-column><el-table-column label="目标席位" width="105"><template #default="{ row }"><el-tag :type="row.preferred_seat_type === 'advanced' ? 'primary' : 'info'" effect="light">{{ row.preferred_seat_type === 'advanced' ? '高级' : '普通' }}</el-tag></template></el-table-column><el-table-column label="席位" width="210"><template #default="{ row }"><div v-if="row.seats_entitled !== null && row.seats_entitled !== undefined" class="seat-pools"><span>普通 {{ seatPool(row, 'standard').assigned || 0 }}/{{ seatPool(row, 'standard').paid || 0 }} · 余 {{ seatPool(row, 'standard').available || 0 }}</span><span :class="{ advanced: row.preferred_seat_type === 'advanced' }">高级 {{ seatPool(row, 'advanced').assigned || 0 }}/{{ seatPool(row, 'advanced').paid || 0 }} · 余 {{ seatPool(row, 'advanced').available || 0 }}</span></div><span v-else>-</span></template></el-table-column><el-table-column label="状态" width="105"><template #default="{ row }"><el-switch :model-value="row.enabled" @change="toggleMother(row, $event)" /></template></el-table-column><el-table-column label="最近检查" width="170"><template #default="{ row }"><span :class="{ 'error-text': row.last_error }">{{ row.last_error || fmtTime(row.last_checked_at) }}</span></template></el-table-column><el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-tooltip content="触发该母号轮转"><el-button text circle :disabled="status.state !== 'running'" :icon="Refresh" :loading="actionLoading === `rotate-mother-${row.id}`" @click="triggerMotherRotation(row)" /></el-tooltip><el-tooltip content="查看成员"><el-button text circle :icon="View" @click="inspectMother(row)" /></el-tooltip><el-tooltip content="编辑母号"><el-button text circle :icon="Edit" @click="openEdit(row)" /></el-tooltip><el-tooltip content="删除母号"><el-button text circle type="danger" :icon="Delete" @click="removeMother(row)" /></el-tooltip></template></el-table-column></el-table>
          <div v-if="status.mothers.length" class="table-footer"><span class="table-hint">显示 {{ (motherPage - 1) * MOTHER_PAGE_SIZE + 1 }}-{{ Math.min(motherPage * MOTHER_PAGE_SIZE, status.mothers.length) }} / 共 {{ status.mothers.length }} 个母号</span><el-pagination v-model:current-page="motherPage" :page-size="MOTHER_PAGE_SIZE" :total="status.mothers.length" layout="prev, pager, next" background size="small" /></div>
        </el-card>
      </div>

      <div v-else class="team-history-view">
        <el-card class="table-panel"><template #header><div class="panel-header member-header"><div class="panel-title-group"><div class="title-line"><h2 class="section-title">轮转账号</h2><span class="header-count">{{ filteredMembers.length }} 条记录</span></div><p class="section-subtitle">实时查看子号状态和 Hub 推送情况</p></div><el-radio-group v-model="memberFilter" size="small" class="member-filters"><el-radio-button v-for="item in memberFilterOptions" :key="item.value" :value="item.value">{{ item.label }} {{ item.count }}</el-radio-button></el-radio-group></div></template>
          <el-table :data="paginatedMembers" stripe empty-text="暂无轮转记录"><el-table-column prop="email" label="子号" min-width="220" show-overflow-tooltip /><el-table-column prop="mother_name" label="母号" min-width="140" show-overflow-tooltip /><el-table-column label="状态" width="105"><template #default="{ row }"><el-tag :type="statusTag(row.status)[1]" effect="light">{{ statusTag(row.status)[0] }}</el-tag></template></el-table-column><el-table-column label="执行阶段" width="110"><template #default="{ row }">{{ stageLabel(row.stage) }}</template></el-table-column><el-table-column label="Hub" width="120"><template #default="{ row }"><el-tooltip :disabled="!row.hub_error" :content="row.hub_error"><el-tag :type="hubStatusTag(row.hub_status)[1]" effect="light">{{ hubStatusTag(row.hub_status)[0] }}</el-tag></el-tooltip></template></el-table-column><el-table-column label="加入时间" width="170"><template #default="{ row }">{{ fmtTime(row.joined_at) }}</template></el-table-column><el-table-column label="最近检查" width="170"><template #default="{ row }">{{ fmtTime(row.last_checked_at) }}</template></el-table-column><el-table-column prop="error" label="状态说明" min-width="220" show-overflow-tooltip /></el-table>
          <div v-if="filteredMembers.length" class="table-footer"><span class="table-hint">显示 {{ (memberPage - 1) * MEMBER_PAGE_SIZE + 1 }}-{{ Math.min(memberPage * MEMBER_PAGE_SIZE, filteredMembers.length) }} / 共 {{ filteredMembers.length }} 条</span><el-pagination v-model:current-page="memberPage" :page-size="MEMBER_PAGE_SIZE" :total="filteredMembers.length" layout="prev, pager, next" background size="small" /></div>
        </el-card>
        <el-card><template #header><h2 class="section-title">轮转事件</h2></template><div class="event-log"><div v-for="event in status.events" :key="event.id" class="event-row" :class="event.level.toLowerCase()"><time>{{ fmtTime(event.created_at) }}</time><el-tag size="small" effect="plain">{{ event.action }}</el-tag><strong v-if="event.mother_name">{{ event.mother_name }}</strong><span v-if="event.email" class="mono">{{ event.email }}</span><span>{{ event.message }}</span></div><el-empty v-if="!status.events.length" :image-size="54" description="暂无事件" /></div></el-card>
      </div>
    </div>

    <el-dialog v-model="motherDialog" :title="editingId ? '编辑母号' : '添加母号'" width="620px" destroy-on-close>
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="名称"><el-input v-model="motherForm.name" maxlength="100" /></el-form-item>
          <el-form-item label="Workspace ID"><el-input v-model="motherForm.workspace_id" placeholder="可从 Session 自动识别" /></el-form-item>
        </div>
        <el-form-item :label="editingId ? 'Session / Access Token（留空不修改）' : 'Session / Access Token'">
          <el-input v-model="motherForm.session" type="textarea" :rows="7" resize="vertical" />
        </el-form-item>
        <el-form-item label="子号加入方式">
          <el-radio-group v-model="motherForm.join_mode" :disabled="motherForm.preferred_seat_type === 'advanced'">
            <el-radio-button value="invite_accept">主动邀请</el-radio-button>
            <el-radio-button value="auto_accept_request">无需审核</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="高级席位">
          <el-switch
            :model-value="motherForm.preferred_seat_type === 'advanced'"
            active-text="开启"
            inactive-text="关闭"
            @change="setAdvancedSeat"
          />
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="motherForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="motherDialog = false">取消</el-button>
        <el-button type="primary" :icon="Check" :loading="motherSaving" @click="saveMother">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="detailVisible" :title="`${detailMother?.name || ''} · Team 成员`" width="900px">
      <div class="detail-summary">
        <span>总席位 <strong>{{ detail.seats.entitled ?? '-' }}</strong></span>
        <span>已使用 <strong>{{ detail.seats.in_use ?? '-' }}</strong></span>
        <span>普通剩余 <strong>{{ detail.seats.remaining_standard ?? '-' }}</strong></span>
        <span>高级剩余 <strong>{{ detail.seats.remaining_advanced ?? '-' }}</strong></span>
      </div>
      <el-table v-loading="detailLoading" :data="paginatedDetailMembers" stripe max-height="520" empty-text="暂无成员">
        <el-table-column prop="email" label="邮箱" min-width="220" show-overflow-tooltip />
        <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
        <el-table-column prop="role" label="角色" width="130" />
        <el-table-column label="席位类型" width="120"><template #default="{ row }">{{ seatTypeLabel(row.seat_type) }}</template></el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="danger" :icon="Delete" :disabled="row.is_owner" @click="kickMember(row)">移出</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="detail.members.length" class="table-footer detail-footer">
        <span class="table-hint">共 {{ detail.members.length }} 位成员</span>
        <el-pagination v-model:current-page="detailPage" :page-size="DETAIL_PAGE_SIZE" :total="detail.members.length" layout="prev, pager, next" background size="small" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.team-page { width: 100%; height: calc(100vh - 74px); min-height: 560px; display: flex; flex-direction: column; }
.team-studio { min-height: 0; display: flex; flex: 1; flex-direction: column; overflow: hidden; border: 1px solid #dedfe4; border-radius: 10px; background: #fff; }
.studio-tabs { min-height: 44px; height: 44px; display: flex; align-items: stretch; gap: 2px; padding: 0 10px; border-bottom: 1px solid #e1e2e6; background: #fff; }
.studio-tabs button { position: relative; min-width: 76px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 0 10px; border: 0; border-radius: 7px; background: transparent; color: #777a82; font-size: 10px; font-weight: 500; cursor: pointer; }
.studio-tabs button:hover { color: #2d2f34; background: #fafafa; }
.studio-tabs button.active { color: #292b30; font-weight: 650; }
.studio-tabs button.active::after { position: absolute; right: 7px; bottom: 0; left: 7px; height: 2px; border-radius: 2px 2px 0 0; background: #292b30; content: ''; }
.studio-tabs button b { min-width: 19px; padding: 2px 5px; border-radius: 5px; background: #eeeef0; color: #777980; font-size: 8px; font-weight: 600; }
.studio-tabs > span { flex: 1; }
.studio-live-controls { display: flex; align-items: center; gap: 7px; }
.studio-live-controls :deep(.el-switch) { --el-switch-on-color: #18bd78; }
.studio-live-controls :deep(.el-switch__label) { color: #6d7078; font-size: 10px; }
.stop-button, .sync-button { height: 32px; border-radius: 8px; font-size: 10px; }
.stop-button { border-color: #d8dae0; color: #a94e48; box-shadow: inset 2px 0 0 #c8655d; }
.sync-button { border-color: #d8dae0; color: #55585f; }
.team-run-layout { min-height: 0; display: grid; flex: 1; grid-template-columns: minmax(0, 1fr) 350px; overflow: hidden; }
.team-canvas { position: relative; min-width: 0; overflow: hidden; background-color: #fff; background-image: radial-gradient(circle, rgba(123, 130, 144, .17) .8px, transparent .8px); background-size: 14px 14px; }
.team-runbar { position: absolute; top: 20px; left: 22px; z-index: 4; height: 46px; display: flex; align-items: center; gap: 10px; max-width: calc(100% - 44px); padding: 0 12px; overflow: hidden; border: 1px solid #d9dbe1; border-radius: 12px; background: rgba(255, 255, 255, .96); box-shadow: 0 4px 14px rgba(49, 55, 82, .06); color: #72757d; font-size: 10px; }
.team-runbar strong { color: #33353a; font-size: 11px; white-space: nowrap; }
.team-runbar > span:not(.live-dot) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.team-runbar .run-stage { padding: 3px 6px; border: 1px solid #e1e3e8; border-radius: 5px; color: #858a93; font-size: 8px; }
.team-runbar .run-stage.success { border-color: #b9efd3; background: #e9fbf2; color: #128058; }
.team-runbar .run-stage.warning { border-color: #f1e0bd; background: #fff8e9; color: #a66b18; }
.team-runbar .run-stage.danger { border-color: #f1c8c5; background: #fff1f0; color: #b74f49; }
.team-runbar > i { width: 1px; height: 20px; margin-left: auto; background: #e2e3e7; }
.team-runbar > .el-icon { color: #70747c; }
.live-dot { width: 9px; height: 9px; flex: 0 0 9px; border-radius: 50%; background: #d3d5da; }
.live-dot.on { background: #18bd78; box-shadow: 0 0 0 4px rgba(24, 189, 120, .12); }
.team-radial { position: absolute; inset: 68px 24px 18px; display: grid; place-items: center; }
.radial-ring { position: absolute; aspect-ratio: 1; border: 1px solid #dfe5ef; border-radius: 50%; pointer-events: none; }
.radial-ring.ring-outer { width: min(72%, 520px); }
.radial-ring.ring-inner { width: min(57%, 408px); border-color: #e8edf4; }
.radial-center-glow { position: absolute; width: min(45%, 320px); aspect-ratio: 1; border-radius: 50%; background: radial-gradient(circle, rgba(232, 237, 250, .62), rgba(255, 255, 255, 0) 70%); }
.team-center-node { position: relative; z-index: 2; width: min(300px, 43%); aspect-ratio: 1; display: flex; align-items: center; flex-direction: column; justify-content: center; padding: 32px 31px 22px; border: 1px solid #eef1f7; border-radius: 50%; background: rgba(255, 255, 255, .97); box-shadow: 0 20px 38px rgba(68, 80, 123, .1), 0 0 0 10px rgba(249, 250, 255, .86); text-align: center; }
.node-eyebrow { color: #8e94a3; font-size: 8px; font-weight: 700; letter-spacing: .1em; }
.team-center-node h2 { margin: 6px 0 0; color: #30343c; font-size: 20px; font-weight: 700; }
.team-center-node > p { margin: 5px 0 15px; color: #9298a5; font-size: 9px; }
.center-stat-grid { width: 100%; display: grid; grid-template-columns: repeat(2, 1fr); border-top: 1px solid #edf0f4; }
.center-stat-grid div { min-width: 0; padding: 9px 5px 6px; border-right: 1px solid #edf0f4; border-bottom: 1px solid #edf0f4; }
.center-stat-grid div:nth-child(2n) { border-right: 0; }
.center-stat-grid span, .center-stat-grid strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.center-stat-grid span { color: #a1a6b1; font-size: 8px; }
.center-stat-grid strong { margin-top: 3px; color: #555a65; font-size: 9px; font-weight: 650; }
.adjust-button { display: inline-flex; align-items: center; gap: 5px; margin-top: 10px; padding: 0; border: 0; background: transparent; color: #6576c2; font-size: 9px; font-weight: 650; cursor: pointer; }
.adjust-button:hover { color: #3654b5; }
.radial-node { position: absolute; z-index: 3; display: flex; align-items: center; flex-direction: column; gap: 5px; min-width: 96px; text-align: center; }
.radial-node-icon { width: 60px; height: 60px; display: grid; place-items: center; border: 1px solid #cfeee0; border-radius: 50%; background: #effbf6; color: #2da477; box-shadow: 0 7px 16px rgba(47, 151, 116, .08); }
.radial-node.blue .radial-node-icon { border-color: #d2dcfb; background: #f1f4ff; color: #657ed6; }
.radial-node strong { color: #418d76; font-size: 10px; font-weight: 650; }
.radial-node.blue strong { color: #6372be; }
.radial-node small { color: #99a0ae; font-size: 9px; }
.radial-node.is-active .radial-node-icon { box-shadow: 0 0 0 5px rgba(45, 164, 119, .1), 0 7px 16px rgba(47, 151, 116, .12); }
.radial-node.blue.is-active .radial-node-icon { box-shadow: 0 0 0 5px rgba(101, 126, 214, .1), 0 7px 16px rgba(101, 126, 214, .12); }
.radial-node.north { top: 8%; left: 50%; transform: translateX(-50%); }
.radial-node.east { top: 35%; right: 8%; }
.radial-node.south-east { right: 24%; bottom: 4%; }
.radial-node.south-west { bottom: 4%; left: 24%; }
.radial-node.west { top: 35%; left: 8%; }
.execution-rail { min-width: 0; display: flex; flex-direction: column; overflow: auto; border-left: 1px solid #e1e2e6; background: #fff; }
.rail-header { padding: 24px 18px 14px; border-bottom: 1px solid #edf0f4; }
.rail-header > span, .queue-section > header > span { color: #9a9da5; font-size: 8px; font-weight: 700; letter-spacing: .08em; }
.rail-header > div, .queue-section > header > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 5px; }
.rail-header h2, .queue-section h2 { margin: 0; color: #34373d; font-size: 16px; font-weight: 650; }
.rail-header b, .queue-section b { min-width: 23px; padding: 5px 6px; border-radius: 7px; background: #f0f1f3; color: #777b83; font-size: 9px; text-align: center; }
.execution-list { display: grid; gap: 7px; padding: 14px 14px 18px; }
.execution-item { min-height: 57px; display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 9px; background: #f7f8fa; }
.execution-item:first-child { background: #eef2ff; }
.execution-mark { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; background: #fae1de; color: #d66d65; font-size: 12px; }
.execution-mark.success { background: #dff5e9; color: #2a976f; }
.execution-mark.warning { background: #fff0d8; color: #b7781d; }
.execution-mark.danger { background: #fae1de; color: #d66d65; }
.execution-mark.info { background: #eef0f3; color: #858a93; }
.execution-item div { min-width: 0; }
.execution-item strong, .execution-item small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.execution-item strong { color: #484b53; font-size: 10px; font-weight: 650; }
.execution-item small { margin-top: 3px; color: #9297a1; font-size: 8px; }
.execution-item > span:last-child { color: #8b9099; font-size: 8px; white-space: nowrap; }
.queue-section { margin-top: auto; padding: 17px 18px 24px; border-top: 1px solid #e7e9ed; }
.queue-row { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 3px 8px; align-items: center; margin-top: 14px; }
.queue-row > span { width: 24px; height: 24px; display: grid; place-items: center; grid-row: 1 / 3; border-radius: 7px; background: #f0f1f3; color: #757980; font-size: 9px; }
.queue-row strong { color: #545860; font-size: 10px; }
.queue-row small { color: #969aa3; font-size: 8px; }
.team-settings-view, .team-list-view, .team-history-view { min-height: 0; flex: 1; overflow: auto; padding: 14px; background: #f7f8fa; }
.team-settings-view .control-panel, .team-list-view .table-panel, .team-history-view .table-panel { margin-bottom: 12px; }
.team-history-view { display: grid; align-content: start; gap: 12px; }
.team-history-view .el-card { margin: 0; }
.team-list-view .stats-grid { margin-bottom: 12px; }
@media (max-width: 980px) {
  .team-run-layout { grid-template-columns: minmax(0, 1fr) 300px; }
  .radial-ring.ring-outer { width: min(78%, 470px); }
  .radial-ring.ring-inner { width: min(62%, 370px); }
  .team-center-node { width: min(260px, 48%); padding-inline: 25px; }
}
@media (max-width: 760px) {
  .team-page { height: auto; min-height: calc(100vh - 96px); }
  .team-studio { overflow: visible; }
  .studio-tabs { overflow-x: auto; }
  .studio-tabs > span { min-width: 12px; }
  .studio-live-controls { display: none; }
  .team-run-layout { display: flex; min-height: 760px; flex-direction: column; overflow: visible; }
  .team-canvas { min-height: 570px; }
  .execution-rail { min-height: 360px; border-top: 1px solid #e1e2e6; border-left: 0; }
  .team-radial { inset: 74px 4px 18px; }
  .radial-node-icon { width: 48px; height: 48px; }
  .radial-node strong { font-size: 9px; }
  .radial-node small { font-size: 8px; }
  .radial-node.north { top: 8%; }
  .radial-node.east { right: 0; }
  .radial-node.south-east { right: 15%; bottom: 3%; }
  .radial-node.south-west { bottom: 3%; left: 15%; }
  .radial-node.west { left: 0; }
  .team-center-node { width: min(240px, 55%); padding: 27px 22px 18px; }
  .team-center-node h2 { font-size: 17px; }
  .center-stat-grid div { padding-block: 7px 5px; }
}
.title-line { display: flex; align-items: center; gap: 10px; }
.header-count { color: var(--el-text-color-secondary); font-size: 12px; font-weight: 500; }
.table-panel :deep(.el-card__header) { padding-bottom: 15px; }
.member-header { align-items: flex-start; gap: 16px; }
.member-filters { flex: 0 0 auto; }
.table-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 14px; }
.table-hint { color: var(--el-text-color-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.detail-footer { margin-top: 12px; }
.seat-pools { display: grid; gap: 2px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.35; }
.seat-pools .advanced { color: var(--el-color-primary); font-weight: 600; }
.control-grid { display: grid; grid-template-columns: 190px 140px 140px minmax(260px, 1fr); gap: 0 16px; align-items: end; }
.control-grid :deep(.el-form-item) { margin-bottom: 12px; }
.control-grid :deep(.control-field) { display: block; }
.control-grid :deep(.control-field .el-form-item__label) { display: block; width: auto; height: auto; padding: 0 0 7px; line-height: 18px; }
.control-grid :deep(.control-field .el-form-item__content) { display: block; margin-left: 0 !important; min-width: 0; }
.control-grid :deep(.el-input-number) { width: 100%; }
.control-grid :deep(.el-input) { width: 100%; }
.control-actions { grid-column: 1 / -1; display: flex; gap: 8px; flex-wrap: wrap; }
.runtime-error { margin-top: 14px; }
.stats-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 16px; }
.stats-grid .el-card { margin-bottom: 0; }
.primary-cell { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.primary-cell strong { color: var(--app-title); font-size: 13px; }
.primary-cell span { overflow: hidden; color: var(--el-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.error-text { display: inline-block; max-width: 145px; overflow: hidden; color: var(--el-color-danger); text-overflow: ellipsis; white-space: nowrap; }
.usage-cell { display: grid; grid-template-columns: minmax(90px, 1fr) 44px; align-items: center; gap: 10px; font-variant-numeric: tabular-nums; }
.event-log { max-height: 340px; overflow-y: auto; }
.event-row { display: grid; grid-template-columns: 150px 72px minmax(0, auto) minmax(0, auto) 1fr; align-items: center; gap: 10px; min-height: 36px; padding: 6px 4px; border-bottom: 1px solid var(--app-border); font-size: 12px; }
.event-row:last-child { border-bottom: 0; }
.event-row time { color: var(--el-text-color-secondary); font-variant-numeric: tabular-nums; }
.event-row.error > span:last-child { color: var(--el-color-danger); }
.event-row.warning > span:last-child { color: var(--el-color-warning); }
.event-row strong, .event-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.detail-summary { display: flex; gap: 28px; margin-bottom: 14px; color: var(--el-text-color-secondary); font-size: 12px; }
.detail-summary strong { margin-left: 5px; color: var(--app-title); font-size: 16px; }
@media (max-width: 1050px) {
  .control-grid { grid-template-columns: 1fr 1fr; }
  .control-grid > :nth-child(4) { grid-column: 1 / -1; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 680px) {
  .control-grid, .form-grid { grid-template-columns: 1fr; }
  .control-grid > :nth-child(4), .control-actions { grid-column: 1; }
  .stats-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .member-header { flex-direction: column; align-items: stretch; }
  .member-filters { max-width: 100%; overflow-x: auto; }
  .table-footer { flex-direction: column; align-items: flex-start; }
  .event-row { grid-template-columns: 1fr auto; gap: 5px 8px; }
  .event-row > span:last-child { grid-column: 1 / -1; white-space: normal; }
  .detail-summary { gap: 14px; justify-content: space-between; }
}
</style>
