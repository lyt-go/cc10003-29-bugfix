<template>
  <section class="page" data-module="dashboard">
    <header class="page-head">
      <div>
        <h2>看板配置管理</h2>
        <p class="page-desc">配置数据看板页面，组合多个指标卡片、图表和筛选器形成完整的业务监控视图。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记看板页面</button>
        <button class="btn" type="button" @click="exportRows">导出看板配置清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>关联路径版本 / 发布门禁</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-tag" :class="statusClass(row.status)">{{ row.status ?? '—' }}</span>
          </td>
          <td>
            <template v-if="row.路径版本">
              <div class="ok-text">{{ row.路径版本 }}</div>
              <div v-if="row.invalid_reason" class="error-text">下钻已阻止：{{ row.invalid_reason }}</div>
              <div v-if="row.最新可用版本" class="hint-text">最新可用版本：{{ row.最新可用版本 }}</div>
            </template>
            <template v-else-if="row.invalid_reason">
              <span class="error-text">{{ row.invalid_reason }}</span>
              <div v-if="row.最新可用版本" class="hint-text">最新可用版本：{{ row.最新可用版本 }}</div>
              <div v-if="row.路径深度值" class="hint-text">默认层最大值：{{ row.路径深度值 }}</div>
            </template>
            <template v-else><span class="hint-text">{{ row.path_label && row.path_label !== 'none' ? row.path_label : '未绑定可用路径' }}</span></template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="row.status === '看板评审中'"
              class="link"
              type="button"
              @click="runPublishCheck(row)"
            >
              发布门禁检查
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无看板配置数据，可先登记看板页面</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条看板配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/dashboard'
const columns = ["看板编号", "看板名称", "包含指标", "布局模板", "刷新间隔", "可见范围", "所属业务线", "看板负责人"]
const statuses = ["草稿", "看板评审中", "已发布", "已下线"]
const stats = [{"label": "看板页面总数", "value": 0}, {"label": "已发布看板", "value": 0}, {"label": "看板评审中", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 看板生命周期同样只能逐档推进，按当前状态只暴露合法的下一动作。
function availableActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交看板评审']
    case '看板评审中':
      return ['发布看板']
    case '已发布':
      return ['下线看板']
    default:
      return []
  }
}

function statusClass(status: unknown): string {
  if (status === '已发布') return 'status-ok'
  if (status === '已下线') return 'status-closed'
  return 'status-pending'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '看板页面登记入口尚未接入审批流'
}

async function runPublishCheck(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/publish-check`)
    const check = await response.json()
    if (check.passed) {
      errorMessage.value = ''
      window.alert(check.message)
    } else {
      const detail = (check.problems ?? []).map((item: { message: string }) => item.message).join('；')
      errorMessage.value = `发布门禁未通过（${check.invalid_reason}）：${detail}`
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布门禁检查失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.message ?? '看板配置动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板配置操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('看板页面列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板配置列表读取失败'
  }
}

onMounted(reload)
</script>
