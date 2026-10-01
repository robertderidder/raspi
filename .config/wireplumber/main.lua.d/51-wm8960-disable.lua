-- Keep PipeWire away from the WM8960 HAT. With no UCM profile for the card,
-- WirePlumber falls back to a generic one and, while setting it up, drives
-- Headphone to 0 (mute) on every start, after alsa-restore has already put
-- the saved level back. Nothing here plays through PipeWire anyway:
-- raspotify writes to ALSA directly via Waveshare's /etc/asound.conf.
table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "equals", "alsa_card.platform-soc_sound" },
    },
  },
  apply_properties = {
    ["device.disabled"] = true,
  },
})
