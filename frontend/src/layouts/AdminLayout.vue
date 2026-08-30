<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useThemeStore } from '@/stores/theme'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import { copyText } from '@/api/request'

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

function shareWorkspace() {
  copyText(window.location.href)
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
  <el-container class="admin-shell console-shell">
    <div v-if="isMobile && mobileOpen" class="sidebar-backdrop" @click="mobileOpen = false" />
    <el-aside
      :width="isMobile ? '246px' : (collapse ? '56px' : '232px')"
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

      <div v-if="!collapse || isMobile" class="sidebar-tools">
        <el-select
          v-model="search"
          filterable
          clearable
          placeholder="搜索与快捷操作"
          class="workspace-command"
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
        <div class="topbar-row">
          <div class="page-context">
            <el-button v-if="isMobile" class="mobile-menu" text circle @click="toggleSidebar">
              <el-icon :size="18"><Menu /></el-icon>
            </el-button>
            <el-icon class="context-icon" :size="16"><component :is="route.meta.icon" /></el-icon>
            <div class="page-heading">
              <h1>{{ pageTitle }}</h1>
              <p>{{ pageDescription }}</p>
            </div>
          </div>

          <div class="top-actions">
            <span class="scope-badge">本地工作区</span>
            <el-button class="share-button" text @click="shareWorkspace">
              <el-icon :size="15"><Share /></el-icon>分享
            </el-button>
            <i class="top-divider" />
            <el-tooltip :content="theme.dark ? '切换浅色模式' : '切换深色模式'">
              <el-button class="icon-action" circle @click="theme.toggle">
                <el-icon :size="17"><Moon v-if="!theme.dark" /><Sunny v-else /></el-icon>
            </el-button>
          </el-tooltip>
          <el-dropdown>
            <button class="profile-button" aria-label="账户菜单">
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
.admin-shell { height: 100vh; min-height: 100vh; overflow: hidden; padding: 8px; background: #f2f2f4; }
.workspace { width: auto; min-width: 0; height: calc(100vh - 16px); min-height: 0; overflow: hidden; border: 1px solid var(--app-border); border-radius: 0 13px 13px 0; background: var(--app-content-bg); }
.sidebar {
  position: relative;
  z-index: 20;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 16px);
  min-height: 0;
  background: var(--app-sidebar-bg);
  border: 1px solid var(--app-border);
  border-right: 0;
  border-radius: 13px 0 0 13px;
  padding: 0;
  overflow: hidden;
  transition: width 0.24s cubic-bezier(0.4, 0, 0.2, 1);
}
.brand {
  height: 50px;
  min-height: 50px;
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  padding: 0 14px;
  border-bottom: 1px solid var(--app-border);
  overflow: hidden;
}
.brand.mini { justify-content: center; padding-inline: 0; }
.brand-mark {
  width: 23px;
  height: 23px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  color: #fff;
  background: #202124;
  box-shadow: none;
}
.brand-mark.interactive { cursor: pointer; }
.brand-copy { min-width: 0; display: flex; flex-direction: column; line-height: 1.2; }
.brand-copy strong { color: var(--app-title); font-size: 14px; font-weight: 700; white-space: nowrap; }
.brand-copy span { margin-top: 2px; color: var(--app-sidebar-muted); font-size: 9px; }
.role-chip { min-width: 0; flex: 0 0 auto; padding: 3px 6px; border: 1px solid #d7ddf6; border-radius: 6px; color: #596ab2; background: #f3f5ff; font-size: 8px; font-weight: 600; }
.collapse-button { margin-left: auto; color: var(--el-text-color-secondary); }
.brand.mini .collapse-button { margin: 0; }
.sidebar-tools {
  height: 48px;
  flex: 0 0 48px;
  display: flex;
  align-items: center;
  padding: 6px 10px;
}
.workspace-command { width: 100%; }
.workspace-command :deep(.el-select__wrapper) {
  min-height: 34px;
  height: 34px;
  border-color: transparent !important;
  border-radius: 7px;
  background: transparent;
  box-shadow: none !important;
}
.workspace-command :deep(.el-select__wrapper:hover),
.workspace-command :deep(.el-select__wrapper.is-focused) {
  border-color: var(--app-border) !important;
  background: var(--app-elevated-bg);
  box-shadow: none !important;
}
.workspace-command :deep(.el-select__placeholder),
.workspace-command :deep(.el-select__selected-item),
.workspace-command :deep(.el-icon) { color: var(--app-sidebar-text); font-size: 10px; }
.nav-scroll { flex: 1; min-height: 0; }
.side-menu { border-right: 0; background: transparent; padding: 5px 13px 12px; }
.sidebar-status {
  margin: 0;
  padding: 10px 13px 13px;
  border: 1px solid var(--app-border);
  border-right: 0;
  border-bottom: 0;
  border-left: 0;
  border-radius: 0;
  background: transparent;
}
.sidebar-status.compact { display: grid; place-items: center; margin-inline: 0; padding: 6px; }
.status-heading, .status-meta { display: flex; align-items: center; justify-content: space-between; }
.status-heading { font-size: 10px; color: var(--app-sidebar-muted); }
.status-heading strong { color: var(--app-title); font-size: 12px; }
.status-track { height: 4px; margin: 11px 0 9px; overflow: hidden; border-radius: 999px; background: var(--el-fill-color-darker); }
.status-track span { display: block; height: 100%; min-width: 3px; border-radius: inherit; background: var(--brand); transition: width .3s ease; }
.status-meta { gap: 8px; font-size: 9px; color: var(--app-sidebar-muted); }
.status-meta span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.dot { width: 6px; height: 6px; display: inline-block; border-radius: 50%; }
.dot.available { background: var(--el-color-success); box-shadow: 0 0 0 3px var(--el-color-success-light-9); }
.topbar {
  height: 50px;
  min-height: 50px;
  display: flex;
  align-items: center;
  padding: 0 14px;
  background: var(--app-header-bg);
  border-bottom: 1px solid var(--app-border);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}
.topbar-row { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 15px; }
.page-context, .top-actions { display: flex; align-items: center; min-width: 0; }
.page-context { gap: 9px; color: var(--app-sidebar-muted); }
.context-icon { color: var(--app-sidebar-text); flex: 0 0 auto; }
.page-heading { min-width: 0; }
.page-heading h1 { margin: 0; overflow: hidden; color: var(--app-title); font-size: 14px; font-weight: 600; line-height: 1.25; letter-spacing: 0; text-overflow: ellipsis; white-space: nowrap; }
.page-heading p { display: none; }
.top-actions { gap: 6px; flex: 0 0 auto; }
.scope-badge { min-height: 25px; display: inline-flex; align-items: center; padding: 0 8px; border: 1px solid #d7ddf6; border-radius: 7px; background: #f3f5ff; color: #596ab2; font-size: 9px; font-weight: 600; }
.share-button { height: 31px; padding-inline: 7px; color: var(--app-sidebar-text); font-size: 10px; }
.share-button:hover { color: var(--app-title); background: var(--app-hover-bg); }
.top-divider { width: 1px; height: 25px; margin: 0 5px; background: var(--app-border); }
.icon-action { border-color: var(--app-border); background: transparent; }
.profile-button {
  height: 34px;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px;
  color: var(--app-title);
  border: 0;
  border-radius: 7px;
  background: transparent;
  cursor: pointer;
}
.profile-button:hover { background: var(--el-fill-color-light); }
.profile-avatar { width: 28px; height: 28px; color: var(--brand); background: var(--brand-soft); }
.profile-copy, .profile-button > .el-icon { display: none; }
.content { min-height: 0; padding: 12px 18px 35px; overflow-x: hidden; overflow-y: auto; background: var(--app-content-bg); }
.mobile-menu { margin-right: 8px; }
.sidebar-backdrop { position: fixed; inset: 0; z-index: 19; background: rgba(0, 0, 0, .32); backdrop-filter: blur(2px); }
:global(html.dark) .role-chip,
:global(html.dark) .scope-badge { border-color: rgba(127, 159, 241, .32); background: rgba(127, 159, 241, .14); color: #9eb5f4; }

@media (max-width: 1080px) {
  .profile-copy { display: none; }
}
@media (max-width: 820px) {
  .admin-shell { padding: 0; }
  .workspace { height: 100vh; border: 0; border-radius: 0; }
  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 20;
    transform: translateX(-104%);
    transition: transform .24s cubic-bezier(.4,0,.2,1);
    box-shadow: 18px 0 48px rgba(0, 0, 0, .16);
    height: 100vh;
    border-radius: 0 13px 13px 0;
  }
  .sidebar.mobile-open { transform: translateX(0); }
  .topbar { height: 64px; min-height: 64px; padding: 0 14px; }
  .top-divider, .scope-badge, .share-button { display: none; }
  .profile-button { display: none; }
  .content { padding: 14px 10px 40px; }
}
</style>
