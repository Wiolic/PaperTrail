Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
Set WshShell = CreateObject("WScript.Shell")
stopFlag = repoRoot & "\.STOP_PAPERTRAIL"

' 清理上次残留的停止标志
If fso.FileExists(stopFlag) Then fso.DeleteFile stopFlag

' 守护循环: Streamlit 被杀/崩溃后自动重启 (3秒延迟)
' 停止方式: 运行 "Stop PaperTrail.bat" 或手动创建 .STOP_PAPERTRAIL 文件
firstRun = True
Do
    cmd = "cmd /c cd /d """ & repoRoot & """ && streamlit run scripts\ui\app.py --server.port 8501 --server.maxUploadSize 50"
    ' 首次启动时延迟后主动打开浏览器(隐藏窗口下 Streamlit 可能不自动弹)
    If firstRun Then
        WshShell.Run cmd, 0, False
        WScript.Sleep 4000
        WshShell.Run "http://localhost:8501", 1, False
        firstRun = False
        ' 等待 Streamlit 进程结束(通过轮询端口)
        Do
            WScript.Sleep 5000
            If fso.FileExists(stopFlag) Then Exit Do
            On Error Resume Next
            Set http = CreateObject("MSXML2.XMLHTTP")
            http.Open "GET", "http://localhost:8501/_stcore/health", False
            http.Send
            alive = (http.Status = 200)
            Set http = Nothing
            On Error GoTo 0
            If Not alive Then Exit Do
        Loop
    Else
        WshShell.Run cmd, 0, True
    End If
    If fso.FileExists(stopFlag) Then
        fso.DeleteFile stopFlag
        Exit Do
    End If
    WScript.Sleep 3000
Loop
