Pellet burner monitor - easy install
====================================

1. Write the card with Raspberry Pi Imager: Raspberry Pi OS Lite (64-bit).
   In Imager's settings: host name pelletpi, a user name and password, your Wi-Fi, your time zone.
2. Copy everything from this zip onto the card's boot drive ("bootfs"):
   the folder "pelletpi" and the file "vendor-data". Change nothing else.
3. Put the card in the Pi. Plug the USB adapter into the boiler's module port. Switch the Pi on.
4. Wait about 15 minutes, then open http://pelletpi.local on your phone or PC (same Wi-Fi).

Progress: put the card back in a PC and open pelletpi-status.txt on the boot drive,
or open http://pelletpi.local while it is installing.
The Pi needs internet the first time. Without it, it waits and tries again every minute.
Wrong Wi-Fi? After 3 minutes without network the Pi opens the Wi-Fi "pelletpi-setup" (no password).
Join it with your phone, open http://10.42.0.1:8099 and pick your Wi-Fi (new, not tested on a real Pi).

Auto-update: every night the Pi installs a new release, but only while the burner is off or
waiting. To turn it off, set AUTO_UPDATE=no in pelletpi/settings.txt.
