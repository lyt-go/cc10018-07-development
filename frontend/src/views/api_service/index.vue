<template>
  <section class="page" data-module="api_service">
    <header class="page-head">
      <div>
        <h2>API服务管理</h2>
        <p class="page-desc">维护数据查询接口的SQL定义、参数绑定与访问控制，为API服务提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记数据接口</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出API服务清单</button>
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
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="contract-panel" data-testid="contract-panel">
      <header class="contract-head">
        <h3>接口SQL契约包</h3>
        <span class="contract-hint">查询SQL、参数类型与返回字段同属一版已校验契约；草稿或只校验一半的契约不会出现在这里。</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>接口编号</th>
            <th>生效版本</th>
            <th>关联模板</th>
            <th>参数声明</th>
            <th>返回字段（按序）</th>
            <th>契约摘要</th>
            <th>发布时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in contracts" :key="item.api_code" :data-testid="`contract-row-${item.api_code}`">
            <td>{{ item.api_code }}</td>
            <td>{{ item.version_label }}</td>
            <td>{{ item.template_ref }}</td>
            <td>{{ item.params.map((param: ContractParam) => `${param.name}:${param.type}`).join(', ') }}</td>
            <td>{{ item.return_fields.join(', ') }}</td>
            <td class="mono">{{ item.checksum }}</td>
            <td>{{ item.published_at }}</td>
          </tr>
          <tr v-if="!contracts.length">
            <td colspan="7" class="empty-state">暂无已发布的接口SQL契约包</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`contract-${row.id}`"
              @click="openContract(row)"
            >
              契约版本
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无API服务数据，可先登记数据接口</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记数据接口</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="contractRow" class="modal-backdrop" data-testid="contract-modal">
      <div class="modal">
        <h3>接口SQL契约包 · {{ contractRow['接口编号'] }}</h3>
        <p class="page-desc">发布前校验会把查询SQL、参数类型与返回字段一起确认；校验未完整通过时不能发布，旧版继续对外取数。</p>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>模板</th><th>参数声明</th><th>返回字段</th><th>契约摘要</th></tr>
          </thead>
          <tbody>
            <tr v-for="version in contractVersions" :key="version.version_no" :data-testid="`version-row-${version.version_no}`">
              <td>{{ version.version_label }}</td>
              <td>{{ version.template_ref }} / {{ version.template_version }}</td>
              <td>{{ version.params.map((param: ContractParam) => `${param.name}:${param.type}`).join(', ') }}</td>
              <td>{{ version.return_fields.join(', ') }}</td>
              <td class="mono">{{ version.checksum }}</td>
            </tr>
            <tr v-if="!contractVersions.length">
              <td colspan="5" class="empty-state">该接口尚无已发布契约版本</td>
            </tr>
          </tbody>
        </table>
        <p v-if="contractMessage" class="contract-message" data-testid="contract-message">{{ contractMessage }}</p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="contract-close" @click="contractRow = null">关闭</button>
          <button class="btn primary" type="button" data-testid="contract-precheck" @click="runPrecheck">
            发布前校验
          </button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条API服务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type ContractParam = { name: string; type: string; required: boolean; default: string | null }
type ContractVersion = {
  version_no: number
  version_label: string
  template_ref: string
  template_version: string
  params: ContractParam[]
  return_fields: string[]
  checksum: string
  published_at: string
}
type ContractPackage = ContractVersion & { api_code: string; version_count: number }

const ENDPOINT = '/api/api_service'
const columns = ["接口编号", "接口名称", "查询SQL", "参数列表", "返回字段", "缓存时长", "限流阈值", "接口版本"]
const actions = ["提交校验", "发布接口", "下线接口"]
const statuses = ["草稿", "待校验", "已发布", "已下线"]
const metricNames = ["数据接口总数", "待校验接口", "已发布接口"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const contracts = ref<ContractPackage[]>([])
const contractRow = ref<Row | null>(null)
const contractVersions = ref<ContractVersion[]>([])
const contractMessage = ref('')
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'api_service.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('API服务动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (!payload.ok) {
      // 发布前校验失败时把完整原因带到页面上，而不是吞成一句通用错误
      throw new Error(payload.message || 'API服务动作未生效')
    }
    await reload()
    await loadContracts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'API服务操作失败'
  }
}

async function loadContracts() {
  try {
    const response = await request('/api/api_contract/packages')
    if (!response.ok) return
    const payload = await response.json()
    contracts.value = payload.items ?? []
  } catch {
    contracts.value = []
  }
}

async function openContract(row: Row) {
  contractRow.value = row
  contractVersions.value = []
  contractMessage.value = ''
  try {
    const response = await request(`/api/api_contract/packages/${row.id}/versions`)
    if (!response.ok) return
    const payload = await response.json()
    contractVersions.value = payload.items ?? []
  } catch {
    contractVersions.value = []
  }
}

async function runPrecheck() {
  if (!contractRow.value) return
  contractMessage.value = '发布前校验中…'
  try {
    const response = await request(`/api/api_contract/packages/${contractRow.value.id}/precheck`, {
      method: 'POST', body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '校验请求失败')
    contractMessage.value = payload.ok
      ? `校验通过：SQL、${payload.params.length} 个参数与返回字段 ${payload.return_fields.join(', ')} 同属一版，可发布`
      : `校验未通过：${(payload.errors ?? []).join('；')}`
  } catch (error) {
    contractMessage.value = error instanceof Error ? error.message : '校验请求失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('数据接口列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'API服务列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadContracts()
})
</script>
