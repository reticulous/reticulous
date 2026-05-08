import type { boot } from 'quasar/wrappers'
import SettingSlider from '../components/SettingSlider.vue'
import SettingToggle from '../components/SettingToggle.vue'
import SettingSelect from '../components/SettingSelect.vue'
import SettingText from '../components/SettingText.vue'
import PanelHeading from '../components/PanelHeading.vue'
import { registerSystem } from '../modules/system'
import { registerNetwork } from '../modules/network'
import { registerCamera } from '../modules/camera'
import { registerStream } from '../modules/stream'
import { registerRecording } from '../modules/recording'
import { registerTriggers } from '../modules/triggers'
import { registerPlayback } from '../modules/playback'
import { registerAdvanced } from '../modules/advanced'

export default ({ app }: Parameters<Parameters<typeof boot>[0]>[0]) => {
  app.component('SettingSlider', SettingSlider)
  app.component('SettingToggle', SettingToggle)
  app.component('SettingSelect', SettingSelect)
  app.component('SettingText', SettingText)
  app.component('PanelHeading', PanelHeading)

  registerSystem()
  registerNetwork()
  registerCamera()
  registerStream()
  registerRecording()
  registerTriggers()
  registerPlayback()
  registerAdvanced()
}
