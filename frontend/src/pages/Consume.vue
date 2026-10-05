<template>
  <div>
    <h1>按临期消费</h1>
    <p class="muted">先预演（不动任何库存数字），确认后才实际扣减。</p>
    <select v-model.number="item_id" :disabled="!!ticket"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" :disabled="!!ticket" />
    <button @click="doPreview" :disabled="!!ticket || loading">预演</button>

    <div v-if="previewMsg" class="ticket-box">
      <p v-if="previewMsg.text">预演失败：{{ previewMsg.text }}</p>
      <p v-else>库存不足，还差 <b>{{ previewMsg.short }}</b>，未生成预演票，任何余量未变动。</p>
    </div>

    <div v-if="ticket" class="ticket-box">
      <h3>预演票 #{{ ticket.ticket_id }}：将扣以下批次（FEFO）</h3>
      <table class="preview-table">
        <tr><th>批号</th><th>到期</th><th>扣减</th></tr>
        <tr v-for="d in ticket.deductions" :key="d.lot_id">
          <td>{{ d.lot_id }}</td><td>{{ d.expiry || '—' }}</td><td>{{ d.take }}</td>
        </tr>
      </table>
      <p class="muted">全层竖列 / 层页 / 顶条在确认前均不变化。</p>
      <button @click="doConfirm" :disabled="loading">确认扣减</button>
      <button @click="reset" :disabled="loading">放弃预演</button>
    </div>

    <div v-if="confirmFail" class="ticket-box">
      <p>确认失败：{{ confirmFail.text }}</p>
      <p v-if="confirmFail.short">不足数量 short={{ confirmFail.short }}，任何批次余量均未扣减，全层数字与预演前一致。</p>
      <p class="muted" v-else>该票仍保留，可在库存变化后再次确认（按提交瞬间重算）。</p>
    </div>

    <pre v-if="result">{{ result }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const ticket = ref(null)
const previewMsg = ref(null)
const confirmFail = ref(null)
const result = ref('')
const loading = ref(false)

onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })

function reset() {
  ticket.value = null
  previewMsg.value = null
  confirmFail.value = null
  result.value = ''
}

async function doPreview() {
  reset()
  loading.value = true
  try {
    const p = await api('/consume/preview', {
      method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }),
    })
    if (!p.ok) { previewMsg.value = { short: p.short }; return }
    ticket.value = p
  } catch (e) {
    previewMsg.value = { short: 0, text: errText(e) }
  } finally { loading.value = false }
}

async function doConfirm() {
  confirmFail.value = null
  result.value = ''
  loading.value = true
  try {
    const r = await api('/consume/confirm', {
      method: 'POST', body: JSON.stringify({ ticket_id: ticket.value.ticket_id }),
    })
    result.value = JSON.stringify(r, null, 2)
    ticket.value = null
  } catch (e) {
    const d = e.body && e.body.detail
    confirmFail.value = {
      text: (d && (d.code || JSON.stringify(d))) || e.message,
      short: d && typeof d.short === 'number' ? d.short : null,
    }
  } finally { loading.value = false }
}

function errText(e) {
  const d = e.body && e.body.detail
  return (d && (d.code || JSON.stringify(d))) || e.message
}
</script>
