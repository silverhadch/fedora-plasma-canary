#!/usr/bin/env ruby
# frozen_string_literal: true
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2024-2025 Harald Sitter <sitter@kde.org>
# Hijack ninja to run the strip target instead of the default install target.
destdir = ENV.fetch('KDE_MASTER_INSTALL_DESTDIR', '/work/tree/install')
ARGV.each do |arg|
    next arg if arg != 'install'
    raise 'Failed to install with destdir' unless system({'DESTDIR' => destdir}, '/usr/bin/ninja.orig', *ARGV)
    break
end
exec('/usr/bin/ninja.orig', *ARGV)
