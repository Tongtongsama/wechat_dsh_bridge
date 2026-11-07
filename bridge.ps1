<#
  桥接进程管理：启动 / 停止 / 查看状态
  用法：
    .\bridge.ps1 -Action start
    .\bridge.ps1 -Action stop
    .\bridge.ps1 -Action status
  或用同目录的 启动.cmd / 停止.cmd / 状态.cmd（双击即可，已带 Bypass）

  启动方式说明：用 WMI(Win32_Process.Create) 创建进程，而不是 Start-Process。
  因为 Start-Process 出来的子进程属于当前 shell 的作业对象，一旦父 shell 退出
  （或被沙箱/任务管理器清理整棵进程树），桥接就会跟着静默死掉——这已经发生过
  三次，症状都是"突然不回复了，日志无报错"。
#>
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $here 'state\bridge.pid'
$logDir = Join-Path $here 'logs'
$outLog = Join-Path $logDir 'bridge.log'
$errLog = Join-Path $logDir 'bridge.log.err'

function Find-Python {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { return $cmd.Source }
    throw '找不到可用的 Python（WindowsApps 里的 python 是应用商店占位符），请安装 Python 3.12+'
}

function Find-PythonW {
    # 用 pythonw（GUI 子系统、无控制台）跑常驻进程：否则调用方控制台一关，
    # python 会收到 CTRL_CLOSE_EVENT 并静默退出，日志里一个字都不会留。
    $py = Find-Python
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (Test-Path $pyw) { return $pyw }
    return $py
}

function Get-BridgeProcess {
    $found = @()
    foreach ($cim in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if ($cim.CommandLine -and $cim.CommandLine -like '*bridge_main.py*' -and
            ($cim.Name -eq 'python.exe' -or $cim.Name -eq 'pythonw.exe')) {
            $p = Get-Process -Id $cim.ProcessId -ErrorAction SilentlyContinue
            if ($p) { $found += $p }
        }
    }
    return $found
}

function Get-Running {
    $ps = Get-BridgeProcess
    if ($ps.Count -gt 0) { return $ps[0] }
    return $null
}

try {
    switch ($Action) {
        'start' {
            $running = Get-Running
            if ($running) {
                Write-Output "已经在运行了 (PID $($running.Id))。要重启请先 stop。"
                exit 0
            }
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
            New-Item -ItemType Directory -Force -Path (Join-Path $here 'state') | Out-Null

            $py = Find-PythonW
            $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            Add-Content -Path $outLog -Value "`n===== $stamp 启动 =====" -Encoding utf8

            # 直接用 pythonw（无控制台、无 cmd 外壳）：日志由 Python 自己写进
            # logs/bridge.log，启动时不会再闪一个 cmd 黑框。
            $cmdline = '"{0}" bridge_main.py' -f $py
            $created = $null
            try {
                $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
                    CommandLine      = $cmdline
                    CurrentDirectory = $here
                }
            } catch {
                $created = $null
            }

            if ($created -and $created.ReturnValue -eq 0) {
                Write-Output "已用 WMI 启动（脱离当前 shell 的作业对象）"
            } else {
                Write-Output "WMI 启动不可用，退回 Start-Process，可能随父 shell 退出而被终止。"
                Start-Process -FilePath $py -ArgumentList 'bridge_main.py' `
                    -WorkingDirectory $here -RedirectStandardOutput $outLog `
                    -RedirectStandardError $errLog -WindowStyle Hidden | Out-Null
            }

            Start-Sleep -Seconds 5
            $proc = Get-Running
            if ($proc) {
                $proc.Id | Set-Content $pidFile -Encoding utf8
                Write-Output "已启动 (PID $($proc.Id))"
                Write-Output "日志：$outLog"
                Write-Output "关闭：运行 停止.cmd 或 .\bridge.ps1 -Action stop"
            } else {
                Write-Output "启动后没检测到桥接进程，日志末尾："
                if (Test-Path $outLog) { Get-Content $outLog -Encoding utf8 -Tail 20 }
                if (Test-Path $errLog) { Get-Content $errLog -Encoding utf8 -Tail 20 }
                Remove-Item $pidFile -ErrorAction SilentlyContinue
                exit 1
            }
        }
        'stop' {
            $procs = Get-BridgeProcess
            if ($procs.Count -eq 0) {
                Write-Output '没有在运行。'
                Remove-Item $pidFile -ErrorAction SilentlyContinue
                exit 0
            }

            # 先在 state 里放标记：桥接看到它会自己发完下线通知再退出。
            # 直接 Stop-Process 是硬杀，进程没有机会说话。
            New-Item -ItemType Directory -Force -Path (Join-Path $here 'state') | Out-Null
            Set-Content -Path (Join-Path $here 'state\shutdown_requested') -Value '1' -Encoding utf8
            Write-Output '已请求停止，等它发完下线通知…'

            $waited = 0
            while ($waited -lt 20) {
                Start-Sleep -Seconds 1
                $waited++
                if ((Get-BridgeProcess).Count -eq 0) { break }
            }

            $left = Get-BridgeProcess
            if ($left.Count -gt 0) {
                foreach ($p in $left) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
                Remove-Item (Join-Path $here 'state\shutdown_requested') -ErrorAction SilentlyContinue
                Write-Output '等了 20 秒还没退，已强制停止（下线通知可能没发出去）'
            } else {
                Write-Output '已停止（下线通知已发出）'
            }
            Remove-Item $pidFile -ErrorAction SilentlyContinue
        }
        'restart' {
            New-Item -ItemType Directory -Force -Path (Join-Path $here 'state') | Out-Null
            Set-Content -Path (Join-Path $here 'state\restart_pending') -Value '1' -Encoding utf8
            & $PSCommandPath -Action stop
            Start-Sleep -Seconds 2
            & $PSCommandPath -Action start
            exit 0
        }
        'status' {
            $proc = Get-Running
            if ($proc) {
                Write-Output "运行中 (PID $($proc.Id))"
                if (Test-Path $outLog) {
                    Write-Output ''
                    Write-Output '--- 最近日志 ---'
                    Get-Content $outLog -Encoding utf8 -Tail 12
                }
            } else {
                Write-Output '未运行'
            }
        }
    }
} catch {
    Write-Output "出错：$($_.Exception.Message)"
    exit 1
}
