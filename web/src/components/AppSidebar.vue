<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'

defineProps({ health: { type: Object, required: true } })

const route = useRoute()
const recent = ref([])

async function loadRecent() {
  try { recent.value = (await api.library()).slice(0, 6) } catch { recent.value = [] }
}
onMounted(loadRecent)
// Coming back to the library (or opening a song) is when the list can have
// changed - re-read it then rather than polling.
watch(() => route.path, (p) => { if (p.startsWith('/library') || p.startsWith('/song')) loadRecent() })
</script>

<template>
  <aside class="side">
    <RouterLink to="/library" class="brand">
      <svg class="mark" viewBox="0 0 32 32" aria-hidden="true">
        <path d="M11 25.5a3.5 3.5 0 1 1 3.5-3.5V6.8l11-2.3v13.9" fill="none"
              stroke="url(#g)" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" />
        <circle cx="22" cy="18.4" r="3.5" fill="none" stroke="url(#g)" stroke-width="2.1" />
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#ff456b" />
            <stop offset="1" stop-color="#a8172f" />
          </linearGradient>
        </defs>
      </svg>
      <span class="wordmark">Scriptum</span>
    </RouterLink>

    <nav class="nav">
      <div class="eyebrow navhead">Songs</div>
      <RouterLink to="/library" class="navitem">
        <span class="ico">◈</span> Library
      </RouterLink>

      <template v-if="recent.length">
        <div class="recent">
          <RouterLink
            v-for="s in recent"
            :key="s.id"
            :to="`/song/${s.id}`"
            class="navitem sub"
            :class="{ on: route.path.startsWith(`/song/${s.id}`) }"
          >
            <span class="dot" :class="{ ready: s.analyzed }" />
            <span class="tt">{{ s.title }}</span>
          </RouterLink>
        </div>
      </template>

      <div class="eyebrow navhead">Practice room</div>
      <RouterLink to="/live/tab" class="navitem">
        <span class="ico">◉</span> Live tab
      </RouterLink>
      <RouterLink to="/live/key" class="navitem">
        <span class="ico">◐</span> Live key
      </RouterLink>

      <div class="eyebrow navhead">This install</div>
      <RouterLink to="/settings" class="navitem">
        <span class="ico">◇</span> Settings
      </RouterLink>
    </nav>

    <div class="foot">
      <div class="stat">
        <span class="led" :class="{ on: health.mic }" />
        <span class="dim">{{ health.mic ? 'mic ready' : 'no mic' }}</span>
      </div>
      <div class="stat">
        <span class="led" :class="{ on: health.gemini }" />
        <span class="dim">{{ health.gemini ? 'gemini ready' : 'no gemini key' }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.side {
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, var(--surface) 0%, var(--bg-deep) 100%);
  border-right: 1px solid var(--line-soft);
  padding: 18px 12px 12px;
  overflow-y: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 6px 18px;
  color: var(--text);
  text-decoration: none;
}
.brand:hover { text-decoration: none; }
.mark { width: 27px; height: 27px; flex: none; }
.wordmark {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0.015em;
  background: linear-gradient(100deg, #fff 10%, #f0a8b4 55%, var(--red-bright) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}

.nav { display: flex; flex-direction: column; gap: 1px; flex: 1; }
.navhead { padding: 14px 8px 5px; }

.navitem {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 9px;
  border-radius: var(--r-sm);
  color: var(--text-3);
  text-decoration: none;
  font-size: 13.5px;
  border-left: 2px solid transparent;
  transition: background 0.13s, color 0.13s, border-color 0.13s;
}
.navitem:hover { background: var(--surface-2); color: var(--text); text-decoration: none; }
.navitem.router-link-active, .navitem.on {
  background: var(--surface-3);
  color: var(--text);
  border-left-color: var(--red-bright);
}
.ico { color: var(--red-bright); font-size: 12px; width: 13px; text-align: center; }

.recent { display: flex; flex-direction: column; gap: 1px; margin-left: 4px; }
.navitem.sub { font-size: 12.5px; padding: 5px 9px; }
.tt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: var(--line-strong); flex: none;
}
.dot.ready { background: var(--gold); box-shadow: 0 0 6px rgba(217, 164, 65, 0.55); }

.foot {
  border-top: 1px solid var(--line-soft);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11.5px;
}
.stat { display: flex; align-items: center; gap: 7px; padding-left: 8px; }
.led {
  width: 6px; height: 6px; border-radius: 50%;
  background: #4a2a31; flex: none;
}
.led.on { background: var(--ok); box-shadow: 0 0 6px rgba(79, 180, 119, 0.6); }

@media (max-width: 860px) {
  .side { flex-direction: row; align-items: center; overflow-x: auto; padding: 10px; }
  .brand { padding: 0 12px 0 4px; }
  .nav { flex-direction: row; align-items: center; gap: 4px; }
  .navhead, .recent, .foot { display: none; }
}
</style>
