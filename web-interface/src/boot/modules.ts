import type { boot } from 'quasar/wrappers'
import SettingSlider from 'spangap-browser/components/SettingSlider.vue'
import SettingToggle from 'spangap-browser/components/SettingToggle.vue'
import SettingSelect from 'spangap-browser/components/SettingSelect.vue'
import SettingText from 'spangap-browser/components/SettingText.vue'
import PanelHeading from 'spangap-browser/components/PanelHeading.vue'
import { registerSystem } from 'spangap-browser/modules/system'
import { registerAdvanced } from 'spangap-browser/modules/advanced'
// Every staged straddle that declares `browser_register:` is wired here in init
// order (acme/upnp/wg/duckdns/sshd + rns/iface-*/lxmf/nomad). Auto-generated and
// its package.json file: deps maintained by spangap-inside each build, so adding
// a straddle needs no edit to this file.
import registerStraddles from './straddles.gen'

export default ({ app }: Parameters<Parameters<typeof boot>[0]>[0]) => {
  app.component('SettingSlider', SettingSlider)
  app.component('SettingToggle', SettingToggle)
  app.component('SettingSelect', SettingSelect)
  app.component('SettingText', SettingText)
  app.component('PanelHeading', PanelHeading)

  registerSystem()
  registerStraddles()   // includes registerNet (Internet/WiFi/mDNS) from spangap-net
  registerAdvanced()
}
