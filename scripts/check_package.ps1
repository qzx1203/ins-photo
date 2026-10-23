param([string]$ApplicationPath)

$ErrorActionPreference = 'Stop'
if (-not ('ShiguangWindowProbe' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ShiguangWindowProbe {
    private delegate bool WindowCallback(IntPtr window, IntPtr value);
    [DllImport("user32.dll")] private static extern bool EnumWindows(WindowCallback callback, IntPtr value);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    public static IntPtr Find(int processId) {
        IntPtr result = IntPtr.Zero;
        EnumWindows((window, value) => {
            uint owner; GetWindowThreadProcessId(window, out owner);
            if (owner == processId) {
                var title = new StringBuilder(512);
                GetWindowText(window, title, title.Capacity);
                if (title.ToString().Contains("Instagram")) { result = window; return false; }
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
    public static void Close(IntPtr window) { PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero); }
}
'@
}
$projectDir = Split-Path -Parent $PSScriptRoot
if (-not $ApplicationPath) { $ApplicationPath = Join-Path $projectDir 'dist\Shiguang\Shiguang.exe' }
$applicationProcess = Start-Process -FilePath $applicationPath -WorkingDirectory $env:TEMP -WindowStyle Hidden -PassThru
try {
    [void]$applicationProcess.WaitForInputIdle(20000)
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $applicationProcess.Refresh()
        if ($applicationProcess.HasExited) { throw 'The packaged application exited during startup.' }
        $applicationWindow = [ShiguangWindowProbe]::Find($applicationProcess.Id)
        if ($applicationWindow -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($applicationWindow -eq [IntPtr]::Zero) {
        throw "Application window was not found. Actual title: $($applicationProcess.MainWindowTitle)"
    }
    [ShiguangWindowProbe]::Close($applicationWindow)
    if (-not $applicationProcess.WaitForExit(5000)) { throw 'The application did not close normally.' }
    if ($applicationProcess.ExitCode -ne 0) { throw "Application exit code: $($applicationProcess.ExitCode)" }
    Write-Host 'Package startup and graceful shutdown passed.'
} finally {
    $applicationProcess.Refresh()
    if (-not $applicationProcess.HasExited) { Stop-Process -Id $applicationProcess.Id }
}
