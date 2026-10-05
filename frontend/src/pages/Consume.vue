<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :key="i.id" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button :disabled="busy" @click="preview">预演</button>

    <div v-if="message" :class="['msg', msgKind]">{{ message }}</div>

    <div v-if="ticket" class="ticket">
      <h3>预演票（未扣减）</h3>
      <p v-for="d in ticket.deductions" :key="d.lot_id" class="lot">
        批号 #{{ d.lot_id }} · 到期 {{ d.expiry || '—' }} · 拟扣 {{ d.take }}
      </p>
      <button :disabled="busy" @click="confirm">确认扣减</button>
      <button :disabled="busy" @click="discard">放弃</button>
    </div>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
import { bus } from '../bus'

const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const ticket = ref(null)
const message = ref('')
const msgKind = ref('')
const busy = ref(false)

onMounted(async () => {
  items.value = await api('/items')
  if (items.value[0]) item_id.value = items.value[0].id
})

// Any edit after a preview invalidates that ticket's promise to the user.
watch([item_id, qty], discard)

function discard() {
  ticket.value = null
}

function say(text, kind = '') {
  message.value = text
  msgKind.value = kind
}

async function preview() {
  if (!(Number(qty.value) > 0)) {
    say('数量必须为正数', 'err')
    return
  }
  busy.value = true
  try {
    ticket.value = await api('/consume/preview', {
      method: 'POST',
      body: JSON.stringify({ item_id: item_id.value, qty: Number(qty.value) }),
    })
    say('预演完成：仅出票，未改任何余量，可确认或放弃', 'info')
  } catch (e) {
    ticket.value = null
    if (e.body && e.body.reason === 'short') {
      say(`预演失败：余量不足，缺口 ${e.body.short}（未出票、未扣减）`, 'err')
    } else {
      say('预演失败：' + e.message, 'err')
    }
  } finally {
    busy.value = false
  }
}

async function confirm() {
  const token = ticket.value?.token
  if (!token) return
  busy.value = true
  try {
    const r = await api('/consume/confirm', {
      method: 'POST',
      body: JSON.stringify({ token }),
    })
    ticket.value = null
    say(`确认成功：共扣 ${r.deductions.reduce((s, d) => s + d.take, 0)}`, 'ok')
    // Only a landed confirmation may move the shelf numbers anywhere.
    bus.emit({ type: 'shelf-changed' })
  } catch (e) {
    // All-or-nothing: server guarantees no qty_remain changed, so keep the
    // shelf exactly as it was before the preview — emit nothing, burn ticket.
    ticket.value = null
    const b = e.body
    if (b && b.reason === 'short') {
      say(`确认失败：余量不足，缺口 ${b.short}。全部批余量未减，全层数字与预演前一致`, 'err')
    } else if (b && (b.reason === 'lot_expired' || b.reason === 'lot_changed')) {
      say(`确认失败：批次已被消费或过期下架（${b.reason}），缺口 ${b.short}。余量未减，数字与预演前一致`, 'err')
    } else if (b && b.reason === 'ticket_closed') {
      say('确认失败：该预演票已使用或作废', 'err')
    } else {
      say('确认失败：' + e.message + '（余量未减）', 'err')
    }
  } finally {
    busy.value = false
  }
}
</script>
