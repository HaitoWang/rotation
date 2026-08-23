<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
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
  stopTeamRotation,
  updateTeamMother,
} from '@/api/team'
import { fmtTime } from '@/api/request'

const loading = ref(false)
const actionLoading = ref('')
const status = ref({
  state: 'stopped',
  config: { interval_seconds: 300, quota_threshold: 100, proxy: '' },
  counts: {},
  mothers: [],
  members: [],
  events: [],
})
const config = reactive({ interval_seconds: 300, quota_threshold: 100, proxy: '' })
const configHydrated = ref(false)
let pollTimer = 0

const stateMeta = computed(() => ({
  running: { label: '运行中', type: 'success' },
  paused: { label: '已暂停', type: 'warning' },
  stopped: { label: '已停止', type: 'info' },
}[status.value.state] || { label: status.value.state, type: 'info' }))

const activeUsage = computed(() => {
  const active = status.value.members.filter((item) => item.status === 'active')
  if (!active.length) return 0
  const values = active.map(usageOf).filter((value) => value !== null)
  return values.length ? Math.round(values.reduce((sum, value) => sum + value, 0) / values.length) : 0
})

function usageOf(row) {
  const values = [row.primary_used_percent, row.secondary_used_percent]
    .filter((value) => value !== null && value !== undefined)
    .map(Number)
  return values.length ? Math.max(...values) : null
}

function statusTag(value) {
  return {
    pending: ['待加入', 'warning'],
    active: ['使用中', 'success'],
    exhausted: ['额度耗尽', 'danger'],
    removed: ['已移出', 'info'],
    failed: ['加入失败', 'danger'],
  }[value] || [value || '-', 'info']
}

function hubStatusTag(value) {
  return {
    success: ['已推送', 'success'],
    failed: ['推送失败', 'danger'],
    disabled: ['Hub 未启用', 'info'],
    pending: ['待推送', 'warning'],
  }[value] || [value || '待推送', 'warning']
}

async function loadStatus(silent = false) {
  if (!silent) loading.value = true
  try {
    const data = await getTeamRotationStatus()
    status.value = data
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
    await ElMessageBox.confirm('停止后不会继续检查额度或自动补位。', '停止 Team 轮转', {
      type: 'warning', confirmButtonText: '停止', cancelButtonText: '取消',
    })
  } catch { return }
  runAction('stop', stopTeamRotation, '已请求停止')
}
function checkNow() {
  runAction('check', checkTeamRotationNow, '已触发立即检查')
}

const motherDialog = ref(false)
const motherSaving = ref(false)
const editingId = ref('')
const motherForm = reactive({ name: '', workspace_id: '', session: '', enabled: true })

