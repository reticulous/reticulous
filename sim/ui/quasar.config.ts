import { configure } from 'quasar/wrappers';
import { readFileSync } from 'node:fs';
import { linkedDepsHmr } from 'spangap-browser/vite/linked-deps-hmr';

// spangap-browser is pulled in as a `file:` dep and npm-linked. It IS code
// under development, so Vite must NOT pre-bundle it — a stale optimized chunk
// is why an edit to it wouldn't show up until the cache was blown away.
// Excluding it from optimizeDeps serves it as live source; the other half of
// that is the linkedDepsHmr plugin below, which watches where it really lives.
const pkg = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
) as { dependencies?: Record<string, string> };
const linkedStraddles = Object.entries(pkg.dependencies ?? {})
  .filter(([, v]) => typeof v === 'string' && v.startsWith('file:'))
  .map(([name]) => name);

// Where `quasar dev` sends /ws and /api. simd binds this inside the container;
// `spangap sim --dev` runs the two side by side.
const SIMD = process.env.SPANGAP_SIMD || 'http://127.0.0.1:9011';

export default configure(() => {
  return {
    boot: [],
    css: ['app.css'],
    extras: [],
    build: {
      target: { browser: ['es2022'] },
      vueRouterMode: 'history',
      vitePlugins: [[linkedDepsHmr, {}]],
      extendViteConf(viteConf) {
        // spangap-browser is a file: dep — vite must resolve its peers (vue,
        // pinia, quasar, vue-router) from this consumer's node_modules, not
        // from the symlinked package's location. preserveSymlinks keeps the
        // resolution context anchored to the symlink site.
        viteConf.resolve = { ...viteConf.resolve, preserveSymlinks: true };
        viteConf.optimizeDeps = {
          ...viteConf.optimizeDeps,
          exclude: [...(viteConf.optimizeDeps?.exclude ?? []), ...linkedStraddles],
        };
        if (viteConf.build) {
          viteConf.build.chunkSizeWarningLimit = Infinity;
        }
      },
    },
    devServer: {
      open: false,
      // Same-origin with simd, so the websocket and the station links behave
      // exactly as when simd serves the built page itself.
      proxy: {
        '/ws': { target: SIMD, changeOrigin: true, ws: true },
        '/api': { target: SIMD, changeOrigin: true },
      },
    },
    framework: {
      iconSet: 'svg-material-icons',
      config: { dark: true },
      plugins: ['Dialog', 'Notify'],
    },
  };
});
