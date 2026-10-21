#!/usr/bin/env bash
# Tradewind installer for Linux and Raspberry Pi (Raspberry Pi OS, Debian, Ubuntu, Mint).
# Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB) - MIT License
#
#   bash install_linux.sh            install, then run with:  tradewind
#   bash install_linux.sh --sdr      also install the RTL-SDR tools (receive with a dongle)
#   bash install_linux.sh --service  start Tradewind automatically at power-on (headless Pi)
#
# With --service the face plate is reachable from phones on your network at
# http://<pi-address>:8073 - set a station PIN under MENU > STATION first.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SDR=0; SERVICE=0
for a in "$@"; do
  case "$a" in
    --sdr) SDR=1 ;;
    --service) SERVICE=1 ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
  esac
done

if [ ! -f "$DIR/app.py" ]; then
  echo "Run this script from the Tradewind folder (the one with app.py)."; exit 1
fi

echo "== Installing system packages (asks for your password) =="
PKGS="python3 python3-venv python3-pip libportaudio2"
[ "$SDR" = 1 ] && PKGS="$PKGS rtl-sdr"
sudo apt-get update
sudo apt-get install -y $PKGS

echo "== Setting up Python packages =="
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

echo "== Self-test =="
(cd "$DIR" && "$DIR/.venv/bin/python" test_modem.py) || echo "Self-test reported a problem - Tradewind may still run; see above."

# serial ports (CAT / PTT / TNC), sound cards and USB dongles
sudo usermod -aG dialout,audio,plugdev "$USER" || true

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
cat > "$HOME/.local/bin/tradewind" <<EOF
#!/usr/bin/env bash
cd "$DIR" && exec "$DIR/.venv/bin/python" app.py "\$@"
EOF
chmod +x "$HOME/.local/bin/tradewind"

cat > "$HOME/.local/share/applications/tradewind.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Tradewind
Comment=Caribbean VHF/UHF propagation alerts and APRS chat
Exec=$HOME/.local/bin/tradewind
Icon=$DIR/icon.png
Terminal=true
Categories=HamRadio;Network;
EOF

if [ "$SERVICE" = 1 ]; then
  echo "== Starting Tradewind at power-on =="
  sudo tee /etc/systemd/system/tradewind.service > /dev/null <<EOF
[Unit]
Description=Tradewind APRS station
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python app.py --host 0.0.0.0 --no-browser --autostart
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now tradewind
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo
  echo "Tradewind is running. Open http://${IP:-<this-computer>}:8073 on a phone or PC on your network."
  echo "First visit from another device: set a station PIN on this computer (MENU > STATION) - "
  echo "or open http://127.0.0.1:8073 here. Stop it with: sudo systemctl stop tradewind"
else
  echo
  echo "Installed. Log out and back in once (for radio/USB permissions), then run:  tradewind"
  echo "Tradewind also appears in your applications menu."
fi
