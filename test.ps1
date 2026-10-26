# Invoke-WebRequest -Uri "https://go2tencentcloud-1251783334.cos.accelerate.myqcloud.com/lighthouse/windows.ps1" -OutFile "windows.ps1"; .\windows.ps1
param($secretId,$secretKey,$sessionToken)
function before
{
}
function downloadTool
{
    $client = New-Object System.Net.WebClient
    $url = "https://go2tencentcloud-1251783334.cos.accelerate.myqcloud.com/latest/go2tencentcloud.zip"
    $path = "C:\\Windows\\System32\\go2tencentcloud.zip"
    $exist = (Test-Path $path)
    if ($exist -eq "True") {
        Remove-Item -Force $path
    }
    $client.DownloadFile($url, $path)
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
function unzip
{
    param([string]$zipfile, [string]$outpath)
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zipfile, $outpath)
}
function run
{
    $path = "C:\\Windows\\System32\\go2tencentcloud"
    $exist = (Test-Path $path)
    if ($exist -eq "True") {
        Remove-Item -Force -Recurse $path
    }
    unzip C:\\Windows\\System32\\go2tencentcloud.zip C:\\Windows\\System32
    cd C:\\Windows\\System32\\go2tencentcloud\\go2tencentcloud-windows
    if ($null -ne $sessionToken) {
        .\\go2tencentcloud_x64.exe -lighthouse --secret-id=$secretId --secret-key=$secretKey --session-token=$sessionToken
    } else {
        .\\go2tencentcloud_x64.exe -lighthouse --secret-id=$secretId --secret-key=$secretKey
    }
}
function main
{
    before
    downloadTool
    run
}
main