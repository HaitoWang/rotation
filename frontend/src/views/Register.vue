<script setup>
import { computed, onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { startRegister, getRegistered, listRuns } from '@/api/register'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'

const route = useRoute()
const { form } = storeToRefs(useFormStore())
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
const { runningSingle, lastRunResult } = storeToRefs(runtime)

const starting = ref(false)
const regEmail = ref('')
const activeView = ref('run')
const runHistory = ref([])
const historyLoading = ref(false)
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) regEmail.value = String(route.query.email)
  loadHistory()
})

const runState = computed(() => {
  if (runningSingle.value) return 'running'
  if (lastRunResult.value?.error) return 'error'
  if (lastRunResult.value) return 'complete'
  return 'idle'
})
const runLabel = computed(() => ({ running: '运行中', complete: '已完成', error: '执行失败', idle: '等待执行' }[runState.value]))
const pipelineNodes = computed(() => [
  { key: 'mail', label: '邮箱与密码', detail: '自动领取邮箱并生成密码', icon: 'Message', tone: 'green', state: runState.value === 'idle' ? 'idle' : 'complete' },
  { key: 'otp', label: '注册与邮箱 OTP', detail: 'Outlook 邮件协议验证', icon: 'MagicStick', tone: 'red', state: runState.value === 'error' ? 'error' : runState.value === 'running' ? 'running' : runState.value === 'complete' ? 'complete' : 'idle' },
  { key: 'sms', label: '手机接码', detail: form.value.want2fa ? '等待短信验证码' : '未配置时跳过', icon: 'Iphone', tone: 'green', state: form.value.want2fa ? 'idle' : 'skipped' },
  { key: 'oauth', label: '双重验证与 OAuth', detail: '双重验证 · OAuth', icon: 'Connection', tone: 'blue', state: 'idle' },
  { key: 'pool', label: '进入人成品池', detail: '注册成功后归属当前用户', icon: 'Files', tone: 'orange', state: 'idle' },
])

async function loadHistory() {
  historyLoading.value = true
  try {
    const { items } = await listRuns(12)
    runHistory.value = Array.isArray(items) ? items : []
  } catch (_) {
    runHistory.value = []
  } finally {
    historyLoading.value = false
  }
}

async function run() {
  starting.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const r = await startRegister({
      email: regEmail.value.trim() || null,
      proxy: form.value.proxy.trim(),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_2fa: form.value.want2fa,
    })
    runtime.addLog(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, 'evt')
    runtime.streamRun(r.run_id)
    loadHistory()
  } catch (e) {
    ElMessage.error(e.message)
    lastRunResult.value = { error: e.message }
  } finally {
    starting.value = false
  }
}

async function copyField(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) {
    ElMessage.error('加载凭证失败: ' + e.message)
  }
}
</script>

