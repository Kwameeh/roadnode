#!/usr/bin/env bash
set -Eeuo pipefail

# Used by install/update so the unprivileged web service can manage Wi-Fi.
NETWORK_USER="${1:?Pass the web service user}"
sudo apt install -y network-manager polkitd
sudo groupadd --force roadnode-network
sudo usermod -aG roadnode-network "$NETWORK_USER"
sudo install -d -m 0755 /etc/polkit-1/rules.d
sudo tee /etc/polkit-1/rules.d/49-roadnode-network.rules >/dev/null <<'RULE'
polkit.addRule(function(action, subject) {
    var allowed = [
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.settings.modify.system",
        "org.freedesktop.NetworkManager.enable-disable-wifi",
        "org.freedesktop.NetworkManager.wifi.scan"
    ];
    if (subject.isInGroup("roadnode-network") && allowed.indexOf(action.id) !== -1) {
        return polkit.Result.YES;
    }
});
RULE
sudo chmod 0644 /etc/polkit-1/rules.d/49-roadnode-network.rules
