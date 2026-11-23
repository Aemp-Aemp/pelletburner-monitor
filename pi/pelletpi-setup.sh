#!/bin/sh
# One-time setup, started from cmdline.txt (systemd.run). Installs the USB-C network gadget and the
# boot status report, removes itself from cmdline.txt, then the Pi reboots.
set +e
LOG=/boot/firmware/pi-setup.log
exec >"$LOG" 2>&1
echo "setup start $(date)"

# 1) USB-C as a network cable that Windows recognises by itself (RNDIS + Microsoft OS descriptors)
cat >/usr/local/bin/pelletpi-usbnet <<'EOF'
#!/bin/sh
modprobe libcomposite
G=/sys/kernel/config/usb_gadget/pelletpi
[ -d "$G" ] && exit 0
mkdir -p "$G" && cd "$G" || exit 1
echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB
echo 0xEF > bDeviceClass
echo 0x02 > bDeviceSubClass
echo 0x01 > bDeviceProtocol
mkdir -p strings/0x409
echo "pelletpi-0001" > strings/0x409/serialnumber
echo "Pellet burner" > strings/0x409/manufacturer
echo "Pellet burner Pi" > strings/0x409/product
mkdir -p configs/c.1/strings/0x409
echo "RNDIS network" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower
echo 1 > os_desc/use
echo 0xcd > os_desc/b_vendor_code
echo MSFT100 > os_desc/qw_sign
mkdir -p functions/rndis.usb0
echo 02:50:42:00:00:01 > functions/rndis.usb0/dev_addr
echo 02:50:42:00:00:02 > functions/rndis.usb0/host_addr
echo RNDIS > functions/rndis.usb0/os_desc/interface.rndis/compatible_id
echo 5162001 > functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id
ln -s functions/rndis.usb0 configs/c.1/
ln -s configs/c.1 os_desc
ls /sys/class/udc > UDC
EOF
chmod 755 /usr/local/bin/pelletpi-usbnet
cat >/etc/systemd/system/pelletpi-usbnet.service <<'EOF'
[Unit]
Description=USB-C network cable to the PC
After=systemd-modules-load.service
Before=NetworkManager.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/pelletpi-usbnet
[Install]
WantedBy=multi-user.target
EOF

# 2) usb0 = 10.55.0.1, hands the PC an address
mkdir -p /etc/NetworkManager/system-connections
cat >/etc/NetworkManager/system-connections/usb0.nmconnection <<'EOF'
[connection]
id=usb0
type=ethernet
interface-name=usb0
autoconnect=true
[ipv4]
method=shared
address1=10.55.0.1/24
[ipv6]
method=link-local
EOF
chmod 600 /etc/NetworkManager/system-connections/usb0.nmconnection

# 3) status report on the boot partition after every start
cat >/usr/local/bin/pelletpi-diag <<'EOF'
#!/bin/sh
sleep 45
out=/boot/firmware/pi-diag.txt
{
  echo "== $(date)"; echo "== kernel $(uname -r)"
  echo "== addresses"; ip -br addr
  echo "== usb gadget"; ls /sys/kernel/config/usb_gadget/ 2>&1; cat /sys/kernel/config/usb_gadget/*/UDC 2>&1; ls /sys/class/udc 2>&1
  echo "== modules"; lsmod | grep -E 'dwc2|libcomposite|usb_f|g_' || true
  echo "== network manager"; nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>&1
  echo "== services"; for s in ssh docker NetworkManager pelletpi-usbnet; do echo "$s: $(systemctl is-active $s 2>&1)"; done
  echo "== docker"; docker --version 2>&1
  echo "== serial ports"; ls -l /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/ 2>&1
  echo "== cloud-init"; cloud-init status --long 2>&1 | head -n 20
  echo "== cloud-init output (last 30 lines)"; tail -n 30 /var/log/cloud-init-output.log 2>&1
  echo "== usbnet log"; journalctl -b -u pelletpi-usbnet --no-pager 2>&1 | tail -n 15
} > "$out" 2>&1
sync
EOF
chmod 755 /usr/local/bin/pelletpi-diag
cat >/etc/systemd/system/pelletpi-diag.service <<'EOF'
[Unit]
Description=Write a status report to the boot partition
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/pelletpi-diag
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pelletpi-usbnet.service pelletpi-diag.service ssh
echo "services enabled"

# 4) take this script out of cmdline.txt so it only runs once
sed -i 's| systemd.run=[^ ]*||; s| systemd.run_success_action=[^ ]*||; s| systemd.unit=kernel-command-line.target||' /boot/firmware/cmdline.txt
echo "cmdline now: $(cat /boot/firmware/cmdline.txt)"
echo "setup done $(date)"
sync
exit 0
