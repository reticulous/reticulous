import { configure } from 'quasar/wrappers';
import { readFileSync } from 'node:fs';

// The browser-half straddles are pulled in as `file:` deps and npm-linked. They
// ARE the code under development, so Vite must NOT pre-bundle them — a stale
// optimized chunk is why an edit to e.g. spangap-browser wouldn't show up under
// `spangap dev` until the cache was blown away. Excluding them from optimizeDeps
// serves them as live source (proper HMR); their real npm deps still optimize.
const pkg = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
) as { dependencies?: Record<string, string> };
const linkedStraddles = Object.entries(pkg.dependencies ?? {})
  .filter(([, v]) => typeof v === 'string' && v.startsWith('file:'))
  .map(([name]) => name);

export default configure(() => {
  return {
    boot: ['modules'],
    css: ['app.css'],
    extras: [],
    build: {
      target: { browser: ['es2022'] },
      vueRouterMode: 'history',
      extendViteConf(viteConf) {
        // spangap-browser is a file: dep — vite must resolve its peers
        // (vue, pinia, quasar, vue-router) from this consumer's node_modules,
        // not from the symlinked package's location. preserveSymlinks keeps
        // the resolution context anchored to the symlink site.
        viteConf.resolve = { ...viteConf.resolve, preserveSymlinks: true };
        // Keep the linked straddles out of dep pre-bundling so edits to them are
        // picked up live (see linkedStraddles above).
        viteConf.optimizeDeps = {
          ...viteConf.optimizeDeps,
          exclude: [...(viteConf.optimizeDeps?.exclude ?? []), ...linkedStraddles],
        };
        if (viteConf.build) {
          viteConf.build.assetsDir = '';
          viteConf.build.cssCodeSplit = false;
          // We deliberately ship one chunk (see manualChunks below) because
          // code-splitting buys nothing on a single-device ESP32 SPA. Mute
          // rollup's "chunk larger than 500 kB" advice so the build is clean.
          viteConf.build.chunkSizeWarningLimit = Infinity;
          viteConf.build.rollupOptions = {
            ...viteConf.build.rollupOptions,
            output: {
              entryFileNames: 'app.js',
              chunkFileNames: 'app.js',  /* merge all chunks into entry */
              assetFileNames: '[name][extname]',
              manualChunks: () => 'app',  /* everything in one chunk */
            },
          };
        }
      },
    },
    devServer: {
      open: false,
      // Reverse-proxy the device so the dev server is same-origin with it: the
      // session cookie, /auth, and the /webrtc signaling all behave exactly as
      // when the SPA is served from the device. `spangap dev` passes the active
      // device address in SPANGAP_DEVICE; without it (a bare `quasar dev`) there's
      // no target and these routes 404 locally. secure:false accepts the device's
      // self-signed TLS. (The WebDAV file editor talks to arbitrary /<path> URLs
      // that can't be prefix-proxied without shadowing Vite's assets, so that one
      // feature is unavailable under `spangap dev` — everything else works.)
      proxy: (() => {
        const device = process.env.SPANGAP_DEVICE
        if (!device) return undefined
        const target = `https://${device}`
        const common = { target, changeOrigin: true, secure: false }
        return {
          '/auth': common,
          '/state': common,
          '/webrtc': { ...common, ws: true },
          // checkAuth()'s X-Authenticated probe → the device root (see auth.ts).
          '/__authcheck': { ...common, rewrite: () => '/' },
        }
      })(),
    },
    framework: {
      iconSet: 'svg-material-icons',
      config: {
        dark: true,
      },
      plugins: [],
    },
    pwa: {
      workboxMode: 'InjectManifest',
      injectPwaMetaTags: true,
      swFilename: 'sw.js',
      manifestFilename: 'manifest.json',
      useCredentialsForManifestTag: false,
      extendManifestJson(json) {
        Object.assign(json, {
          name: 'reticulous',
          short_name: 'reticulous',
          description: 'Reticulum mesh networking for the masses!',
          display: 'standalone',
          orientation: 'any',
          background_color: '#1d1d1d',
          theme_color: '#1d1d1d',
          icons: [
            { src: 'icons/icon-128x128.png', sizes: '128x128', type: 'image/png' },
            { src: 'icons/icon-192x192.png', sizes: '192x192', type: 'image/png' },
            { src: 'icons/icon-256x256.png', sizes: '256x256', type: 'image/png' },
            { src: 'icons/icon-512x512.png', sizes: '512x512', type: 'image/png' },
          ],
        });
      },
      extendInjectManifestOptions(cfg) {
        // Don't precache icons — they're only used for install
        cfg.globIgnores = cfg.globIgnores || [];
        cfg.globIgnores.push('icons/**');
      },
    },
  };
});
