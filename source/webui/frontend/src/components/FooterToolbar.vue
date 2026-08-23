<script setup>
// 内容区底部悬浮操作条（Ant Pro FooterToolbar 风格）：滚到哪都能保存。
</script>

<template>
  <div class="footer-toolbar">
    <div class="ft-left"><slot name="left" /></div>
    <div class="ft-right"><slot /></div>
  </div>
</template>

<style scoped>
.footer-toolbar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: min(100%, 760px);
  margin: 16px 0 0;
  padding: 11px 13px;
  background: color-mix(in srgb, var(--app-elevated-bg) 92%, transparent);
  border: 1px solid var(--app-border);
  border-radius: 10px;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.07);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
}
.ft-left { color: var(--el-text-color-secondary); font-size: 12px; }
.ft-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
@media (max-width: 640px) {
  .footer-toolbar { flex-wrap: wrap; padding: 10px 12px; }
  .ft-left { flex: 1 1 100%; }
  .ft-right { width: 100%; justify-content: flex-end; }
}
</style>