function openCreate() {
  editingId.value = ''
  Object.assign(motherForm, { name: '', workspace_id: '', session: '', enabled: true })
  motherDialog.value = true
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(motherForm, {
    name: row.name,
    workspace_id: row.workspace_id,
    session: '',
    enabled: row.enabled,
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

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailMother = ref(null)
const detail = ref({ seats: {}, members: [] })

async function inspectMother(row) {
  detailVisible.value = true
  detailLoading.value = true
  detailMother.value = row
  detail.value = { seats: {}, members: [] }
  try {
    detail.value = await getTeamMotherDetail(row.id)
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
    <el-card class="control-panel">
      <template #header>
        <div class="panel-header">
          <div class="panel-title-group">
            <div class="title-line">
              <h2 class="section-title">轮转控制</h2>
              <el-tag :type="stateMeta.type" effect="light">{{ stateMeta.label }}</el-tag>
            </div>
            <p class="section-subtitle">
              第 {{ status.cycle_count || 0 }} 轮
              <span v-if="status.current_mother"> · {{ status.current_mother }}</span>
              <span v-if="status.next_cycle_at"> · 下次 {{ fmtTime(status.next_cycle_at) }}</span>
            </p>
          </div>
          <el-button :icon="Refresh" circle :loading="actionLoading === 'refresh'" @click="loadStatus()" />
        </div>
      </template>

      <div class="control-grid">
        <el-form-item label="检查间隔（秒）">
          <el-input-number v-model="config.interval_seconds" :min="10" :max="86400" :step="30" controls-position="right" />
        </el-form-item>
        <el-form-item label="移出阈值（%）">
          <el-input-number v-model="config.quota_threshold" :min="1" :max="100" :step="1" controls-position="right" />
        </el-form-item>
        <el-form-item label="Team 请求代理">
          <el-input v-model="config.proxy" clearable placeholder="留空直连" />
        </el-form-item>
        <div class="control-actions">
          <el-button v-if="status.state === 'stopped'" type="primary" :icon="VideoPlay" :loading="actionLoading === 'start'" @click="start">启动</el-button>
          <el-button v-if="status.state === 'running'" :icon="VideoPause" :loading="actionLoading === 'pause'" @click="pause">暂停</el-button>
          <el-button v-if="status.state === 'paused'" type="primary" :icon="VideoPlay" :loading="actionLoading === 'resume'" @click="resume">恢复</el-button>
          <el-button v-if="status.state !== 'stopped'" :icon="Refresh" :loading="actionLoading === 'check'" @click="checkNow">立即检查</el-button>
          <el-button v-if="status.state !== 'stopped'" type="danger" plain :icon="SwitchButton" :loading="actionLoading === 'stop'" @click="stop">停止</el-button>
        </div>
      </div>
      <el-alert v-if="status.last_error" class="runtime-error" type="error" :closable="false" :title="status.last_error" />
    </el-card>

    <div class="stats-grid">
      <el-card class="stat-card"><div class="stat-value">{{ status.mothers.length }}</div><div class="stat-label">母号</div></el-card>
      <el-card class="stat-card"><div class="stat-value">{{ status.counts.active || 0 }}</div><div class="stat-label">使用中子号</div></el-card>
      <el-card class="stat-card"><div class="stat-value">{{ status.counts.exhausted || 0 }}</div><div class="stat-label">已轮出</div></el-card>
      <el-card class="stat-card"><div class="stat-value">{{ activeUsage }}%</div><div class="stat-label">平均额度使用</div></el-card>
    </div>

    <el-card>
      <template #header>
        <div class="panel-header">
          <div class="panel-title-group"><h2 class="section-title">母号与席位</h2></div>
          <el-button type="primary" :icon="Plus" @click="openCreate">添加母号</el-button>
        </div>
      </template>
      <el-table :data="status.mothers" stripe empty-text="暂无母号">
        <el-table-column label="母号" min-width="190">
          <template #default="{ row }">
            <div class="primary-cell"><strong>{{ row.name }}</strong><span>{{ row.email || '-' }}</span></div>
          </template>
        </el-table-column>
        <el-table-column prop="workspace_id" label="Workspace ID" min-width="220" show-overflow-tooltip />
        <el-table-column label="席位" width="150">
          <template #default="{ row }">
            <span v-if="row.seats_entitled !== null && row.seats_entitled !== undefined">
              {{ row.seats_in_use || 0 }} / {{ row.seats_entitled }} · 余 {{ row.seats_remaining || 0 }}
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="105">
          <template #default="{ row }"><el-switch :model-value="row.enabled" @change="toggleMother(row, $event)" /></template>
        </el-table-column>
        <el-table-column label="最近检查" width="170">
          <template #default="{ row }"><span :class="{ 'error-text': row.last_error }">{{ row.last_error || fmtTime(row.last_checked_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-tooltip content="查看成员"><el-button text circle :icon="View" @click="inspectMother(row)" /></el-tooltip>
            <el-tooltip content="编辑母号"><el-button text circle :icon="Edit" @click="openEdit(row)" /></el-tooltip>
            <el-tooltip content="删除母号"><el-button text circle type="danger" :icon="Delete" @click="removeMother(row)" /></el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card>
      <template #header><h2 class="section-title">轮转账号</h2></template>
      <el-table :data="status.members" stripe empty-text="暂无轮转记录">
        <el-table-column prop="email" label="子号" min-width="220" show-overflow-tooltip />
        <el-table-column prop="mother_name" label="母号" min-width="140" show-overflow-tooltip />
        <el-table-column label="状态" width="105">
          <template #default="{ row }"><el-tag :type="statusTag(row.status)[1]" effect="light">{{ statusTag(row.status)[0] }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Hub" width="120">
          <template #default="{ row }">
            <el-tooltip :disabled="!row.hub_error" :content="row.hub_error">
              <el-tag :type="hubStatusTag(row.hub_status)[1]" effect="light">{{ hubStatusTag(row.hub_status)[0] }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="额度" min-width="190">
          <template #default="{ row }">
            <div v-if="usageOf(row) !== null" class="usage-cell">
              <el-progress :percentage="usageOf(row)" :stroke-width="7" :show-text="false" :status="usageOf(row) >= config.quota_threshold ? 'exception' : ''" />
              <span>{{ usageOf(row) }}%</span>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="加入时间" width="170"><template #default="{ row }">{{ fmtTime(row.joined_at) }}</template></el-table-column>
        <el-table-column label="最近检查" width="170"><template #default="{ row }">{{ fmtTime(row.last_checked_at) }}</template></el-table-column>
        <el-table-column prop="error" label="状态说明" min-width="220" show-overflow-tooltip />
      </el-table>
    </el-card>

    <el-card>
      <template #header><h2 class="section-title">轮转事件</h2></template>
      <div class="event-log">
        <div v-for="event in status.events" :key="event.id" class="event-row" :class="event.level.toLowerCase()">
          <time>{{ fmtTime(event.created_at) }}</time>
          <el-tag size="small" effect="plain">{{ event.action }}</el-tag>
          <strong v-if="event.mother_name">{{ event.mother_name }}</strong>
          <span v-if="event.email" class="mono">{{ event.email }}</span>
          <span>{{ event.message }}</span>
        </div>
        <el-empty v-if="!status.events.length" :image-size="54" description="暂无事件" />
      </div>
    </el-card>

    <el-dialog v-model="motherDialog" :title="editingId ? '编辑母号' : '添加母号'" width="620px" destroy-on-close>
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="名称"><el-input v-model="motherForm.name" maxlength="100" /></el-form-item>
          <el-form-item label="Workspace ID"><el-input v-model="motherForm.workspace_id" placeholder="可从 Session 自动识别" /></el-form-item>
        </div>
        <el-form-item :label="editingId ? 'Session / Access Token（留空不修改）' : 'Session / Access Token'">
          <el-input v-model="motherForm.session" type="textarea" :rows="7" resize="vertical" />
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
        <span>剩余 <strong>{{ detail.seats.remaining_default ?? '-' }}</strong></span>
      </div>
      <el-table v-loading="detailLoading" :data="detail.members" stripe max-height="520" empty-text="暂无成员">
        <el-table-column prop="email" label="邮箱" min-width="220" show-overflow-tooltip />
        <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
        <el-table-column prop="role" label="角色" width="130" />
        <el-table-column prop="seat_type" label="席位类型" width="120" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="danger" :icon="Delete" :disabled="row.is_owner" @click="kickMember(row)">移出</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.title-line { display: flex; align-items: center; gap: 10px; }
.control-grid { display: grid; grid-template-columns: 190px 190px minmax(260px, 1fr); gap: 0 16px; align-items: end; }
.control-grid :deep(.el-form-item) { margin-bottom: 12px; }
.control-grid :deep(.el-input-number) { width: 100%; }
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
  .control-grid > :nth-child(3) { grid-column: 1 / -1; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 680px) {
  .control-grid, .form-grid { grid-template-columns: 1fr; }
  .control-grid > :nth-child(3), .control-actions { grid-column: 1; }
  .stats-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .event-row { grid-template-columns: 1fr auto; gap: 5px 8px; }
  .event-row > span:last-child { grid-column: 1 / -1; white-space: normal; }
  .detail-summary { gap: 14px; justify-content: space-between; }
}
</style>
