<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { autoStart, autoPause, autoResume, autoStop } from '@/api/register'
import { useFormStore } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { form } = storeToRefs(useFormStore())
const proxyStore = useProxyStore()
const { count: proxyCount } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
const { autoStatus } = storeToRefs(runtime)

const st = computed(() => autoStatus.value.state || 'stopped')
const canStart = computed(() => st.value === 'stopped')
const canPause = computed(() => st.value === 'running')
const canResume = computed(() => st.value === 'paused')
const canStop = computed(() => st.value !== 'stopped')

const stateLabel = computed(() => ({
  stopped: '未运行', running: '运行中', paused: '已暂停',
}[st.value] || st.value))
const stateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[st.value] || 'info'))

const workers = computed(() => Array.isArray(autoStatus.value.workers) ? autoStatus.value.workers : [])
const stageEntries = computed(() => Object.entries(autoStatus.value.stage_counts || {}))

function duration(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  if (value < 60) return `${Math.round(value)} 秒`
  if (value < 3600) return `${(value / 60).toFixed(1)} 分钟`
  return `${(value / 3600).toFixed(1)} 小时`
}

async function start() {
  try {
    await autoStart({
      proxy: form.value.proxy.trim(),
      proxy_pool: proxyStore.text,
      concurrency: parseInt(form.value.autoConcurrency, 10) || 1,
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      cool_down_seconds: parseFloat(form.value.autoCoolDown) || 0,
      target_count: parseInt(form.value.autoTargetCount, 10) || 0,
      push_to_hub: form.value.autoPushToHub,
      // 批量默认绑 2FA（后端默认是 false，这个字段以前压根没传，
      // 所以批量跑出来的号一个都没 2FA）。留开关是因为绑定不可逆。
      want_2fa: form.value.autoWant2fa,
    })
    ElMessage.success('自动跑号已启动')
  } catch (e) { ElMessage.error('启动失败: ' + e.message) }
}
async function call(fn, name) {
  try { await fn(); ElMessage.success(name + ' 成功') }
  catch (e) { ElMessage.error(name + ' 失败: ' + e.message) }
}
</script>

<template>
  <div class="page auto-page">
    <div class="auto-grid">
      <el-card shadow="never" class="settings-card">
        <template #header>
          <div class="panel-title-group">
            <h2 class="section-title">任务设置</h2>
            <p class="section-subtitle">启动前调整本轮批量注册策略</p>
          </div>
        </template>

        <el-form label-position="top">
          <div class="field-grid">
            <el-form-item label="并发任务">
              <el-input-number v-model="form.autoConcurrency" :min="1" />
            </el-form-item>
            <el-form-item label="请求冷却（秒）">
              <el-input-number v-model="form.autoCoolDown" :min="0" :max="120" />
            </el-form-item>
            <el-form-item label="目标数量">
              <el-input-number v-model="form.autoTargetCount" :min="0" :max="100000" />
              <span class="field-note">0 表示不限</span>
            </el-form-item>
            <el-form-item label="OTP 等待（秒）">
              <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
            </el-form-item>
          </div>
        </el-form>

        <div class="setting-row">
          <div class="setting-copy">
            <strong>自动绑定 2FA</strong>
            <span>仅对有密码的账号生效，secret 需要及时导出备份</span>
          </div>
          <el-switch v-model="form.autoWant2fa" />
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <strong>成功后推送 Hub</strong>
            <span>仅控制本轮自动注册，不影响 Team 轮转加入后的 Hub 推送</span>
          </div>
          <el-switch v-model="form.autoPushToHub" />
        </div>

        <div class="proxy-row">
          <span class="proxy-icon"><el-icon><Connection /></el-icon></span>
          <div class="proxy-copy">
            <strong>代理池</strong>
            <span>{{ proxyCount ? `${proxyCount} 个代理将由 worker 轮流取用` : '当前为空，将使用单次注册页的兜底代理' }}</span>
          </div>
          <el-button text type="primary" @click="router.push('/proxy')">管理</el-button>
        </div>

        <div class="run-controls">
          <el-button type="primary" :disabled="!canStart" @click="start"><el-icon><VideoPlay /></el-icon>开始任务</el-button>
          <el-button :disabled="!canPause" @click="call(autoPause, '暂停')">暂停</el-button>
          <el-button :disabled="!canResume" @click="call(autoResume, '恢复')">恢复</el-button>
          <el-button type="danger" plain :disabled="!canStop" @click="call(autoStop, '停止')">停止</el-button>
        </div>
      </el-card>

      <el-card shadow="never" class="monitor-card">
        <template #header>
          <div class="panel-header">
            <div class="panel-title-group">
              <h2 class="section-title">运行概况</h2>
              <p class="section-subtitle">任务性能与 worker 健康状态</p>
            </div>
            <StatusDot :type="stateType" :text="stateLabel" />
          </div>
        </template>

        <div class="monitor-metrics">
          <div><span>成功</span><strong class="success">{{ autoStatus.registered_ok || 0 }}</strong></div>
          <div><span>失败</span><strong class="danger">{{ autoStatus.registered_fail || 0 }}</strong></div>
          <div><span>有效并发</span><strong>{{ autoStatus.effective_concurrency || 0 }}</strong></div>
          <div><span>成功/分钟</span><strong>{{ Number(autoStatus.success_per_minute || 0).toFixed(2) }}</strong></div>
          <div><span>独立代理</span><strong>{{ autoStatus.independent_proxy_count || 0 }}</strong></div>
          <div><span>卡死任务</span><strong :class="{ danger: autoStatus.stuck_task_count }">{{ autoStatus.stuck_task_count || 0 }}</strong></div>
        </div>

        <div class="monitor-detail">
          <span>任务年龄 P95</span><strong>{{ duration(autoStatus.p95_task_age_seconds) }}</strong>
        </div>
        <div class="stage-list">
          <el-tag v-for="([name, count]) in stageEntries" :key="name" size="small" effect="plain">{{ name }} {{ count }}</el-tag>
          <span v-if="!stageEntries.length" class="hint">暂无运行任务</span>
        </div>
        <div v-if="workers.length" class="worker-list">
          <div v-for="w in workers" :key="w.id" class="worker-row">
            <span class="worker-dot" />
            <strong>worker-{{ w.id }}</strong>
            <span>{{ w.phase || '注册流程' }}</span>
            <small>{{ w.email }}</small>
          </div>
        </div>
        <p v-if="autoStatus.last_message" class="last-message">{{ autoStatus.last_message }}</p>
      </el-card>
    </div>

    <el-card shadow="never">
      <LogPanel />
    </el-card>
  </div>
