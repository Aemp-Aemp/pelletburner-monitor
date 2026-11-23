#!/bin/sh
# Open the page without a port number: http://pelletpi.local instead of http://pelletpi.local:8787
# Run once on the Pi:  sudo sh pi/enable-port-80.sh
# Port 80 is passed on to the program's port 8787 (8787 keeps working too). Survives restarts.
set -e
cat > /etc/systemd/system/pelletburner-port80.service <<'EOF'
[Unit]
Description=Pellet burner monitor: web page on port 80
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 8787 2>/dev/null || iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 8787'
ExecStop=/bin/sh -c 'iptables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 8787 || true'

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pelletburner-port80.service
echo "Done: open http://$(hostname).local"
