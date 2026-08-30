<script setup>
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const statsStore = useStatsStore()
const { stats } = storeToRefs(statsStore)
const { autoStatus } = storeToRefs(useRuntimeStore())

const cards = computed(() => [
  { label: '账号总数', value: stats.value.total, icon: 'Files', tone: 'blue' },
  { label: '当前可用', value: stats.value.available, icon: 'CircleCheck', tone: 'green' },
  { label: '注册中', value: stats.value.in_use, icon: 'Loading', tone: 'orange' },
  { label: '注册完成', value: stats.value.done, icon: 'Select', tone: 'cyan' },
  { label: '失败', value: stats.value.failed, icon: 'CircleClose', tone: 'red' },
])

const actions = [
  { title: '邮箱列表', detail: '添加并管理接码邮箱账号', icon: 'Files', path: '/pool' },
  { title: '单次注册', detail: '指定一个账号立即执行', icon: 'VideoPlay', path: '/register' },
  { title: '全自动批量', detail: '配置并启动持续任务', icon: 'MagicStick', path: '/auto' },
  { title: '账号池', detail: '检查、重授权并批量导出凭证', icon: 'CircleCheck', path: '/registered' },
]

const autoStateLabel = computed(() => ({
  stopped: '未运行', running: '运行中', paused: '已暂停',
}[autoStatus.value.state] || autoStatus.value.state))
const autoStateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[autoStatus.value.state] || 'info'))
const targetProgress = computed(() => {
  const target = Number(autoStatus.value.target_count) || 0
  if (!target) return 0
  return Math.min(100, Math.round(((Number(autoStatus.value.registered_ok) || 0) / target) * 100))
})
</script>

<template>
  <div class="page dashboard-page">
    <section class="workspace-heading">
      <div>
        <span class="workspace-eyebrow"><i />WORKSPACE OVERVIEW</span>
        <h2>注册工作台</h2>
        <p>账号资源、注册任务与接码状态</p>
      </div>
      <el-button class="heading-refresh" text circle aria-label="刷新数据" @click="statsStore.refresh">
        <el-icon :size="16"><Refresh /></el-icon>
      </el-button>
    </section>
    <section class="stat-grid" aria-label="账号统计">
      <article v-for="card in cards" :key="card.label" class="metric-card" :class="`tone-${card.tone}`">
        <div class="metric-icon"><el-icon :size="18"><component :is="card.icon" /></el-icon></div>
        <div class="metric-content">
          <strong>{{ card.value || 0 }}</strong>
          <span>{{ card.label }}</span>
        </div>
      </article>
    </section>

    <section class="dashboard-grid">
      <el-card shadow="never" class="runtime-card">
        <template #header>
          <div class="panel-header">
            <div class="panel-title-group">
              <h2 class="section-title">自动任务</h2>
              <p class="section-subtitle">当前批量注册任务的实时概况</p>
            </div>
            <StatusDot :type="autoStateType" :text="autoStateLabel" />
          </div>
        </template>

        <div class="runtime-summary">
          <div class="runtime-primary">
            <span>本轮成功</span>
            <strong>{{ autoStatus.registered_ok || 0 }}</strong>
            <small v-if="autoStatus.target_count">目标 {{ autoStatus.target_count }}</small>
            <small v-else>未设置目标数量</small>
          </div>
          <div class="runtime-stats">
            <div><span>失败</span><strong>{{ autoStatus.registered_fail || 0 }}</strong></div>
            <div><span>并发</span><strong>{{ autoStatus.concurrency || 1 }}</strong></div>
            <div><span>效率/分钟</span><strong>{{ Number(autoStatus.success_per_minute || 0).toFixed(1) }}</strong></div>
          </div>
        </div>

        <div class="progress-block">
          <div class="progress-label">
            <span>任务进度</span>
            <b>{{ autoStatus.target_count ? `${targetProgress}%` : '持续运行' }}</b>
          </div>
          <el-progress :percentage="autoStatus.target_count ? targetProgress : 100" :show-text="false" :indeterminate="!autoStatus.target_count && autoStatus.state === 'running'" />
        </div>

        <el-button type="primary" @click="router.push('/auto')">
          管理自动任务<el-icon class="el-icon--right"><ArrowRight /></el-icon>
        </el-button>
      </el-card>

      <el-card shadow="never" class="actions-card">
        <template #header>
          <div class="panel-title-group">
            <h2 class="section-title">快捷操作</h2>
            <p class="section-subtitle">从常用工作流快速开始</p>
          </div>
        </template>
        <div class="action-list">
          <button v-for="action in actions" :key="action.path" class="action-row" @click="router.push(action.path)">
            <span class="action-icon"><el-icon :size="17"><component :is="action.icon" /></el-icon></span>
            <span class="action-copy"><strong>{{ action.title }}</strong><small>{{ action.detail }}</small></span>
            <el-icon class="action-arrow"><ArrowRight /></el-icon>
          </button>
        </div>
      </el-card>
    </section>
  </div>
</template>