</template>

<style scoped>
.auto-grid { display: grid; grid-template-columns: minmax(390px, .9fr) minmax(480px, 1.1fr); gap: 16px; }
.auto-grid .el-card { margin-bottom: 16px; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.field-grid .el-input-number { width: 100%; }
.field-note { margin-top: 5px; color: var(--el-text-color-secondary); font-size: 10px; }
.setting-row, .proxy-row { display: flex; align-items: center; gap: 12px; padding: 12px 13px; border: 1px solid var(--app-border); border-radius: 9px; }
.setting-row { justify-content: space-between; background: var(--el-fill-color-lighter); }
.setting-row + .setting-row { margin-top: 10px; }
.setting-copy, .proxy-copy { min-width: 0; display: flex; flex: 1; flex-direction: column; }
.setting-copy strong, .proxy-copy strong { color: var(--app-title); font-size: 12px; font-weight: 600; }
.setting-copy span, .proxy-copy span { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 10px; line-height: 1.45; }
.proxy-row { margin-top: 10px; }
.proxy-icon { width: 31px; height: 31px; display: grid; place-items: center; flex: 0 0 auto; color: var(--brand); border-radius: 8px; background: var(--brand-soft); }
.run-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
.monitor-metrics { display: grid; grid-template-columns: repeat(3, 1fr); overflow: hidden; border: 1px solid var(--app-border); border-radius: 9px; }
.monitor-metrics div { padding: 13px; border-right: 1px solid var(--app-border); border-bottom: 1px solid var(--app-border); }
.monitor-metrics div:nth-child(3n) { border-right: 0; }
.monitor-metrics div:nth-last-child(-n + 3) { border-bottom: 0; }
.monitor-metrics span, .monitor-metrics strong { display: block; }
.monitor-metrics span { color: var(--el-text-color-secondary); font-size: 10px; }
.monitor-metrics strong { margin-top: 6px; color: var(--app-title); font-size: 17px; font-weight: 650; }
.monitor-metrics .success { color: var(--el-color-success); }
.monitor-metrics .danger { color: var(--el-color-danger); }
.monitor-detail { display: flex; justify-content: space-between; margin-top: 14px; color: var(--el-text-color-secondary); font-size: 11px; }
.monitor-detail strong { color: var(--app-title); }
.stage-list { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 11px; }
.worker-list { max-height: 132px; overflow-y: auto; margin-top: 13px; border-top: 1px solid var(--app-border); }
.worker-row { display: grid; grid-template-columns: 8px 72px 90px minmax(0, 1fr); align-items: center; gap: 7px; padding: 9px 2px; border-bottom: 1px solid var(--app-border); font-size: 10px; }
.worker-row strong { color: var(--app-title); font-weight: 600; }
.worker-row span, .worker-row small { overflow: hidden; color: var(--el-text-color-secondary); text-overflow: ellipsis; white-space: nowrap; }
.worker-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--el-color-success); }
.last-message { margin: 12px 0 0; color: var(--el-text-color-secondary); font-size: 11px; line-height: 1.5; }
@media (max-width: 1120px) { .auto-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .field-grid { grid-template-columns: 1fr; } .monitor-metrics { grid-template-columns: repeat(2, 1fr); } .monitor-metrics div, .monitor-metrics div:nth-child(3n), .monitor-metrics div:nth-last-child(-n + 3) { border-right: 1px solid var(--app-border); border-bottom: 1px solid var(--app-border); } .monitor-metrics div:nth-child(2n) { border-right: 0; } .monitor-metrics div:nth-last-child(-n + 2) { border-bottom: 0; } }
</style>
