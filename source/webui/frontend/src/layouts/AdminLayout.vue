<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useThemeStore } from '@/stores/theme'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'

const route = useRoute()
const router = useRouter()
const theme = useThemeStore()
const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { stats } = storeToRefs(statsStore)

const collapse = ref(false)
const isMobile = ref(false)
const mobileOpen = ref(false)
const search = ref('')

const PAGE_DESCRIPTIONS = {
  '/': '掌握账号池状态与当前任务进度',
  '/register': '选择指定邮箱并执行一次完整注册',
  '/auto': '配置并管理持续运行的批量任务',
  '/proxy': '维护注册流程使用的代理资源',
  '/pool': '查看和管理待注册邮箱账号',
  '/registered': '检查、重授权、导出并维护账号凭证',
  '/team': '管理 Team 席位、成员额度与自动补位',
  '/runs': '回顾任务执行历史与结果',
  '/settings/mail': '配置接码邮箱服务与连接参数',
  '/settings/sms': '配置手机号接码服务与策略',
  '/settings/export': '设置注册完成后的自动导出规则',
}

function updateViewport() {
  isMobile.value = window.innerWidth <= 820
  if (!isMobile.value) mobileOpen.value = false
}

function toggleSidebar() {
  if (isMobile.value) mobileOpen.value = !mobileOpen.value
  else collapse.value = !collapse.value
}

function onMenuSelect() {
  if (isMobile.value) mobileOpen.value = false
}

const GROUP_ORDER = ['概览', '注册', '运营', '数据', '配置']
const groups = computed(() => {
  const map = {}
  for (const r of router.getRoutes()) {
    if (!r.meta?.title) continue
    const group = r.meta.group || '其他'
    ;(map[group] ||= []).push(r)
  }
  return GROUP_ORDER.filter((group) => map[group]).map((group) => ({ name: group, items: map[group] }))
})

const activeMenu = computed(() => route.path)
const pageTitle = computed(() => route.meta.title || 'Outlook Register')
const pageDescription = computed(() => PAGE_DESCRIPTIONS[route.path] || '')
const menuOptions = computed(() =>
  router.getRoutes()
    .filter((r) => r.meta?.title)
    .map((r) => ({ value: r.path, label: r.meta.title, group: r.meta.group })),
)
const completionRate = computed(() => {
  const total = Number(stats.value.total) || 0
  return total ? Math.round(((Number(stats.value.done) || 0) / total) * 100) : 0
})

function onSearch(path) {
  if (path) router.push(path)
  search.value = ''
}

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
  theme.apply()
  statsStore.startPolling()
  runtime.connectAutoStream()
})
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))
</script>

