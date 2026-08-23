<script setup>
import { nextTick, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRuntimeStore } from '@/stores/runtime'

const runtime = useRuntimeStore()
const { logs } = storeToRefs(runtime)
const boxRef = ref(null)

// 新日志自动滚到底
watch(
  () => logs.value.length,
  async () => {
    await nextTick()
    const el = boxRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<template>
  <div class="log-wrap">
    <div class="log-head">
      <div>
        <h2 class="section-title">实时日志</h2>
        <p class="section-subtitle">注册流程的实时事件与错误输出</p>
      </div>
      <el-button size="small" text :disabled="!logs.length" @click="runtime.clearLogs">清空</el-button>
    </div>
    <div ref="boxRef" class="log-box">
      <div v-for="l in logs" :key="l.id" class="line" :class="l.kind">{{ l.text }}</div>
      <div v-if="!logs.length" class="line" style="color: #8a7">等待日志输出…</div>
    </div>
  </div>
</template>

<style scoped>
.log-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
</style>
