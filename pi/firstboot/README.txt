Pellet burner monitor - easy install
====================================

The easy install is new and has not been tried on a fresh card yet; if you get stuck, the manual setup
in the project's README is the tested way.

1. Write the card with Raspberry Pi Imager: Raspberry Pi OS Lite (64-bit)
   (under Raspberry Pi OS (other); device: Raspberry Pi 4).
   In Imager's settings: host name pelletpi, a user name and password, your Wi-Fi, your time zone.
2. Copy everything from this zip onto the card's boot drive ("bootfs"). The folder "pelletpi" and the
   file "vendor-data" must lie directly on the drive: bootfs\pelletpi\ and bootfs\vendor-data.
   On Windows, "Extract all" makes an extra "pelletpi-card" folder: move what is inside it up onto
   the drive. Change nothing else.
3. Wire the cable, plug it into J4 on the boiler and the USB adapter into the Pi.
   Then put the card in the Pi and switch the Pi on.
4. Wait about 15 minutes, then open http://pelletpi.local on your phone or PC (same Wi-Fi).
5. When the page opens, turn on IO BOKS on the boiler: MONTØR -> AVANCERET -> EKSTRA UDSTYR -> IO BOKS.
   Turned on before the monitor runs, it gives alarm 15 after about 50 s; then reset the alarm on the boiler.

Progress: put the card back in a PC and open pelletpi-status.txt on the boot drive,
or open http://pelletpi.local while it is installing.
The Pi needs internet the first time. Without it, it waits and tries again every minute.

pelletpi.local does not open?
- Wait the full 15 minutes.
- Use the same Wi-Fi as the Pi, not a guest network.
- Try the number address from the router's or phone hotspot's list of devices.
- Read pelletpi-status.txt on the card. No status file after 20 minutes means the files are in the
  wrong place (see step 2).

Wrong Wi-Fi? After 3 minutes without network the Pi opens the Wi-Fi "pelletpi-setup" (no password).
Join it with your phone, open http://10.42.0.1:8099 and pick your Wi-Fi (new, not tested on a real Pi).

Updates: on the page, Settings, About this monitor. "Check for update" only looks; "Install" installs.
The switch "Automatic updates at night" turns the nightly update on or off. It only updates while the
burner is off or waiting. AUTO_UPDATE=no in pelletpi/settings.txt turns it off for good.
No internet at the boiler: share it from your phone's hotspot for a few minutes and press the buttons.