<template>
  <el-container class="admin-shell">
    <div v-if="isMobile && mobileOpen" class="sidebar-backdrop" @click="mobileOpen = false" />
    <el-aside
      :width="isMobile ? '260px' : (collapse ? '76px' : '248px')"
      class="sidebar"
      :class="{ 'mobile-open': mobileOpen, 'is-collapsed': collapse && !isMobile }"
    >
      <div class="brand" :class="{ mini: collapse && !isMobile }">
        <el-tooltip :disabled="!collapse || isMobile" content="展开侧栏" placement="right">
          <span class="brand-mark" :class="{ interactive: collapse && !isMobile }" @click="collapse && !isMobile && toggleSidebar()">
            <el-icon :size="19"><Platform /></el-icon>
          </span>
        </el-tooltip>
        <div v-if="!collapse || isMobile" class="brand-copy">
          <strong>Outlook Register</strong>
          <span>Workspace</span>
        </div>
        <el-tooltip v-if="!isMobile && !collapse" content="收起侧栏" placement="right">
          <el-button class="collapse-button" text circle @click="toggleSidebar">
            <el-icon :size="16"><Expand v-if="collapse" /><Fold v-else /></el-icon>
          </el-button>
        </el-tooltip>
      </div>

      <el-scrollbar class="nav-scroll">
        <el-menu
          :default-active="activeMenu"
          router
          :collapse="!isMobile && collapse"
          class="side-menu"
          @select="onMenuSelect"
        >
          <el-menu-item-group v-for="group in groups" :key="group.name" :title="collapse && !isMobile ? '' : group.name">
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              <el-icon><component :is="item.meta.icon" /></el-icon>
              <template #title>{{ item.meta.title }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>
      </el-scrollbar>

      <div class="sidebar-status" :class="{ compact: collapse && !isMobile }">
        <template v-if="!collapse || isMobile">
          <div class="status-heading">
            <span>邮箱池</span>
            <strong>{{ stats.total || 0 }}</strong>
          </div>
          <div class="status-track"><span :style="{ width: completionRate + '%' }" /></div>
          <div class="status-meta">
            <span><i class="dot available" />可用 {{ stats.available || 0 }}</span>
            <span>完成 {{ completionRate }}%</span>
          </div>
        </template>
        <el-tooltip v-else content="查看邮箱列表" placement="right">
          <el-button text circle @click="router.push('/pool')">
            <el-icon :size="18"><Files /></el-icon>
          </el-button>
        </el-tooltip>
      </div>
    </el-aside>

    <el-container class="workspace">
      <el-header class="topbar">
        <div class="page-context">
          <el-button v-if="isMobile" class="mobile-menu" text circle @click="toggleSidebar">
            <el-icon :size="20"><Menu /></el-icon>
          </el-button>
          <div class="page-heading">
            <h1>{{ pageTitle }}</h1>
            <p>{{ pageDescription }}</p>
          </div>
        </div>

        <div class="top-actions">
          <el-select
            v-model="search"
            filterable
            clearable
            placeholder="搜索页面"
            class="global-search"
            @change="onSearch"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
            <el-option
              v-for="option in menuOptions"
              :key="option.value"
              :label="`${option.label} · ${option.group}`"
              :value="option.value"
            />
          </el-select>
          <el-tooltip :content="theme.dark ? '切换浅色模式' : '切换深色模式'">
            <el-button class="icon-action" circle @click="theme.toggle">
              <el-icon :size="17"><Moon v-if="!theme.dark" /><Sunny v-else /></el-icon>
            </el-button>
          </el-tooltip>
          <el-dropdown>
            <button class="profile-button">
              <el-avatar :size="32" class="profile-avatar"><el-icon><User /></el-icon></el-avatar>
              <span class="profile-copy"><strong>管理员</strong><small>本地工作区</small></span>
              <el-icon :size="13"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="theme.toggle">
                  {{ theme.dark ? '使用浅色外观' : '使用深色外观' }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="content">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.admin-shell { height: 100vh; background: var(--app-content-bg); }
.workspace { min-width: 0; }
.sidebar {
  position: relative;
  z-index: 20;
  display: flex;
  flex-direction: column;
  background: var(--app-sidebar-bg);
  border-right: 1px solid var(--app-border);
  transition: width 0.24s cubic-bezier(0.4, 0, 0.2, 1);
}
.brand {
  min-height: 76px;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 14px 14px 12px 18px;
  overflow: hidden;
}
.brand.mini { justify-content: center; padding-inline: 10px; }
.brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  color: #fff;
  background: linear-gradient(145deg, #3b8cff, #0867e8);
  box-shadow: 0 7px 18px rgba(10, 105, 229, 0.22), inset 0 1px rgba(255,255,255,.28);
}
.brand-mark.interactive { cursor: pointer; }
.brand-copy { min-width: 0; display: flex; flex-direction: column; line-height: 1.2; }
.brand-copy strong { color: var(--app-title); font-size: 14px; font-weight: 650; white-space: nowrap; }
.brand-copy span { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 11px; }
.collapse-button { margin-left: auto; color: var(--el-text-color-secondary); }
.brand.mini .collapse-button { margin: 0; }
.nav-scroll { flex: 1; min-height: 0; }
.side-menu { border-right: 0; background: transparent; padding: 2px 10px 12px; }
.sidebar-status {
  margin: 10px 12px 14px;
  padding: 13px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-elevated-bg);
}
.sidebar-status.compact { display: grid; place-items: center; margin-inline: 10px; padding: 6px; }
.status-heading, .status-meta { display: flex; align-items: center; justify-content: space-between; }
.status-heading { font-size: 12px; color: var(--el-text-color-secondary); }
.status-heading strong { color: var(--app-title); font-size: 13px; }
.status-track { height: 4px; margin: 11px 0 9px; overflow: hidden; border-radius: 999px; background: var(--el-fill-color-darker); }
.status-track span { display: block; height: 100%; min-width: 3px; border-radius: inherit; background: var(--brand); transition: width .3s ease; }
.status-meta { gap: 8px; font-size: 10px; color: var(--el-text-color-secondary); }
.status-meta span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.dot { width: 6px; height: 6px; display: inline-block; border-radius: 50%; }
.dot.available { background: var(--el-color-success); box-shadow: 0 0 0 3px var(--el-color-success-light-9); }
.topbar {
  height: 76px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 28px;
  background: color-mix(in srgb, var(--app-header-bg) 90%, transparent);
  border-bottom: 1px solid var(--app-border);
  backdrop-filter: saturate(180%) blur(18px);
  -webkit-backdrop-filter: saturate(180%) blur(18px);
}
.page-context, .top-actions { display: flex; align-items: center; min-width: 0; }
.page-heading { min-width: 0; }
.page-heading h1 { margin: 0; color: var(--app-title); font-size: 19px; font-weight: 650; line-height: 1.25; letter-spacing: 0; }
.page-heading p { margin: 4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; white-space: nowrap; }
.top-actions { gap: 9px; flex: 0 0 auto; }
.global-search { width: 220px; }
.icon-action { border-color: var(--app-border); background: var(--app-elevated-bg); }
.profile-button {
  height: 42px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 4px 8px 4px 5px;
  color: var(--app-title);
  border: 0;
  border-radius: 9px;
  background: transparent;
  cursor: pointer;
}
.profile-button:hover { background: var(--el-fill-color-light); }
.profile-avatar { color: var(--brand); background: var(--brand-soft); }
.profile-copy { display: flex; flex-direction: column; align-items: flex-start; line-height: 1.15; }
.profile-copy strong { font-size: 12px; font-weight: 600; }
.profile-copy small { margin-top: 3px; color: var(--el-text-color-secondary); font-size: 10px; }
.content { padding: 20px 28px 32px; overflow-x: hidden; background: var(--app-content-bg); }
.mobile-menu { margin-right: 8px; }
.sidebar-backdrop { position: fixed; inset: 0; z-index: 19; background: rgba(0, 0, 0, .32); backdrop-filter: blur(2px); }

@media (max-width: 1080px) {
  .profile-copy { display: none; }
  .global-search { width: 190px; }
}
@media (max-width: 820px) {
  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 20;
    transform: translateX(-104%);
    transition: transform .24s cubic-bezier(.4,0,.2,1);
    box-shadow: 18px 0 48px rgba(0, 0, 0, .16);
  }
  .sidebar.mobile-open { transform: translateX(0); }
  .topbar { height: 68px; padding: 0 16px; }
  .page-heading p { display: none; }
  .page-heading h1 { font-size: 17px; }
  .global-search { width: 40px; }
  .global-search :deep(.el-select__wrapper) { padding-inline: 11px; }
  .global-search :deep(.el-select__placeholder),
  .global-search :deep(.el-select__selection) { display: none; }
  .profile-button { display: none; }
  .content { padding: 14px 14px 24px; }
}
</style>
