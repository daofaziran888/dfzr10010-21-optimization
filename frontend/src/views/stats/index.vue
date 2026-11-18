<template>
  <section class="page" data-module="stats">
    <header class="page-head">
      <div>
        <h2>表统计信息管理</h2>
        <p class="page-desc">
          维护表统计的行数、文件数与容量分析；执行计划批次结果沿用同一版本，历史记录保持只读展示。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记统计报告</button>
        <button class="btn" type="button" @click="exportRows">导出表统计信息清单</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>报告编号 / 表名称</span>
        <input v-model="filters.keyword" placeholder="按报告编号或表名检索" />
      </label>
      <label class="filter-item">
        <span>报告状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>执行计划</th>
          <th>批次结果</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row['批次编号']">{{ row['批次编号'] }}（v{{ row['执行计划版本'] }}）</span>
            <span v-else class="muted-text">历史记录</span>
          </td>
          <td>
            <span v-if="row['是否历史记录']" class="muted-text">未关联执行计划</span>
            <span v-else>
              被选 {{ (row['被选任务'] || []).join('、') || '—' }}；
              文件数变化 {{ row['文件数变化'] }}；容量变化 {{ row['容量变化'] }}
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
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
          <td :colspan="columns.length + 3" class="empty-state">暂无表统计信息数据，可先登记统计报告</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条表统计信息记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/stats'
const columns = ["报告编号", "表名称", "行总数", "文件总数", "总容量", "列统计", "统计时间", "报告状态"]
const actions = ["采集统计", "标记过期", "清理报告"]
const statuses = ["草稿", "已采集", "已过期", "已清理"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({ keyword: '', status: '' })

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '统计报告登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (payload?.ok === false) {
      errorMessage.value = payload.message || '表统计信息动作未生效'
      return
    }
    if (!response.ok) {
      throw new Error('表统计信息动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message || `统计报告已${action}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.status) query.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('统计报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息列表读取失败'
  }
}

onMounted(reload)
</script>