<template>
  <div class="page register-page">
    <div class="register-studio">
      <nav class="studio-tabs" aria-label="注册机工作区视图">
        <button :class="{ active: activeView === 'run' }" @click="activeView = 'run'"><el-icon><VideoPlay /></el-icon>运行 <b>{{ runHistory.length }}</b></button>
        <button :class="{ active: activeView === 'logs' }" @click="activeView = 'logs'"><el-icon><Document /></el-icon>运行日志</button>
        <button @click="router.push('/pool')"><el-icon><Files /></el-icon>邮箱池</button>
        <button @click="router.push('/registered')"><el-icon><CircleCheck /></el-icon>成品池</button>
        <button :class="{ active: activeView === 'settings' }" @click="activeView = 'settings'"><el-icon><Connection /></el-icon>流程设置</button>
        <span />
        <div class="studio-live-controls">
          <el-switch :model-value="runningSingle" active-text="Live" />
          <el-button type="primary" :loading="starting || runningSingle" :icon="VideoPlay" @click="run">新建批次</el-button>
        </div>
      </nav>

      <div v-if="activeView === 'run'" class="register-run-layout">
        <section class="register-canvas">
          <div class="register-runbar">
            <strong>{{ lastRunResult?.run_id ? `运行 ${lastRunResult.run_id}` : '运行准备' }}</strong>
            <span><i :class="runState" />{{ runLabel }}</span>
            <span v-if="lastRunResult?.email">{{ lastRunResult.email }}</span>
            <button type="button" @click="activeView = 'settings'"><el-icon><Connection /></el-icon>流程设置</button>
          </div>
          <div class="registration-pipeline">
            <i class="pipeline-wire wire-top" />
            <i class="pipeline-wire wire-bottom" />
            <i class="pipeline-wire wire-drop" />
            <template v-for="(node, index) in pipelineNodes" :key="node.key">
              <article class="registration-pipeline-node" :class="[node.tone, `state-${node.state}`]">
                <span><el-icon :size="17"><component :is="node.icon" /></el-icon></span>
                <strong>{{ node.label }}</strong>
                <small>{{ node.detail }}</small>
                <em v-if="node.state !== 'idle'" class="node-status" :class="node.state">{{ node.state === 'complete' ? '已完成' : node.state === 'running' ? '执行中' : node.state === 'error' ? '执行失败' : '已跳过' }}</em>
              </article>
              <i v-if="index < pipelineNodes.length - 1" class="pipeline-link"><el-icon :size="14"><ArrowRight /></el-icon></i>
            </template>
          </div>
        </section>
        <aside class="run-history-rail">
          <header class="rail-header"><span>EXECUTION</span><div><h2>运行历史</h2><b>{{ runHistory.length }}</b></div></header>
          <div class="run-history-list">
            <button v-for="(item, index) in runHistory" :key="item.run_id || index" class="run-history-item" :class="{ selected: index === 0 }" type="button">
              <span class="history-mark" :class="item.status"><el-icon><CircleClose v-if="item.status === 'failed'" /><CircleCheck v-else /></el-icon></span>
              <span><strong>{{ item.run_id || `Run #${index + 1}` }}</strong><small>{{ item.email || '-' }} · {{ item.status || 'pending' }}</small></span>
              <time>{{ fmtTime(item.started_at) }}</time>
            </button>
            <el-empty v-if="!runHistory.length && !historyLoading" :image-size="58" description="暂无运行历史" />
            <el-skeleton v-if="historyLoading" :rows="4" animated />
          </div>
          <section class="run-overview">
            <header><span>概览</span><b>{{ lastRunResult?.run_id ? '当前运行' : '等待运行' }}</b></header>
            <div class="overview-grid">
              <div class="success"><strong>{{ lastRunResult && !lastRunResult.error ? 1 : 0 }}</strong><span>成功</span></div>
              <div class="danger"><strong>{{ lastRunResult?.error ? 1 : 0 }}</strong><span>失败</span></div>
              <div><strong>{{ runningSingle ? 1 : 0 }}</strong><span>执行中</span></div>
              <div><strong>{{ form.want2fa ? '开' : '关' }}</strong><span>2FA</span></div>
            </div>
            <div class="run-progress"><div><strong>批次进度</strong><span>{{ runState === 'complete' ? '5 / 5' : runState === 'running' ? '执行中' : '等待执行' }}</span></div><el-progress :percentage="runState === 'complete' ? 100 : runState === 'running' ? 48 : 0" :show-text="false" /></div>
            <p v-if="lastRunResult?.error" class="run-error">{{ lastRunResult.error }}</p>
          </section>
        </aside>
      </div>

      <div v-else-if="activeView === 'logs'" class="register-logs-view">
        <el-card shadow="never"><LogPanel /></el-card>
      </div>

      <div v-else-if="activeView === 'settings'" class="register-settings-view">
        <div class="register-grid">
      <el-card shadow="never" class="setup-card">
          <template #header>
            <div class="panel-title-group">
              <h2 class="section-title">注册参数</h2>
              <p class="section-subtitle">留空邮箱时，系统会自动领取下一个可用账号</p>
            </div>
          </template>
          <el-form label-position="top">
            <el-form-item label="邮箱账号">
              <el-input v-model="regEmail" placeholder="自动选择，或输入指定邮箱" clearable>
                <template #prefix><el-icon><Message /></el-icon></template>
              </el-input>
            </el-form-item>
            <el-form-item label="网络代理">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="直连，或选择 / 输入代理地址"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
            <div class="form-row">
              <el-form-item label="OTP 等待时间">
                <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
                <span class="input-suffix">秒</span>
              </el-form-item>
            </div>
            <div class="setting-row">
              <div class="setting-copy">
                <strong>自动绑定 2FA</strong>
                <span>注册成功后启用 TOTP 动态验证码</span>
              </div>
              <el-switch v-model="form.want2fa" />
            </div>
            <el-alert v-if="form.want2fa" class="security-note" type="warning" :closable="false" show-icon>
              2FA secret 仅下发一次。注册完成后请立即复制并妥善备份。
            </el-alert>
            <el-button class="primary-action" type="primary" :loading="starting || runningSingle" @click="run">
              <el-icon><VideoPlay /></el-icon>{{ runningSingle ? '注册进行中' : '开始注册' }}
            </el-button>
          </el-form>

          <el-alert
            v-if="lastRunResult && !lastRunResult.error"
            type="success" :closable="false" class="result-alert"
          >
            <strong>注册完成</strong> · {{ lastRunResult.email }}
            <div v-if="lastRunResult.password" class="cred-line">
              <span class="cred-label">密码</span><code class="cred-val">{{ lastRunResult.password }}</code>
            </div>
            <div v-else class="cred-line hint">该号未设置密码（服务端未走密码注册流程）</div>
            <div v-if="lastRunResult.totp_secret" class="cred-line">
              <span class="cred-label">2FA</span><code class="cred-val">{{ lastRunResult.totp_secret }}</code>
              <span class="hint" style="margin-left: 6px">仅此一次！务必复制录入验证器</span>
            </div>
            <div class="result-actions">
              <el-button size="small" @click="copyText(lastRunResult.email)">复制邮箱</el-button>
              <template v-if="lastRunResult.password">
                <el-button size="small" type="primary" @click="copyText(lastRunResult.password)">复制密码</el-button>
                <el-button size="small" @click="copyText(lastRunResult.email + '----' + lastRunResult.password)">
                  复制 邮箱----密码
                </el-button>
              </template>
              <el-button v-if="lastRunResult.access_token_len > 0" size="small"
                         @click="copyField(lastRunResult.email, 'access_token')">复制 access_token</el-button>
              <el-button v-if="lastRunResult.totp_secret" size="small" type="warning"
                         @click="copyText(lastRunResult.totp_secret)">复制 2FA secret</el-button>
            </div>
          </el-alert>
          <el-alert
            v-else-if="lastRunResult && lastRunResult.error"
            type="error" :closable="false" class="result-alert" :title="lastRunResult.error"
          />
      </el-card>

      <el-card shadow="never" class="log-card">
          <LogPanel />
      </el-card>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.register-page { width: 100%; height: calc(100vh - 74px); min-height: 560px; display: flex; flex-direction: column; }
