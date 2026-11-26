param([Parameter(Mandatory)][string]$ProjectPath)

$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($ProjectPath).TrimEnd('\')
foreach ($editor in Get-CimInstance Win32_Process -Filter "Name = 'Unity.exe'") {
    if ($editor.CommandLine -notmatch '(?i)(?:^|\s)-projectPath\s+(?:"([^"]+)"|(\S+))') { continue }
    $path = ($Matches[1] + $Matches[2])
    if ($path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') { continue }
    if ([IO.Path]::GetFullPath($path).TrimEnd('\') -ne $target) { continue }

    $process = Get-Process -Id $editor.ProcessId -ErrorAction SilentlyContinue
    if (!$process -or $process.HasExited) { continue }
    if (!$process.CloseMainWindow()) { exit 1 }
    if (!$process.WaitForExit(120000)) { exit 2 }
}
