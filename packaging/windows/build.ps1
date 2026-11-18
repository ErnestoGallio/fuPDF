param(
    [ValidateSet('Console', 'Windowed')][string]$Mode = 'Console',
    [switch]$Probe,
    [switch]$PreflightOnly,
    [switch]$DistributionCandidate,
    [switch]$SystemVCRuntime
)
$ErrorActionPreference = 'Stop'
if ($SystemVCRuntime -and -not $DistributionCandidate) {
    throw 'SystemVCRuntime requires DistributionCandidate.'
}
if ($DistributionCandidate -and ($Probe -or $Mode -ne 'Windowed')) {
    throw 'DistributionCandidate requires Windowed mode without Probe.'
}
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$helper = Join-Path $PSScriptRoot 'build_environment.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Repository Python venv is missing.' }
# Isolated stdlib startup ignores inherited PYTHONHOME/PYTHONPATH and site hooks.
$environmentJson = & $python -I -S $helper environment
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine trusted build environment.' }
$environment = $environmentJson | ConvertFrom-Json
$previous = @{}
foreach ($property in $environment.PSObject.Properties) {
    $previous[$property.Name] = [Environment]::GetEnvironmentVariable($property.Name, 'Process')
}
$previous['FUPDF_BUILD_MODE'] = $env:FUPDF_BUILD_MODE
$previous['PYINSTALLER_CONFIG_DIR'] = $env:PYINSTALLER_CONFIG_DIR
$previous['FUPDF_SYSTEM_VC_RUNTIME'] = $env:FUPDF_SYSTEM_VC_RUNTIME
try {
    foreach ($property in $environment.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, $property.Value, 'Process')
    }
    $workRoot = Join-Path $repoRoot 'build\windows'
    $distRoot = Join-Path $repoRoot 'dist\windows'
    $label = if ($Probe) { 'QtProbeSanitized' } elseif ($Mode -eq 'Windowed') { 'Windowed' } else { 'ConsoleSanitized' }
    if ($DistributionCandidate) { $label = 'Python-3.14.10-distribution-candidate' }
    if ($SystemVCRuntime) { $label = 'Python-3.14.10-system-crt-candidate' }
    $work = Join-Path $workRoot $label
    $dist = if ($Probe) { Join-Path $distRoot 'Diagnostics' } else { Join-Path $distRoot $label }
    $name = if ($Probe) { 'QtProbeSanitized' } else { 'fuPDF' }
    $bundle = Join-Path $dist $name
    # Delete only the selected new build trees, preserving the failed artifacts.
    foreach ($entry in @(@($workRoot, $work), @($distRoot, $bundle))) {
        $parentPath = [IO.Path]::GetFullPath($entry[0]).TrimEnd('\') + '\'
        $targetPath = [IO.Path]::GetFullPath($entry[1])
        if (-not $targetPath.StartsWith($parentPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup outside packaging output: $targetPath"
        }
        $checkPath = $targetPath
        while ($checkPath.Length -gt $repoRoot.Length) {
            if ((Test-Path -LiteralPath $checkPath) -and
                ((Get-Item -LiteralPath $checkPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Refusing cleanup through a reparse point: $checkPath"
            }
            $checkPath = Split-Path -Parent $checkPath
        }
        if (-not $PreflightOnly -and (Test-Path -LiteralPath $targetPath)) {
            Remove-Item -LiteralPath $targetPath -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $work 'pyinstaller-cache'
    & $python -I $helper preflight --output (Join-Path $work 'environment.json')
    if ($LASTEXITCODE -ne 0) { throw 'Build environment preflight failed.' }
    & $python -I (Join-Path $PSScriptRoot 'prepare.py')
    if ($LASTEXITCODE -ne 0) { throw 'Packaging resource preparation failed.' }
    if ($PreflightOnly) { return }
    $env:FUPDF_BUILD_MODE = $Mode
    $env:FUPDF_SYSTEM_VC_RUNTIME = if ($SystemVCRuntime) { '1' } else { '0' }
    if ($Probe) {
        & $python -I -m PyInstaller --noconfirm --clean --onedir --console --noupx --name $name --specpath $work --workpath $work --distpath $dist (Join-Path $PSScriptRoot 'qt_probe.py')
        $toc = Join-Path $work "$name\COLLECT-00.toc"
    } else {
        & $python -I -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist (Join-Path $PSScriptRoot 'fupdf.spec')
        $toc = Join-Path $work 'fupdf\COLLECT-00.toc'
    }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $python -I $helper provenance --toc $toc --bundle $bundle --output (Join-Path $work 'native-inventory.json')
    if ($LASTEXITCODE -ne 0) { throw 'Native provenance gate failed; output is NOT qualified.' }
    if ($DistributionCandidate) {
        $portableArgs = @('portable', '--bundle', $bundle, '--inventory', (Join-Path $work 'native-inventory.json'))
        if ($SystemVCRuntime) { $portableArgs += '--system-vc-runtime' }
        & $python -I $helper @portableArgs
        if ($LASTEXITCODE -ne 0) { throw 'Portable contents gate failed; output is NOT qualified.' }
    }
    Write-Output (Join-Path $bundle "$name.exe")
} finally {
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
}
