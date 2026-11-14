<template>
  <section class="page" data-module="datadrill">
    <header class="page-head">
      <div>
        <h2>多维下钻管理</h2>
        <p class="page-desc">定义数据多维下钻的分析路径，配置从汇总到明细的层级钻取和每层度量。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记下钻路径</button>
        <button class="btn" type="button" @click="exportRows">导出多维下钻清单</button>
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
          <th>下钻可用性 / 失效原因</th>
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
            <template v-if="row.可下钻">
              <span class="ok-text">可下钻（{{ row.完整版本标签 || row.路径版本 }}）</span>
            </template>
            <template v-else-if="row.失效原因">
              <span class="error-text">已阻止：{{ row.失效原因 }}</span>
              <div v-if="row.最新可用版本" class="hint-text">最新可用版本：{{ row.最新可用版本 }}</div>
              <div v-if="row.有效默认展开层" class="hint-text">默认层最大值：{{ row.有效默认展开层 }}</div>
            </template>
            <template v-else><span class="hint-text">未启用，暂不可下钻</span></template>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无多维下钻数据，可先登记下钻路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条多维下钻记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/datadrill'
const columns = ["路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量", "路径深度", "默认展开层", "关联看板"]
const statuses = ["草稿", "路径评审中", "已启用", "已关闭"]
const stats = [{"label": "下钻路径总数", "value": 0}, {"label": "已启用路径", "value": 0}, {"label": "路径评审中", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 生命周期只能 草稿→路径评审中→已启用→已关闭，按当前状态只暴露下一动作，
// 从入口上阻止跳级、倒序、关闭后恢复与重复归档。
function availableActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交路径评审']
    case '路径评审中':
      return ['启用路径']
    case '已启用':
      return ['关闭路径']
    default:
      return []
  }
}

function statusClass(status: unknown): string {
  if (status === '已启用') return 'status-ok'
  if (status === '已关闭') return 'status-closed'
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
  errorMessage.value = '下钻路径登记入口尚未接入审批流'
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
      throw new Error(payload?.message ?? '多维下钻动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('下钻路径列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻列表读取失败'
  }
}

onMounted(reload)
</script>