<style scoped>
.dashboard-page { display: flex; flex-direction: column; gap: 12px; }
.workspace-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; padding: 8px 2px 3px; }
.workspace-eyebrow { display: inline-flex; align-items: center; gap: 6px; color: #8b8b8b; font-size: 9px; font-weight: 650; letter-spacing: .08em; }
.workspace-eyebrow i { width: 6px; height: 6px; border-radius: 50%; background: #2e8064; }
.workspace-heading h2 { margin: 6px 0 0; color: var(--app-title); font-size: 25px; font-weight: 650; letter-spacing: -.025em; }
.workspace-heading p { margin: 5px 0 0; color: var(--app-sidebar-muted); font-size: 12px; }
.heading-refresh { color: var(--app-sidebar-muted); }
.heading-refresh:hover { color: var(--app-title); background: var(--app-hover-bg); }
.stat-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0;
  overflow: hidden;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-elevated-bg);
}
.metric-card {
  min-width: 0;
  min-height: 98px;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 15px 17px;
  border: 0;
  border-right: 1px solid var(--app-border);
  border-radius: 0;
  background: var(--app-elevated-bg);
  box-shadow: none;
}
.metric-card:last-child { border-right: 0; }
.metric-icon { width: 29px; height: 29px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 5px; }
.metric-content { min-width: 0; display: flex; flex-direction: column; }
.metric-content strong { color: var(--app-title); font-size: 24px; font-weight: 650; line-height: 1; font-variant-numeric: tabular-nums; }
.metric-content span { margin-top: 7px; color: var(--app-sidebar-muted); font-size: 10px; }
.tone-blue .metric-icon { color: #5270bf; background: var(--app-icon-bg); }
.tone-green .metric-icon { color: #3d8a77; background: var(--app-icon-bg); }
.tone-orange .metric-icon { color: #ae7b35; background: var(--app-icon-bg); }
.tone-cyan .metric-icon { color: #4c8596; background: var(--app-icon-bg); }
.tone-red .metric-icon { color: #b4645f; background: var(--app-icon-bg); }
:global(html.dark) .metric-icon { background: var(--app-icon-bg); }
.dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(320px, .8fr); gap: 12px; }
.dashboard-grid .el-card { margin: 0; }
.runtime-summary { display: grid; grid-template-columns: minmax(160px, .75fr) 1.5fr; gap: 18px; align-items: stretch; }
.runtime-primary { padding: 16px; border-radius: 9px; background: var(--el-fill-color-lighter); }
.runtime-primary span, .runtime-primary small { display: block; color: var(--el-text-color-secondary); font-size: 11px; }
.runtime-primary strong { display: block; margin: 7px 0; color: var(--app-title); font-size: 34px; font-weight: 680; line-height: 1; }
.runtime-stats { display: grid; grid-template-columns: repeat(3, 1fr); align-items: center; border: 1px solid var(--app-border); border-radius: 9px; }
.runtime-stats div { min-width: 0; padding: 12px 15px; border-right: 1px solid var(--app-border); }
.runtime-stats div:last-child { border-right: 0; }
.runtime-stats span, .runtime-stats strong { display: block; }
.runtime-stats span { color: var(--el-text-color-secondary); font-size: 10px; white-space: nowrap; }
.runtime-stats strong { margin-top: 7px; color: var(--app-title); font-size: 18px; font-weight: 650; }
.progress-block { margin: 19px 0 18px; }
.progress-label { display: flex; justify-content: space-between; margin-bottom: 8px; color: var(--el-text-color-secondary); font-size: 11px; }
.progress-label b { color: var(--app-title); font-weight: 600; }
.action-list { display: flex; flex-direction: column; margin: -5px 0; }
.action-row { width: 100%; display: flex; align-items: center; gap: 12px; padding: 11px 5px; color: inherit; text-align: left; border: 0; border-bottom: 1px solid var(--app-border); background: transparent; cursor: pointer; }
.action-row:last-child { border-bottom: 0; }
.action-row:hover .action-icon { color: #fff; background: var(--brand); }
.action-icon { width: 34px; height: 34px; display: grid; place-items: center; flex: 0 0 auto; color: var(--brand); border-radius: 8px; background: var(--brand-soft); transition: .16s ease; }
.action-copy { min-width: 0; display: flex; flex: 1; flex-direction: column; }
.action-copy strong { color: var(--app-title); font-size: 12px; font-weight: 600; }
.action-copy small { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 10px; }
.action-arrow { color: var(--el-text-color-placeholder); }
@media (max-width: 1180px) { .stat-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 900px) { .dashboard-grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) {
  .workspace-heading { align-items: flex-start; }
  .workspace-heading h2 { font-size: 22px; }
  .workspace-heading p { font-size: 11px; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .metric-card { min-height: 88px; padding: 13px 12px; gap: 10px; }
  .metric-icon { width: 27px; height: 27px; }
  .metric-content strong { font-size: 22px; }
  .runtime-summary { grid-template-columns: 1fr; }
  .runtime-stats div { padding-inline: 10px; }
}
</style>
