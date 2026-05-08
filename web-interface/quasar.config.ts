import { configure } from 'quasar/wrappers';

export default configure(() => {
  return {
    boot: ['modules'],
    css: ['app.css'],
    extras: [],
    build: {
      target: { browser: ['es2022'] },
      vueRouterMode: 'history',
      extendViteConf(viteConf) {
        if (viteConf.build) {
          viteConf.build.assetsDir = '';
          viteConf.build.cssCodeSplit = false;
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
          name: 'seccam',
          short_name: 'seccam',
          description: 'Security Camera',
          display: 'standalone',
          orientation: 'any',
          background_color: '#1a1a2e',
          theme_color: '#1a1a2e',
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
