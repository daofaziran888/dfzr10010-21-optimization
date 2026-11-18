<template>
  <section class="page" data-module="iceberg">
    <header class="page-head">
      <div>
        <h2>表优化管理</h2>
        <p class="page-desc">
          维护表优化的类型选择、参数配置与效果评估；执行计划按收益排序分配预算，同表同时只保留一个活动计划。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记优化任务</button>
        <button class="btn" type="button" @click="exportRows">导出表优化管理清单</button>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</p>

    <!-- 优化任务列表 -->
    <template v-if="activeTab === 'tasks'">
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>任务编号 / 表名称</span>
          <input v-model="filters.keyword" placeholder="按任务编号或表名检索" />
        </label>
        <label class="filter-item">
          <span>任务状态</span>
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
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
            <td :colspan="columns.length + 1" class="empty-state">暂无表优化管理数据，可先登记优化任务</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条表优化管理记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 执行计划批次 -->
    <template v-else-if="activeTab === 'plans'">
      <form class="filter-bar" @submit.prevent="reloadPlans(1)">
        <label class="filter-item">
          <span>表名称</span>
          <input v-model="planFilters.table" placeholder="按表名检索" />
        </label>
        <label class="filter-item">
          <span>最低预计收益（GB）</span>
          <input v-model="planFilters.minBenefit" type="number" min="0" placeholder="收益下限" />
        </label>
        <label class="filter-item">
          <span>排序</span>
          <select v-model="planFilters.sort">
            <option value="">按批次倒序</option>
            <option value="capacity">按容量降序</option>
            <option value="benefit">按收益降序</option>
            <option value="budget">按预算占用降序</option>
          </select>
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="planFilters.status">
            <option value="">全部</option>
            <option v-for="status in planStatuses" :key="status" :value="status">{{ status }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>预算总量（GB）</span>
          <input v-model="planFilters.budget" type="number" min="1" placeholder="如 520" />
        </label>
        <button class="btn primary" type="button" @click="createPlan">生成执行计划批次</button>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetPlanFilters">重置条件</button>
      </form>

      <div class="stat-row">
        <article v-for="item in planStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>表名称</th>
            <th>计划版本</th>
            <th>预算总量</th>
            <th>预算占用</th>
            <th>预计收益</th>
            <th>被选任务</th>
            <th>状态</th>
            <th>版本冲突</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in plans" :key="String(plan.id)">
            <td>{{ plan['批次编号'] }}</td>
            <td>{{ plan['表名称'] || '—' }}</td>
            <td>{{ plan['计划版本'] }}</td>
            <td>{{ plan['预算总量GB'] }}GB</td>
            <td>{{ plan['预算占用GB'] }}GB</td>
            <td>{{ plan['预计收益GB'] }}GB</td>
            <td>{{ (plan['被选任务'] || []).join('、') || '—' }}</td>
            <td>{{ plan['状态'] }}</td>
            <td>
              <span v-if="plan['版本冲突']" class="warn-text">冲突：{{ plan['版本冲突说明'] }}</span>
              <span v-else>无</span>
            </td>
            <td class="row-actions">
              <button
                v-for="action in planActionsFor(plan)"
                :key="action"
                class="link"
                type="button"
                @click="runPlanAction(action, plan)"
              >
                {{ action }}
              </button>
              <button class="link" type="button" @click="openPlanDetail(plan)">效果详情</button>
            </td>
          </tr>
          <tr v-if="!plans.length">
            <td colspan="10" class="empty-state">暂无执行计划批次，可填写预算总量后生成</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ planTotal }} 条执行计划批次</span>
        <span class="pager">
          <button class="btn ghost" type="button" :disabled="planPage <= 1" @click="reloadPlans(planPage - 1)">
            上一页
          </button>
          <span>第 {{ planPage }} / {{ planPageCount }} 页</span>
          <button
            class="btn ghost"
            type="button"
            :disabled="planPage >= planPageCount"
            @click="reloadPlans(planPage + 1)"
          >
            下一页
          </button>
        </span>
        <span v-if="planError" class="error-text">{{ planError }}</span>
      </footer>
    </template>

    <!-- 效果详情 -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="reloadEffects(1)">
        <label class="filter-item">
          <span>批次编号 / 表名称</span>
          <input v-model="effectFilters.keyword" placeholder="按批次或表名检索" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetEffectFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>计划版本</th>
            <th>表名称</th>
            <th>被选任务</th>
            <th>预算占用</th>
            <th>实际文件数</th>
            <th>文件数变化</th>
            <th>实际容量</th>
            <th>容量变化</th>
            <th>恢复结论</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="effect in effects" :key="String(effect.id)">
            <td>{{ effect['批次编号'] }}</td>
            <td>{{ effect['计划版本'] }}</td>
            <td>{{ effect['表名称'] || '—' }}</td>
            <td>{{ (effect['被选任务'] || []).join('、') || '—' }}</td>
            <td>{{ effect['预算占用GB'] }}GB</td>
            <td>{{ effect['实际文件数'] }}</td>
            <td>{{ effect['文件数变化'] }}</td>
            <td>{{ effect['实际容量GB'] }}GB</td>
            <td>{{ effect['容量变化GB'] }}GB</td>
            <td>{{ effect['恢复结论'] || '—' }}</td>
            <td>{{ effect['状态'] }}</td>
          </tr>
          <tr v-if="!effects.length">
            <td colspan="11" class="empty-state">暂无优化效果记录，执行计划完成或发布后会写入效果统计</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ effectTotal }} 条优化效果记录（只显示最终执行批次）</span>
        <span class="pager">
          <button class="btn ghost" type="button" :disabled="effectPage <= 1" @click="reloadEffects(effectPage - 1)">
            上一页
          </button>
          <span>第 {{ effectPage }} / {{ effectPageCount }} 页</span>
          <button
            class="btn ghost"
            type="button"
            :disabled="effectPage >= effectPageCount"
            @click="reloadEffects(effectPage + 1)"
          >
            下一页
          </button>
        </span>
        <span v-if="effectError" class="error-text">{{ effectError }}</span>
      </footer>
    </template>

    <div v-if="selectedPlan" class="detail-panel">
      <header class="detail-head">
        <h3>执行计划 {{ selectedPlan['批次编号'] }} · 效果详情</h3>
        <button class="btn ghost" type="button" @click="selectedPlan = null">收起</button>
      </header>
      <dl class="detail-grid">
        <div><dt>计划版本</dt><dd>{{ selectedPlan['计划版本'] }}</dd></div>
        <div><dt>当前状态</dt><dd>{{ selectedPlan['状态'] }}</dd></div>
        <div><dt>预算总量</dt><dd>{{ selectedPlan['预算总量GB'] }}GB</dd></div>
        <div><dt>预算占用</dt><dd>{{ selectedPlan['预算占用GB'] }}GB</dd></div>
        <div><dt>实际文件数</dt><dd>{{ selectedPlan['实际文件数'] }}</dd></div>
        <div><dt>文件数变化</dt><dd>{{ selectedPlan['文件数变化'] }}</dd></div>
        <div><dt>实际容量</dt><dd>{{ selectedPlan['实际容量GB'] }}GB</dd></div>
        <div><dt>容量变化</dt><dd>{{ selectedPlan['容量变化GB'] }}GB</dd></div>
        <div><dt>恢复结论</dt><dd>{{ selectedPlan['恢复结论'] || '—' }}</dd></div>
        <div><dt>优化效果</dt><dd>{{ selectedPlan['优化效果'] || '—' }}</dd></div>
      </dl>
      <h4>候选排序（收益优先，预算内择优）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>排名</th>
            <th>执行排名</th>
            <th>任务编号</th>
            <th>表名称</th>
            <th>预算需求</th>
            <th>预计收益</th>
            <th>收益分</th>
            <th>是否选中</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in selectedPlan['候选顺序'] || []" :key="String(item['任务编号'])">
            <td>{{ item['排名'] }}</td>
            <td>{{ item['执行排名'] ?? '—' }}</td>
            <td>{{ item['任务编号'] }}</td>
            <td>{{ item['表名称'] }}</td>
            <td>{{ item['预算需求GB'] }}GB</td>
            <td>{{ item['预计收益GB'] }}GB</td>
            <td>{{ item['收益分'] }}</td>
            <td>{{ item['被选中'] ? '已选中' : '未选中' }}</td>
            <td>{{ item['未选中原因'] || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/iceberg'
const columns = ["任务编号", "表名称", "优化类型", "优化参数", "执行计划", "资源消耗", "优化效果", "任务状态"]
const actions = ["执行优化", "取消优化", "归档任务"]
const statuses = ["待执行", "执行中", "已完成", "已取消"]
const planStatuses = ["草稿", "已确认", "执行中", "已完成", "已发布"]
const tabs = [
  { key: 'tasks', label: '优化任务' },
  { key: 'plans', label: '执行计划' },
  { key: 'effects', label: '效果详情' },
]
const PLAN_TRANSITIONS: Record<string, string[]> = {
  草稿: ['确认计划'],
  已确认: ['执行计划'],
  执行中: ['完成计划'],
  已完成: ['发布批次'],
  已发布: [],
}
const PAGE_SIZE = 5

const activeTab = ref('tasks')
const noticeMessage = ref('')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({ keyword: '', status: '' })

const plans = ref<Row[]>([])
const planTotal = ref(0)
const planPage = ref(1)
const planSummary = ref<Record<string, number>>({})
const planError = ref('')
const planFilters = ref<Record<string, string>>({ table: '', minBenefit: '', sort: '', status: '', budget: '520' })
const selectedPlan = ref<Row | null>(null)

const effects = ref<Row[]>([])
const effectTotal = ref(0)
const effectPage = ref(1)
const effectError = ref('')
const effectFilters = ref<Record<string, string>>({ keyword: '' })

const planPageCount = computed(() => Math.max(1, Math.ceil(planTotal.value / PAGE_SIZE)))
const effectPageCount = computed(() => Math.max(1, Math.ceil(effectTotal.value / PAGE_SIZE)))
const planStats = computed(() => [
  { label: '批次总数', value: planSummary.value['计划总数'] ?? 0 },
  { label: '预算总量', value: `${planSummary.value['预算总量GB'] ?? 0}GB` },
  { label: '预算占用', value: `${planSummary.value['预算占用GB'] ?? 0}GB` },
  { label: '版本冲突', value: planSummary.value['版本冲突计划'] ?? 0 },
])

function planActionsFor(plan: Row): string[] {
  const list = [...(PLAN_TRANSITIONS[String(plan['状态'])] || [])]
  const status = String(plan['状态'])
  if (plan['版本冲突'] && status !== '已完成' && status !== '已发布') {
    list.push('沿用冻结顺序')
    // 已经在执行中的批次不能换任务，只能沿用冻结顺序。
    if (status !== '执行中') list.push('按新统计重排')
  }
  return list
}

function switchTab(tab: string) {
  activeTab.value = tab
  noticeMessage.value = ''
  if (tab === 'plans') void reloadPlans(planPage.value)
  if (tab === 'effects') void reloadEffects(effectPage.value)
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function resetPlanFilters() {
  planFilters.value = { table: '', minBenefit: '', sort: '', status: '', budget: '520' }
  void reloadPlans(1)
}

function resetEffectFilters() {
  effectFilters.value = { keyword: '' }
  void reloadEffects(1)
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '优化任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('表优化管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表优化管理操作失败'
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
      throw new Error('优化任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表优化管理列表读取失败'
  }
}

async function createPlan() {
  planError.value = ''
  const budget = Number(planFilters.value.budget)
  if (!budget || budget <= 0) {
    planError.value = '请先填写大于 0 的预算总量'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/plans`, {
      method: 'POST',
      body: JSON.stringify({ values: { 预算总量GB: budget } }),
    })
    const payload = await response.json()
    if (!operationOk(payload)) {
      planError.value = payload.message || '执行计划生成失败'
      return
    }
    noticeMessage.value = payload.message
    await reloadPlans(1)
    await reload()
  } catch (error) {
    planError.value = error instanceof Error ? error.message : '执行计划生成失败'
  }
}

async function reloadPlans(page: number) {
  planError.value = ''
  const query = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) })
  if (planFilters.value.table) query.set('table', planFilters.value.table)
  if (planFilters.value.minBenefit) query.set('min_benefit', planFilters.value.minBenefit)
  if (planFilters.value.sort) query.set('sort', planFilters.value.sort)
  if (planFilters.value.status) query.set('status', planFilters.value.status)
  try {
    const response = await request(`${ENDPOINT}/plans?${query.toString()}`)
    if (!response.ok) {
      throw new Error('执行计划列表读取失败')
    }
    const payload = await response.json()
    plans.value = payload.items ?? []
    planTotal.value = payload.total ?? plans.value.length
    planSummary.value = payload.summary ?? {}
    planPage.value = page
  } catch (error) {
    planError.value = error instanceof Error ? error.message : '执行计划列表读取失败'
  }
}

async function openPlanDetail(plan: Row) {
  try {
    const response = await request(`${ENDPOINT}/plans/${encodeURIComponent(String(plan['批次编号']))}`)
    if (!response.ok) {
      throw new Error('执行计划详情读取失败')
    }
    selectedPlan.value = await response.json()
  } catch (error) {
    planError.value = error instanceof Error ? error.message : '执行计划详情读取失败'
  }
}

async function runPlanAction(action: string, plan: Row) {
  planError.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/plans/${encodeURIComponent(String(plan['批次编号']))}/actions`,
      { method: 'POST', body: JSON.stringify({ values: { action } }) },
    )
    const payload = await response.json()
    if (!operationOk(payload)) {
      planError.value = payload.message || `${action}未生效`
      await reloadPlans(planPage.value)
      return
    }
    noticeMessage.value = payload.message
    await reloadPlans(planPage.value)
    if (selectedPlan.value && selectedPlan.value['批次编号'] === plan['批次编号']) {
      await openPlanDetail(plan)
    }
    if (action === '完成计划' || action === '发布批次') {
      await reloadEffects(1)
    }
  } catch (error) {
    planError.value = error instanceof Error ? error.message : `${action}未生效`
  }
}

function operationOk(payload: Row): boolean {
  return payload?.ok === true
}

async function reloadEffects(page: number) {
  effectError.value = ''
  const query = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) })
  if (effectFilters.value.keyword) query.set('keyword', effectFilters.value.keyword)
  try {
    const response = await request(`${ENDPOINT}/effects?${query.toString()}`)
    if (!response.ok) {
      throw new Error('优化效果统计读取失败')
    }
    const payload = await response.json()
    effects.value = payload.items ?? []
    effectTotal.value = payload.total ?? effects.value.length
    effectPage.value = page
  } catch (error) {
    effectError.value = error instanceof Error ? error.message : '优化效果统计读取失败'
  }
}

onMounted(reload)
</script>
