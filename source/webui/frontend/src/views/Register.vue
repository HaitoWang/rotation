<script setup>
import { onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { startRegister, getRegistered } from '@/api/register'
import { copyText } from '@/api/request'
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
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) regEmail.value = String(route.query.email)
})

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
</template>

<style scoped>
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
