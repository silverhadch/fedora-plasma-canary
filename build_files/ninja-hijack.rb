#!/usr/bin/env ruby
# frozen_string_literal: true
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2024-2025 Harald Sitter <sitter@kde.org>
# Hijack ninja to run the strip target instead of the default install target.
#
# Every project installs into its own tree under the destdir root, named after
# its build directory (directory-layout: flat, so that is the project name),
# and package-kde.py turns each tree into one RPM. The real install into /usr
# still happens afterwards so later projects build against this one.
root = ENV.fetch('KDE_MASTER_INSTALL_DESTDIR', '/work/tree/install')
build_dir = (i = ARGV.index('-C')) ? ARGV[i + 1] : Dir.pwd
destdir = File.join(root, File.basename(File.expand_path(build_dir)))
ARGV.each do |arg|
    next arg if arg != 'install'
    raise 'Failed to install with destdir' unless system({'DESTDIR' => destdir}, '/usr/bin/ninja.orig', *ARGV)
    break
end
exec('/usr/bin/ninja.orig', *ARGV)
