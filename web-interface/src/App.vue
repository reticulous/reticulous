<template>
  <router-view />
</template>

<style>
/* Prevent rubber-band / bounce scroll on iOS and wheel scroll on desktop over video UI */
html,
body,
#q-app {
  height: 100%;
  margin: 0;
  overflow: hidden;
  overscroll-behavior: none;
}
</style>
