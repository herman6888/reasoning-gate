$ErrorActionPreference = "SilentlyContinue"
function Is-Listening($port) { (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -ne $null }
if (-not (Is-Listening 8901)) {
  Start-Process -FilePath "C:\Users\Public\kev-poc\build\dohnuts-cpp\build-hip\bin\dohnuts-cli.exe" `
    -ArgumentList "--model","C:\Users\Public\kev-poc\dohnuts\kev-4b-q8_0.gguf","--head","C:\Users\Public\kev-poc\dohnuts\kev-head.f32","--metadata","C:\Users\Public\kev-poc\dohnuts\kev.json","--gpu-layers","-1","--server","--host","0.0.0.0","--port","8901" `
    -WindowStyle Hidden -RedirectStandardOutput C:\Users\Public\kev-poc\dohnuts-stdout.log -RedirectStandardError C:\Users\Public\kev-poc\dohnuts-stderr.log
}
if (-not (Is-Listening 8905)) {
  Start-Process -FilePath "I:\AI_Work\ComfyUI-JZL\python_embeded\python.exe" -ArgumentList "C:\Users\Public\kev-poc\kev_relay.py" -WindowStyle Hidden -RedirectStandardOutput C:\Users\Public\kev-poc\relay-stdout.log -RedirectStandardError C:\Users\Public\kev-poc\relay-stderr.log
}
