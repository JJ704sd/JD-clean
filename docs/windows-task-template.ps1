# Windows 计划任务命令模板（仅供人工确认）
#
# 本文件不会自动注册计划任务、写入凭据或启动 worker。请确认项目路径、
# 数据库和输入目录后，再将显示的命令粘贴到管理员已批准的配置流程中。

$ProjectRoot = 'D:\JD clean'
$Database = Join-Path $ProjectRoot 'var\screening-v8.sqlite3'
$InputDirectory = Join-Path $env:USERPROFILE 'Downloads'
$Arguments = @(
    'run', '--locked', 'python', '-m', 'resume_screening',
    '--database', $Database,
    'worker', '--watch', '--input', $InputDirectory, '--auto-route',
    '--poll-seconds', '5'
)

Write-Output '待确认的工作目录：'
Write-Output $ProjectRoot
Write-Output '待确认的命令：'
$QuotedArguments = $Arguments | ForEach-Object {
    if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\\"') + '"' } else { $_ }
}
Write-Output ('uv ' + ($QuotedArguments -join ' '))
Write-Output ''
Write-Output '请在任务计划程序中将“起始于”设置为上述工作目录。'
Write-Output 'MINIMAX_API_KEY 应通过受控的用户环境或凭据注入配置，不要写入此文件或命令行。'

# 人工确认后可将上述命令配置为登录/开机触发的任务；本模板不调用
# 系统任务注册命令，也不实际启动后台进程。
