import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './styles/theme.css'

// Views are lazy so the live panes (and their WebSocket code) are not part of
// the bundle you pay for when you only wanted to read a chart.
const routes = [
  { path: '/', redirect: '/library' },
  { path: '/library', name: 'library', component: () => import('./views/LibraryView.vue') },
  {
    path: '/song/:id',
    component: () => import('./views/SongView.vue'),
    props: true,
    children: [
      { path: '', redirect: (to) => `/song/${to.params.id}/structure` },
      { path: 'structure', name: 'structure', component: () => import('./views/StructureView.vue'), props: true },
      { path: 'tabs', name: 'tabs', component: () => import('./views/TabsView.vue'), props: true },
      { path: 'play', name: 'play', component: () => import('./views/PlayAlongView.vue'), props: true },
      { path: 'lyrics', name: 'lyrics', component: () => import('./views/LyricsView.vue'), props: true },
      { path: 'chart', name: 'chart', component: () => import('./views/ChartView.vue'), props: true },
      { path: 'solo', name: 'solo', component: () => import('./views/SoloView.vue'), props: true },
    ],
  },
  { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue') },
  { path: '/live/tab', name: 'live-tab', component: () => import('./views/LiveTabView.vue') },
  { path: '/live/key', name: 'live-key', component: () => import('./views/LiveKeyView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/library' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

// A rebuild fingerprints every chunk, so a tab left open across one asks for a
// lazy view that no longer exists. The import rejects, vue-router abandons the
// navigation, and the link is dead for the life of the tab - which looks
// exactly like a broken button, with nothing on screen to say why. One reload
// puts the tab on the current build. `_SPAFiles` deliberately 404s a missing
// asset rather than answering it with the shell, which is what makes this
// failure identifiable at all instead of an HTML MIME-type error.
const STALE = /dynamically imported module|Importing a module script failed|MIME type|Failed to fetch/i
const RETRIED = 'scriptum:reloaded-for'

function once(key, path) {
  // Guarded so a chunk that is genuinely broken cannot become a reload loop:
  // one reload per route, cleared as soon as any navigation succeeds.
  try {
    if (sessionStorage.getItem(key) === path) return false
    sessionStorage.setItem(key, path)
  } catch { /* private mode: one reload is still better than a dead link */ }
  return true
}

router.onError((err, to) => {
  if (!STALE.test(err?.message || '')) return
  if (once(RETRIED, to.fullPath)) window.location.assign(to.fullPath)
})
router.afterEach(() => { try { sessionStorage.removeItem(RETRIED) } catch { /* ignore */ } })

createApp(App).use(router).mount('#app')
