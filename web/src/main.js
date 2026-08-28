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
  { path: '/live/tab', name: 'live-tab', component: () => import('./views/LiveTabView.vue') },
  { path: '/live/key', name: 'live-key', component: () => import('./views/LiveKeyView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/library' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

createApp(App).use(router).mount('#app')
