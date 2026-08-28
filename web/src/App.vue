<script setup>
import { ref, onMounted, provide } from 'vue'
import { api } from './api'
import AppSidebar from './components/AppSidebar.vue'

// One health probe at boot tells the whole app what this install can do -
// whether a mic is reachable and whether a Gemini key is set - so features
// that cannot work are explained rather than failing when clicked.
const health = ref({ ok: false, mic: false, gemini: false })
provide('health', health)

onMounted(async () => {
  try { health.value = await api.health() } catch { /* the sidebar shows offline */ }
})
</script>

<template>
  <div class="shell">
    <AppSidebar :health="health" />
    <main class="main">
      <RouterView v-slot="{ Component }">
        <component :is="Component" class="rise" />
      </RouterView>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: var(--sidebar) 1fr;
  height: 100%;
  position: relative;
  z-index: 1;
}
.main {
  overflow-y: auto;
  overflow-x: hidden;
  height: 100vh;
}
@media (max-width: 860px) {
  .shell { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
}
</style>
