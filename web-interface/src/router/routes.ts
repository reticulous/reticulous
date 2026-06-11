import type { RouteRecordRaw } from 'vue-router';

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    component: () => import('spangap-browser/pages/LoginPage.vue'),
  },
  {
    path: '/setup',
    component: () => import('spangap-browser/pages/SetupPage.vue'),
  },
  {
    path: '/',
    component: () => import('src/layouts/MainLayout.vue'),
    children: [
      { path: '', component: () => import('src/pages/IndexPage.vue') },
    ],
  },
];

export default routes;
