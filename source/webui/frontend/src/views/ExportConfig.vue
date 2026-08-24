<script setup>
import { onActivated, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { getExportConfig, getSub2ApiGroups, saveExportConfig, testExport } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const defaultSub2ApiModels = [
  'gpt-image-2',
  'gpt-5.3-codex',
  'gpt-5.4',
  'gpt-5.4-mini',
  'gpt-5.5',
  'gpt-5.6',
  'gpt-5.6-sol',
  'gpt-5.6-luna',
  'gpt-5.6-terra',
  'codex-auto-review',
]
const fingerprintModes = [
  { value: 'off', label: '关闭 (off)' },
  { value: 'device', label: '设备固定 (device)' },
  { value: 'session', label: '设备与会话固定 (session)' },
  { value: 'full', label: '账号级完全固定 (full)' },
]
const cpa = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴 CPA 管理密钥', timeout: 30 })
const sub = reactive({
  enabled: false,
  url: '',
  key: '',
  keyPh: '粘贴面板里生成的 x-api-key',
  groupIds: [],
  groups: [],
  models: [...defaultSub2ApiModels],
  concurrency: 3,
  fingerprintMode: 'session',
  timeout: 30,
})
const saving = ref(false)
const testingCpa = ref(false)
const testingSub = ref(false)
const loadingGroups = ref(false)
const team = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴 team-sso 同步密钥', timeout: 10, pending: 0 })
const testingTeam = ref(false)

async function load() {
  try {
    const { config } = await getExportConfig()
    cpa.enabled = config.cpa_enabled === '1'
    cpa.url = config.cpa_url || ''
    cpa.key = ''
    cpa.keyPh = config.cpa_mgmt_key === '***' ? '已设置（留空不修改）' : '粘贴 CPA 管理密钥'
    cpa.timeout = Number(config.cpa_timeout || 30)
    sub.enabled = config.sub2api_enabled === '1'
    sub.url = config.sub2api_url || ''
    sub.key = ''
    sub.keyPh = config.sub2api_api_key === '***' ? '已设置（留空不修改）' : '粘贴面板里生成的 x-api-key'
    sub.groupIds = String(config.sub2api_group_ids || '')
      .split(',').map(id => id.trim()).filter(Boolean)
    const savedModels = Array.isArray(config.sub2api_models) ? config.sub2api_models : []
    sub.models = [...new Set([...defaultSub2ApiModels, ...savedModels])]
    sub.concurrency = Number(config.sub2api_concurrency || 3)
    sub.fingerprintMode = config.sub2api_fingerprint_mode || 'session'
    sub.timeout = Number(config.sub2api_timeout || 30)
    team.enabled = config.team_sso_enabled === '1'
    team.url = config.team_sso_url || ''
    team.key = ''
    team.keyPh = config.team_sso_sync_key === '***' ? '已设置（留空不修改）' : '粘贴 team-sso 同步密钥'
    team.timeout = Number(config.team_sso_timeout || 10)
    team.pending = Number(config.team_sso_pending || 0)
    if (sub.url && config.sub2api_api_key === '***') await loadGroups(true)
  } catch (e) { ElMessage.error(e.message) }
}

async function loadGroups(silent = false) {
  loadingGroups.value = true
  try {
    const result = await getSub2ApiGroups({
      sub2api_url: sub.url.trim(),
      sub2api_api_key: sub.key.trim() || '***',
      sub2api_timeout: String(sub.timeout || 30),
    })
    sub.groups = Array.isArray(result.groups) ? result.groups : []
    const activeIds = new Set(sub.groups.map(group => String(group.id)))
    sub.groupIds = sub.groupIds.filter(id => activeIds.has(String(id)))
    if (!silent) ElMessage.success(result.message || '分组已刷新')
  } catch (e) {
    if (!silent) ElMessage.error(e.message)
  } finally {
    loadingGroups.value = false
  }
}

async function save() {
  saving.value = true
  try {
    if (sub.enabled && sub.groupIds.length === 0) {
      ElMessage.warning('请先获取并勾选至少一个 OpenAI 分组')
      return
    }
    await saveExportConfig({
      cpa_enabled: cpa.enabled ? '1' : '0',
      cpa_url: cpa.url.trim(),
      cpa_mgmt_key: cpa.key.trim() || '***',
      cpa_timeout: String(cpa.timeout || 30),
      sub2api_enabled: sub.enabled ? '1' : '0',
      sub2api_url: sub.url.trim(),
      sub2api_api_key: sub.key.trim() || '***',
      sub2api_group_ids: sub.groupIds.join(','),
      sub2api_models: [...new Set(sub.models)],
      sub2api_concurrency: String(sub.concurrency || 3),
      sub2api_fingerprint_mode: sub.fingerprintMode || 'session',
      sub2api_timeout: String(sub.timeout || 30),
      team_sso_enabled: team.enabled ? '1' : '0',
      team_sso_url: team.url.trim(),
      team_sso_sync_key: team.key.trim() || '***',
      team_sso_timeout: String(team.timeout || 10),
    })
    ElMessage.success('保存成功')
    load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function test(target) {
  const flag = target === 'cpa' ? testingCpa : target === 'sub2api' ? testingSub : testingTeam
  flag.value = true
  try { const r = await testExport(target); ElMessage.success(r.message || '连通正常') }
  catch (e) { ElMessage.error(e.message) }
  finally { flag.value = false }
}

onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 760px">
      <template #header>
        <span class="section-title" style="margin: 0">注册完成后自动导出</span>
        <el-tag type="danger" size="small" effect="dark" style="margin-left: 8px">仅带 RT 的账号可用</el-tag>
      </template>
      <p class="hint">勾选启用后，每次注册成功落库会导出到对应面板。没勾选完全不执行，导出失败只记日志、不影响注册。</p>
      <p class="hint" style="color: var(--el-color-danger); font-weight: 600">
        注意：只有带 <b>RT（refresh_token）</b>的账号，导出后才能正常使用。没有 RT 的号推过去面板也用不了 —— 想拿到 RT 需要<b>配置接码</b>。
      </p>

      <el-form label-position="top">
        <el-divider content-position="left">CPA 面板</el-divider>
        <el-form-item>
          <el-checkbox v-model="cpa.enabled">启用 CPA 自动导出（POST /v0/management/auth-files）</el-checkbox>
        </el-form-item>
        <el-form-item label="CPA URL">
          <el-input v-model="cpa.url" placeholder="https://cpa.example.com" />
        </el-form-item>
        <el-form-item label="管理密钥（Authorization Bearer + X-Management-Key）">
          <el-input v-model="cpa.key" type="password" show-password :placeholder="cpa.keyPh" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="cpa.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingCpa" @click="test('cpa')">测试 CPA 连通性</el-button>

        <el-divider content-position="left">SUB2API 面板</el-divider>
        <el-form-item>
          <el-checkbox v-model="sub.enabled">启用 SUB2API / Hub 自动导出（POST /api/v1/admin/accounts/batch）</el-checkbox>
        </el-form-item>
        <el-form-item label="SUB2API URL">
          <el-input v-model="sub.url" placeholder="https://sub2api.example.com" />
        </el-form-item>
        <el-form-item label="API Key（安全与认证-管理员 API Key）">
          <el-input v-model="sub.key" type="password" show-password :placeholder="sub.keyPh" />
        </el-form-item>
        <el-form-item label="目标分组（仅显示 active / openai）">
          <div class="group-picker-head">
            <el-button :icon="Refresh" :loading="loadingGroups" @click="loadGroups()">刷新分组</el-button>
          </div>
          <el-checkbox-group v-if="sub.groups.length" v-model="sub.groupIds" class="group-options">
            <el-checkbox
              v-for="group in sub.groups" :key="group.id" :value="String(group.id)" border
            >
              {{ group.name }} (#{{ group.id }})
            </el-checkbox>
          </el-checkbox-group>
          <el-empty v-else :image-size="44" description="暂无可用分组，请检查 URL 和 API Key 后刷新" />
        </el-form-item>
        <el-form-item label="模型列表（默认全部启用，可输入新模型添加）">
          <el-select
            v-model="sub.models" multiple filterable allow-create default-first-option
            placeholder="输入模型名称后回车添加" style="width: 100%"
          >
            <el-option v-for="model in defaultSub2ApiModels" :key="model" :label="model" :value="model" />
          </el-select>
        </el-form-item>
        <el-form-item label="账号并发数">
          <el-input-number v-model="sub.concurrency" :min="1" :max="1000" />
        </el-form-item>
        <el-form-item label="Codex 指纹收敛">
          <el-select v-model="sub.fingerprintMode" style="width: 280px">
            <el-option
              v-for="mode in fingerprintModes"
              :key="mode.value"
              :label="mode.label"
              :value="mode.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="sub.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingSub" @click="test('sub2api')">测试 SUB2API 连通性</el-button>

        <el-divider content-position="left">team-sso free 账号池</el-divider>
        <el-form-item>
          <el-checkbox v-model="team.enabled">启用 team-sso 自动导出</el-checkbox>
        </el-form-item>
        <el-form-item label="内部导入 URL">
          <el-input v-model="team.url" placeholder="http://host.docker.internal:8088/api/internal/free-accounts/import" />
        </el-form-item>
        <el-form-item label="同步密钥">
          <el-input v-model="team.key" type="password" show-password :placeholder="team.keyPh" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="team.timeout" :min="2" :max="120" />
        </el-form-item>
        <el-button :loading="testingTeam" @click="test('team_sso')">测试 team-sso 连通性</el-button>

      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        CPA {{ cpa.enabled ? '已启用' : '未启用' }} · SUB2API {{ sub.enabled ? '已启用' : '未启用' }} · team-sso {{ team.enabled ? '已启用' : '未启用' }}<span v-if="team.pending"> · 待重试 {{ team.pending }}</span>
      </template>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.group-picker-head {
  display: flex;
  justify-content: flex-end;
  width: 100%;
  margin-bottom: 10px;
}

.group-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
}

.group-options :deep(.el-checkbox.is-bordered) {
  margin: 0;
}
</style>
