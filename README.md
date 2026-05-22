# 🐦 fedora-plasma-canary

A Fedora bootc image with git master Plasma and KDE Gears, plus a full KDE development toolchain. Perfect for KDE developers who want to hack on Plasma itself.

## Features

- Plasma (git master)
- KDE Gears (git master)
- KDE development tools and dependencies
- Development utilities: `kdevelop`, `flatpak-builder`, `clang`, `neovim`, `zsh`
- Preinstalled `kde-builder` with shell completions
- Enabled system services: `podman.socket`, `NetworkManager`, `bluetooth`, `cups`, and more

## ⚠️ This is a canary image

This image tracks KDE git master and Fedora rawhide. It **will** break. It is intended for KDE developers and contributors who want to run and hack on the latest Plasma, not for daily driving by regular users.

## Images

| Image | Status |
|---|---|
| `fedora-plasma-canary:latest` | 🐦 Canary |

> [!NOTE]
> The `dev` branch and any `-dev` tagged images are my personal testing ground for experimenting with the image itself before changes land on `main`. They are not releases and are not meant for anyone else to use.

## Rebase to This Image

```bash
sudo rpm-ostree rebase ostree-unverified-registry:ghcr.io/silverhadch/fedora-plasma-canary:latest
```

Reboot afterwards to apply the image.

## Credits

Customizations by @silverhadch. Based on [Fedora](https://fedoraproject.org) and [KDE](https://kde.org).
