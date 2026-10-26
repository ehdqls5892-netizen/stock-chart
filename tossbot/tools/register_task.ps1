# 작업 스케줄러에 '평일 장 시작 10분 전에 로봇 켜기'를 등록합니다.
# 사용: 4_자동실행등록.bat 이 이 파일을 실행합니다.
param([string]$Mode = "paper")
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = Split-Path -Parent $dir
$cfg = Get-Content "$dir\config.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$ms = @($cfg.markets); if ($ms.Count -eq 0) { $ms = @($cfg.market) }
$market = if ($ms -contains "KR") { "KR" } else { "US" }   # 하루 중 먼저 열리는 장 10분 전에 켬 (국장·미장 둘 다 한 번에 돌아감)
if ($market -eq "KR") { $tzId = "Korea Standard Time"; $h = 8; $m = 50 } else { $tzId = "Eastern Standard Time"; $h = 9; $m = 20 }
$src = [TimeZoneInfo]::FindSystemTimeZoneById($tzId)
$base = [DateTime]::SpecifyKind((Get-Date).Date.AddHours($h).AddMinutes($m), [DateTimeKind]::Unspecified)
$local = [TimeZoneInfo]::ConvertTime($base, $src, [TimeZoneInfo]::Local)
$bat = if ($Mode -eq "live") { "3_실전모드.bat" } else { "2_연습모드.bat" }
$name = "TossBot"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$dir\$bat`"" -WorkingDirectory $dir
$days = "Monday","Tuesday","Wednesday","Thursday","Friday"
if ($local.Date -lt $base.Date) { $days = "Sunday","Monday","Tuesday","Wednesday","Thursday" }
if ($local.Date -gt $base.Date) { $days = "Tuesday","Wednesday","Thursday","Friday","Saturday" }
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $local.ToString("HH:mm")
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host ""
Write-Host "등록 완료: $name ($Mode 모드)"
Write-Host ("이 PC 시간으로 매주 " + ($days -join ",") + " " + $local.ToString("HH:mm") + " 에 켜집니다.")
Write-Host "PC 가 잠자기 상태면 깨워서 실행합니다(전원 연결 권장). 완전히 꺼져 있으면 실행되지 않아요."
Write-Host "지우려면: 작업 스케줄러 앱에서 $name 삭제"