.register-studio { min-height: 0; display: flex; flex: 1; flex-direction: column; overflow: hidden; border: 1px solid #dedfe4; border-radius: 10px; background: #fff; }
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
.studio-live-controls .el-button { height: 32px; border-radius: 8px; font-size: 10px; }
.register-run-layout { min-height: 0; display: grid; flex: 1; grid-template-columns: minmax(0, 1fr) 350px; overflow: hidden; }
.register-canvas { position: relative; min-width: 0; overflow: hidden; background-color: #fff; background-image: radial-gradient(circle, rgba(123, 130, 144, .17) .8px, transparent .8px); background-size: 14px 14px; }
.register-runbar { position: absolute; top: 20px; left: 22px; z-index: 5; max-width: calc(100% - 44px); height: 48px; display: flex; align-items: center; gap: 13px; overflow: hidden; padding: 0 13px; border: 1px solid #d9dbe1; border-radius: 12px; background: rgba(255, 255, 255, .96); box-shadow: 0 4px 14px rgba(49, 55, 82, .06); color: #74777e; font-size: 10px; }
.register-runbar strong { color: #33353a; font-size: 12px; white-space: nowrap; }
.register-runbar > span { display: flex; align-items: center; gap: 5px; overflow: hidden; padding-left: 12px; border-left: 1px solid #e2e3e7; white-space: nowrap; }
.register-runbar > span i { width: 16px; height: 16px; flex: 0 0 16px; display: grid; place-items: center; border-radius: 50%; background: #e6e7ea; }
.register-runbar > span i.running { background: #18bd78; box-shadow: 0 0 0 3px rgba(24, 189, 120, .12); }
.register-runbar > span i.complete { background: #18bd78; }
.register-runbar > span i.error { border: 1px solid #d66d65; background: #fff1f0; }
.register-runbar button { display: inline-flex; align-items: center; gap: 5px; flex: 0 0 auto; padding-left: 12px; border: 0; border-left: 1px solid #e2e3e7; background: transparent; color: #73767d; font-size: 10px; cursor: pointer; }
.registration-pipeline { position: absolute; top: 53%; right: 24px; left: 24px; z-index: 3; display: grid; grid-template-columns: minmax(0, 1fr) 28px minmax(0, 1fr) 28px minmax(0, 1fr) 28px minmax(0, 1fr) 28px minmax(0, 1fr); align-items: center; gap: 0; transform: translateY(-50%); }
.registration-pipeline-node { position: relative; min-width: 0; min-height: 72px; display: grid; grid-template-columns: 27px minmax(0, 1fr); grid-template-rows: auto auto; align-content: center; column-gap: 7px; padding: 9px 9px; border: 1px solid #cfd6e2; border-radius: 10px; background: #fff; box-shadow: 0 5px 14px rgba(47, 64, 80, .06); color: #303237; }
.registration-pipeline-node > span { width: 27px; height: 27px; display: grid; place-items: center; grid-row: 1 / 3; border-radius: 7px; background: #e3f3ff; color: #247fa6; }
.registration-pipeline-node.red > span { background: #fff0ef; color: #d16b63; }
.registration-pipeline-node.blue > span { background: #e9efff; color: #4e6fc0; }
.registration-pipeline-node.orange > span { background: #fff0de; color: #aa6a1d; }
.registration-pipeline-node.green > span { background: #e5f7ef; color: #26805f; }
.registration-pipeline-node strong, .registration-pipeline-node small { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.registration-pipeline-node strong { align-self: end; color: #303237; font-size: 10px; font-weight: 650; }
.registration-pipeline-node small { align-self: start; margin-top: 2px; color: #858a93; font-size: 8px; }
.registration-pipeline-node.state-complete { border-color: #9adbbf; }
.registration-pipeline-node.state-running { border-color: #8fb3f4; box-shadow: 0 8px 22px rgba(45, 108, 226, .14); }
.registration-pipeline-node.state-error { border-color: #efaca5; }
.registration-pipeline-node.state-skipped { border-style: dashed; border-color: #cfd3da; background: #fafafa; }
.node-status { position: absolute; top: -21px; right: 8px; min-height: 18px; display: inline-flex; align-items: center; padding: 0 6px; border: 1px solid #dfe3e9; border-radius: 6px; background: #f5f6f8; color: #757b85; font-size: 7px; font-style: normal; font-weight: 700; white-space: nowrap; }
.node-status.complete { border-color: #b9efd3; background: #e9fbf2; color: #128058; }
.node-status.running { border-color: #bfd5ff; background: #edf4ff; color: #2766cf; }
.node-status.error { border-color: #f1c8c5; background: #fff1f0; color: #b74f49; }
.node-status.skipped { border-color: #d9dde4; background: #f5f6f8; color: #868c96; }
.pipeline-link { height: 1px; display: flex; align-items: center; justify-content: flex-end; background: #c8d1df; color: #aebbd0; }
.pipeline-link .el-icon { margin-right: -5px; background: #fff; }
.registration-pipeline { inset: 0; top: 0; right: 0; left: 0; display: block; transform: none; }
.registration-pipeline > .pipeline-link { display: none; }
.registration-pipeline-node { position: absolute; width: min(300px, 30%); min-height: 76px; }
.registration-pipeline-node:nth-of-type(1) { top: 38%; left: 10%; }
.registration-pipeline-node:nth-of-type(2) { top: 38%; right: 10%; }
.registration-pipeline-node:nth-of-type(3) { bottom: 21%; left: 6%; }
.registration-pipeline-node:nth-of-type(4) { bottom: 21%; left: 40%; }
.registration-pipeline-node:nth-of-type(5) { right: 6%; bottom: 21%; }
.pipeline-wire { position: absolute; z-index: 1; display: block; pointer-events: none; }
.pipeline-wire.wire-top { top: calc(38% + 38px); left: calc(10% + min(300px, 30%)); width: calc(80% - min(600px, 60%)); height: 1px; background: #c8d1df; }
.pipeline-wire.wire-bottom { bottom: calc(21% + 38px); left: calc(6% + min(300px, 30%)); width: calc(88% - min(600px, 60%)); height: 1px; background: #c8d1df; }
.pipeline-wire.wire-drop { top: calc(38% + 38px); right: calc(10% + min(300px, 30%)); width: 1px; height: 24%; background: #c8d1df; }
.run-history-rail { min-width: 0; display: flex; flex-direction: column; overflow: auto; border-left: 1px solid #e1e2e6; background: #fff; }
.rail-header { padding: 24px 18px 14px; border-bottom: 1px solid #edf0f4; }
.rail-header > span, .run-overview > header > span { color: #9a9da5; font-size: 8px; font-weight: 700; letter-spacing: .08em; }
.rail-header > div, .run-overview > header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 5px; }
.rail-header h2 { margin: 0; color: #34373d; font-size: 18px; font-weight: 650; }
.rail-header b { min-width: 23px; padding: 5px 6px; border-radius: 7px; background: #f0f1f3; color: #777b83; font-size: 9px; text-align: center; }
.run-history-list { display: grid; gap: 7px; padding: 14px 14px 18px; }
.run-history-item { min-height: 57px; display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 8px 10px; border: 0; border-radius: 9px; background: #f7f8fa; text-align: left; cursor: pointer; }
.run-history-item.selected { background: #f0f3ff; }
.run-history-item:hover { background: #edf1fb; }
.history-mark { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; background: #e0f5e9; color: #2a976f; }
.history-mark.failed { background: #fae1de; color: #d66d65; }
.run-history-item > span:nth-child(2) { min-width: 0; }
.run-history-item strong, .run-history-item small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-history-item strong { color: #484b53; font-size: 10px; font-weight: 650; }
.run-history-item small, .run-history-item time { margin-top: 3px; color: #9297a1; font-size: 8px; }
.run-history-item time { white-space: nowrap; }
.run-overview { margin-top: auto; padding: 17px 18px 24px; border-top: 1px solid #e7e9ed; }
.run-overview > header { margin-top: 0; }
.run-overview > header b { color: #8c9098; font-size: 9px; font-weight: 600; }
.overview-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 12px; }
.overview-grid > div { min-height: 70px; display: flex; justify-content: center; flex-direction: column; padding: 10px; border: 1px solid #e7e9ed; border-radius: 8px; background: #fff; }
.overview-grid > div.success { border-color: #d6f0e2; background: #f3fbf7; }.overview-grid > div.danger { border-color: #f3deda; background: #fff7f6; }
.overview-grid strong { color: #34373d; font-size: 20px; font-weight: 650; }.overview-grid .success strong { color: #26805f; }.overview-grid .danger strong { color: #b74f49; }
.overview-grid span { margin-top: 5px; color: #90949d; font-size: 8px; }
.run-progress { margin-top: 14px; }.run-progress > div { display: flex; justify-content: space-between; color: #7d818a; font-size: 9px; }.run-progress strong { color: #45484f; }.run-progress :deep(.el-progress) { margin-top: 8px; }
.run-error { margin: 13px 0 0; padding: 9px; border: 1px solid #f1d0cc; border-radius: 7px; background: #fff6f5; color: #b74f49; font-size: 9px; line-height: 1.45; word-break: break-word; }
.register-logs-view, .register-settings-view { min-height: 0; flex: 1; overflow: auto; padding: 14px; background: #f7f8fa; }
.register-logs-view .el-card { max-width: 980px; margin: 0 auto; }
.register-settings-view .register-grid { max-width: 1180px; margin: 0 auto; }
@media (max-width: 980px) { .register-run-layout { grid-template-columns: minmax(0, 1fr) 300px; }.registration-pipeline { right: 16px; left: 16px; }.registration-pipeline-node { grid-template-columns: 23px minmax(0, 1fr); column-gap: 5px; padding-inline: 7px; }.registration-pipeline-node > span { width: 23px; height: 23px; }.registration-pipeline-node strong { font-size: 9px; }.registration-pipeline-node small { font-size: 7px; } }
@media (max-width: 760px) { .register-page { height: auto; min-height: calc(100vh - 96px); }.register-studio { overflow: visible; }.studio-tabs { overflow-x: auto; }.studio-tabs > span { min-width: 12px; }.studio-live-controls { display: none; }.register-run-layout { display: flex; min-height: 780px; flex-direction: column; overflow: visible; }.register-canvas { min-height: 550px; }.run-history-rail { min-height: 390px; border-top: 1px solid #e1e2e6; border-left: 0; }.registration-pipeline { right: 10px; left: 10px; grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1fr) 14px minmax(0, 1fr) 14px minmax(0, 1fr) 14px minmax(0, 1fr); }.registration-pipeline-node { min-height: 64px; padding: 7px 5px; column-gap: 4px; }.registration-pipeline-node strong { font-size: 7px; }.registration-pipeline-node small { font-size: 6px; }.register-runbar { top: 12px; left: 12px; max-width: calc(100% - 24px); }.register-settings-view, .register-logs-view { padding: 10px; } }
.register-grid { display: grid; grid-template-columns: minmax(330px, .8fr) minmax(480px, 1.2fr); gap: 16px; align-items: start; }
.register-grid .el-card { margin: 0; }
.form-row { display: flex; align-items: flex-end; }
.form-row .el-form-item { margin-bottom: 18px; }
.input-suffix { margin-left: 8px; color: var(--el-text-color-secondary); font-size: 12px; }
.setting-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 2px 0 12px; padding: 13px 14px; border: 1px solid var(--app-border); border-radius: 9px; background: var(--el-fill-color-lighter); }
.setting-copy { min-width: 0; display: flex; flex-direction: column; }
.setting-copy strong { color: var(--app-title); font-size: 12px; font-weight: 600; }
.setting-copy span { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 10px; line-height: 1.4; }
.security-note { margin-bottom: 14px; }
.security-note :deep(.el-alert__description) { margin: 0; font-size: 11px; line-height: 1.5; }
.primary-action { width: 100%; }
.result-alert { margin-top: 14px; }
.result-actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 11px; }
.log-card :deep(.log-box) { height: 520px; }
@media (max-width: 1050px) { .register-grid { grid-template-columns: 1fr; } .log-card :deep(.log-box) { height: 360px; } }
</style>
