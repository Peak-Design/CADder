<#
.SYNOPSIS
    Links this repository into the addons folder of a Blender installation.

.DESCRIPTION
    The repository lives outside the Blender application data folder, so a
    new Blender version needs a new link and not a new copy. The link is a
    directory junction, which needs no administrator rights and which
    Blender and Python follow like a real folder.

    With no version, the script links every installed Blender that the
    addon supports (blender_manifest.toml says which).

    The script never deletes a real folder. If the addons folder already
    holds a CADder directory that is not a link, the script says so and
    stops, so an installed copy is never lost by accident.

.EXAMPLE
    .\tools\Link-Addon.ps1
    .\tools\Link-Addon.ps1 -Version 5.2
    .\tools\Link-Addon.ps1 -Version 5.1 -Remove
#>
[CmdletBinding()]
param(
    [string] $Version,
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$name = Split-Path -Leaf $repo
$manifest = Join-Path $repo 'blender_manifest.toml'
if (-not (Test-Path $manifest)) {
    throw "no blender_manifest.toml in $repo. Run this from the addon repository."
}

# The lowest Blender the addon runs on, read from the manifest so there is
# one place that says it.
$min = [version] '0.0'
$line = Select-String -Path $manifest -Pattern '^\s*blender_version_min\s*=\s*"([^"]+)"'
if ($line) { $min = [version] $line.Matches[0].Groups[1].Value }

# Remove-Item on a junction asks "the item has children" in Windows
# PowerShell 5.1, and answering yes deletes what the junction POINTS AT.
# Directory.Delete removes the link itself and never follows it.
function Remove-Junction([string] $path) {
    [System.IO.Directory]::Delete($path)
}

$root = Join-Path $env:APPDATA 'Blender Foundation\Blender'
if (-not (Test-Path $root)) { throw "no Blender application data folder at $root" }

if ($Version) {
    $versions = @($Version)
} else {
    $versions = Get-ChildItem $root -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+$' } |
        Where-Object { [version] "$($_.Name).0" -ge [version] "$($min.Major).$($min.Minor).0" } |
        ForEach-Object { $_.Name }
}
if (-not $versions) {
    Write-Host "No installed Blender is $min or newer. Nothing to link."
    return
}

foreach ($v in $versions) {
    $addons = Join-Path $root "$v\scripts\addons"
    $link = Join-Path $addons $name

    $item = Get-Item -LiteralPath $link -ErrorAction SilentlyContinue
    $isLink = $item -and $item.LinkType

    if ($Remove) {
        if (-not $item) { Write-Host "$v : nothing there"; continue }
        if (-not $isLink) { Write-Host "$v : $link is a real folder, left alone"; continue }
        Remove-Junction $link
        Write-Host "$v : link removed"
        continue
    }

    if ($item -and -not $isLink) {
        Write-Host "$v : $link is a real folder. Move or delete it first."
        continue
    }
    if ($isLink) { Remove-Junction $link }

    New-Item -ItemType Directory -Path $addons -Force | Out-Null
    New-Item -ItemType Junction -Path $link -Target $repo | Out-Null
    Write-Host "$v : $link -> $repo"
}
