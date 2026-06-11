import { createPinia } from 'pinia'

// Quasar auto-installs the default export from src/stores/index.ts onto the app.
// The pinia instance is app-owned; spangap-browser's defineStore() calls register
// against this single instance via pinia's setActivePinia in createPinia().
export default createPinia()
