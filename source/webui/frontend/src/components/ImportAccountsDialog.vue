<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { importAccounts } from '@/api/accounts'
import { getMailProviders } from '@/api/settings'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'imported'])

const providers = ref([])
const kind = ref('')
const text = ref('')
const loading = ref(false)
const result = ref('')
const errors = ref([])

const current = computed(() => providers.value.find((provider) => provider.kind === kind.value) || null)
const lineCount = computed(() =>
  text.value.split('\n').filter((line) => line.trim() && !line.trim().startsWith('#')).length,
)

function close() {
  emit('update:modelValue', false)
}

async function loadProviders() {
  try {
    const response = await getMailProviders(true)
    providers.value = response.providers || []
    const selected = response.current
    kind.value = providers.value.some((provider) => provider.kind === selected)
      ? selected
      : (providers.value[0]?.kind || '')
  } catch (error) {
    ElMessage.error(error.message)
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    result.value = ''
    errors.value = []
    loadProviders()
  },
)

async function submit() {
  if (!text.value.trim()) {
    ElMessage.warning('请输入要添加的邮箱账号')
    return
  }
  if (!kind.value) {
    ElMessage.warning('请先选择邮箱来源')
    return
  }

  loading.value = true
  result.value = ''
  errors.value = []
  try {
    const response = await importAccounts(text.value.trim(), kind.value)
    result.value = `解析 ${response.parsed} 行，新增 ${response.inserted}，更新 ${response.updated}，跳过 ${response.skipped}`
    ElMessage.success(result.value)
    text.value = ''
    emit('imported', response)
    close()
  } catch (error) {
    if (error.status === 422 && error.data?.errors?.length) {
      errors.value = error.data.errors
      result.value = `有 ${errors.value.length} 行不合法，整批未添加`
      ElMessage.error('添加失败，请修正标记行后重试')
    } else {
      result.value = '添加失败: ' + error.message
      ElMessage.error(error.message)
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    width="900px"
    top="7vh"
    class="import-dialog"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="dialog-heading">
        <div>
          <h2>添加邮箱</h2>
          <p>整批校验通过后才会写入账号池</p>
        </div>
        <span v-if="lineCount" class="line-counter">{{ lineCount }} 个账号</span>
      </div>
    </template>

    <div class="import-grid">
      <aside class="import-options">
        <el-form label-position="top">
          <el-form-item label="邮箱来源">
            <el-select v-model="kind" placeholder="请选择来源">
              <el-option
                v-for="provider in providers"
                :key="provider.kind"
                :label="provider.display_name"
                :value="provider.kind"
              />
            </el-select>
          </el-form-item>
        </el-form>

        <div v-if="current" class="format-guide">
          <span class="guide-icon"><el-icon><Document /></el-icon></span>
          <strong>输入格式</strong>
          <p>每行一个账号，共 {{ current.line_segments }} 段，使用 <code>----</code> 分隔。</p>
          <code class="format-code">{{ current.import_hint || '' }}</code>
        </div>
        <p class="source-note">不同来源可能具有相同段数，请确认来源后再添加。</p>
      </aside>

      <section class="import-editor">
        <el-input
          v-model="text"
          type="textarea"
          :rows="14"
          class="mono import-textarea"
          :placeholder="current?.import_placeholder || ''"
        />
      </section>
    </div>

    <el-alert
      v-if="errors.length"
      type="error"
      :closable="true"
      show-icon
      class="error-alert"
      title="以下行不合法，账号池未被修改"
      @close="errors = []"
    >
      <ul class="error-list">
        <li v-for="error in errors" :key="error.line">
          <b>第 {{ error.line }} 行</b>：{{ error.error }}
        </li>
      </ul>
    </el-alert>

    <template #footer>
      <div class="dialog-footer">
        <span class="hint">{{ result }}</span>
        <div class="footer-actions">
          <el-button @click="close">取消</el-button>
          <el-button type="primary" :loading="loading" :disabled="!text.trim()" @click="submit">
            <el-icon><Upload /></el-icon>添加到邮箱列表
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-right: 28px; }
.dialog-heading h2 { margin: 0; color: var(--app-title); font-size: 16px; font-weight: 650; }
.dialog-heading p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 11px; }
.line-counter { padding: 5px 9px; color: var(--brand); border-radius: 7px; background: var(--brand-soft); font-size: 11px; font-weight: 600; white-space: nowrap; }
.import-grid { display: grid; grid-template-columns: 245px minmax(0, 1fr); gap: 20px; }
.import-options { padding-right: 20px; border-right: 1px solid var(--app-border); }
.import-options .el-select { width: 100%; }
.format-guide { padding: 14px; border: 1px solid var(--app-border); border-radius: 9px; background: var(--el-fill-color-lighter); }
.guide-icon { width: 30px; height: 30px; display: grid; place-items: center; margin-bottom: 12px; color: var(--brand); border-radius: 8px; background: var(--brand-soft); }
.format-guide strong { color: var(--app-title); font-size: 12px; }
.format-guide p, .source-note { color: var(--el-text-color-secondary); font-size: 11px; line-height: 1.6; }
.format-guide p { margin: 7px 0 9px; }
.format-code { display: block; color: var(--app-title); font-size: 10px; line-height: 1.55; overflow-wrap: anywhere; }
.source-note { margin: 12px 2px 0; }
.import-editor { min-width: 0; }
.import-textarea :deep(.el-textarea__inner) { min-height: 348px !important; resize: vertical; }
.error-alert { margin-top: 14px; }
.error-list { max-height: 150px; margin: 6px 0 0; padding-left: 18px; overflow-y: auto; line-height: 1.7; }
.dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.dialog-footer .hint { flex: 1; text-align: left; }
.footer-actions { display: flex; gap: 8px; }
@media (max-width: 720px) {
  .import-grid { grid-template-columns: 1fr; }
  .import-options { padding: 0 0 14px; border-right: 0; border-bottom: 1px solid var(--app-border); }
  .dialog-footer { align-items: stretch; flex-direction: column; }
  .footer-actions { justify-content: flex-end; }
}
</style>
