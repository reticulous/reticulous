import { route } from 'quasar/wrappers'
import { createRouter, createWebHistory } from 'vue-router'
import routes from './routes'

export default route(function () {
  return createRouter({
    routes,
    history: createWebHistory(process.env.VUE_ROUTER_BASE),
    scrollBehavior: () => ({ left: 0, top: 0 }),
  })
})
