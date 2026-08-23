<script setup>
import { computed, onDeactivated, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)
const addVisible = ref(false)

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  addVisible.value = false
  ElMessage.success(`已保存 ${r.kept} 个代理${r.duplicated ? `（去重 ${r.duplicated} 个）` : ''}`)
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  addVisible.value = false
  ElMessage.success(`已追加 ${r.added} 个新代理`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`确定清空全部 ${count.value} 个代理？`, '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
    proxyStore.clear()
    ElMessage.success('已清空')
  } catch (_) { /* cancel */ }
}
function editInDraft() {
  draft.value = proxyStore.text
}
function openAdd() {
  draft.value = ''
  addVisible.value = true
}
onDeactivated(() => { addVisible.value = false })
</script>

<template>
  <div class="page proxy-page">
    <el-card shadow="never" class="proxy-list-card">
          <template #header>
            <div class="panel-header">
              <div class="panel-title-group">
                <h2 class="section-title">当前代理池</h2>
                <p class="section-subtitle">{{ count }} 个代理<template v-if="invalidCount"> · <span class="invalid-text">{{ invalidCount }} 个格式异常</span></template></p>
              </div>
              <div class="panel-tools">
                <el-button type="primary" @click="openAdd"><el-icon><Plus /></el-icon>添加代理</el-button>
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">测试全部</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" label="代理地址" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="格式" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ row.valid ? '正常' : '异常' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="生效协议" width="110">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="连通性" min-width="150">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">未测</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">正常 {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || '连接失败'" placement="top">
                  <el-tag type="danger" size="small">失败</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >测试</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无代理" :image-size="70">
                <el-button type="primary" @click="openAdd"><el-icon><Plus /></el-icon>添加代理</el-button>
              </el-empty>
            </template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="全自动批量跑号时，各 worker 会按顺序轮流取用这里的代理；代理池为空则所有 worker 用「单次注册」页填的单个代理。"
          />
    </el-card>

    <el-dialog v-model="addVisible" width="680px" top="7vh" class="proxy-dialog">
      <template #header>
        <div class="dialog-heading">
          <h2>添加代理</h2>
          <p>每行一个，支持 HTTP 与 SOCKS5</p>
        </div>
      </template>

      <div class="format-guide">
        <span class="guide-icon"><el-icon><Connection /></el-icon></span>
        <div>
          <strong>代理格式</strong>
          <p><code>[协议://][user:pass@]host:port</code></p>
          <span>不写协议时默认使用 HTTP；SOCKS5 必须包含 <code>socks5://</code>。</span>
        </div>
      </div>

      <el-input
        v-model="draft"
        type="textarea"
        :rows="13"
        class="mono proxy-textarea"
        placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
      />

      <template #footer>
        <div class="dialog-footer">
          <el-button text :disabled="!count" @click="editInDraft">载入当前池</el-button>
          <div class="footer-actions">
            <el-button @click="addVisible = false">取消</el-button>
            <el-button :disabled="!draft.trim()" @click="append">追加合并</el-button>
            <el-button type="primary" :disabled="!draft.trim()" @click="save">覆盖保存</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.proxy-list-card { margin: 0; }
.invalid-text { color: var(--el-color-danger); }
.dialog-heading h2 { margin: 0; color: var(--app-title); font-size: 16px; font-weight: 650; }
.dialog-heading p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 11px; }
.format-guide { display: flex; gap: 12px; margin-bottom: 14px; padding: 13px; border: 1px solid var(--app-border); border-radius: 9px; background: var(--el-fill-color-lighter); }
.guide-icon { width: 32px; height: 32px; display: grid; place-items: center; flex: 0 0 auto; color: var(--brand); border-radius: 8px; background: var(--brand-soft); }
.format-guide strong { color: var(--app-title); font-size: 12px; }
.format-guide p { margin: 5px 0 3px; color: var(--app-title); font-size: 11px; }
.format-guide span { color: var(--el-text-color-secondary); font-size: 10px; line-height: 1.5; }
.proxy-textarea :deep(.el-textarea__inner) { min-height: 320px !important; resize: vertical; }
.dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.footer-actions { display: flex; gap: 8px; }
@media (max-width: 680px) {
  .panel-tools { width: 100%; }
  .panel-tools .el-button { flex: 1 1 auto; }
  .dialog-footer { align-items: stretch; flex-direction: column; }
  .footer-actions { justify-content: flex-end; }
}
</style>
