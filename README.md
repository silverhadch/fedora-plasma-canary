# ⚙️ ublue-kde-dx

A custom bootc image with git master Plasma and KDE Gears, plus a full KDE development toolchain. Perfect for KDE developers who want to hack on Plasma itself.

## Features

- Plasma (git master)
- KDE Gears (git master)
- KDE development tools and dependencies
- Development utilities: `kdevelop`, `flatpak-builder`, `clang`, `neovim`, `zsh`
- Preinstalled `kde-builder` with shell completions
- Enabled system services: `podman.socket`, `NetworkManager`, `bluetooth`, `cups`, and more

## Images

| Image | Status |
|---|---|
| `ublue-kde-dx:latest` | ✅ Stable |
| `ublue-kde-dx-nvidia:latest` | ✅ Stable |
| `ublue-kde-dx-dev:dev` | ⚠️ WIP |
| `ublue-kde-dx-dev-nvidia:dev` | ⚠️ WIP |

> [!WARNING]
> **`-dev` images are experimental and will be broken.**
> They are built from the `dev` branch and may fail to boot or produce a broken desktop at any time. Do not use them.

## Rebase to This Image

### Regular (AMD/Intel)

```bash
sudo rpm-ostree rebase ostree-unverified-registry:ghcr.io/silverhadch/ublue-kde-dx:latest
```

### NVIDIA

```bash
sudo rpm-ostree rebase ostree-unverified-registry:ghcr.io/silverhadch/ublue-kde-dx-nvidia:latest
```

Reboot afterwards to apply the image.

## Credits

Customizations by @silverhadch. Based on Fedora and the [Universal Blue](https://universal-blue.org) project.
