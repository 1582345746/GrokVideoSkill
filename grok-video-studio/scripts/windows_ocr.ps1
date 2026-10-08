param(
    [Parameter(Mandatory=$true)][string]$ImagePath,
    [string]$Language = 'zh-Hans-CN'
)
# Uses Windows' already installed OCR language packs. Never downloads a model.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
    $null=[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
    $null=[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]
    $null=[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
    $null=[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]
    $null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
    $null=[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
    $null=[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
    $taskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {$_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1} |
        Select-Object -First 1
    function Await($operation,[Type]$resultType) {
        $task=$taskMethod.MakeGenericMethod($resultType).Invoke($null,@($operation))
        $task.Wait()
        return $task.Result
    }
    function Normalize-CjkSpaces([string]$text) {
        return [regex]::Replace($text, '(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '')
    }
    $available=@([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object {$_.LanguageTag})
    if ($available -notcontains $Language) {
        throw "OCR language $Language is not installed. Available: $($available -join ', ')."
    }
    $engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
    if ($null -eq $engine) {throw 'Windows OCR engine is unavailable.'}
    $resolved=(Resolve-Path -LiteralPath $ImagePath).Path
    $storage=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolved)) ([Windows.Storage.StorageFile])
    $stream=Await ($storage.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    if ([Math]::Max($decoder.PixelWidth,$decoder.PixelHeight) -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
        throw "Image is larger than Windows OCR MaxImageDimension $([Windows.Media.Ocr.OcrEngine]::MaxImageDimension); caller must resize and record coordinate scale."
    }
    $bitmap=Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $ocr=Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines=@()
    foreach ($line in $ocr.Lines) {
        $words=@()
        foreach ($word in $line.Words) {
            $rect=$word.BoundingRect
            $words += [pscustomobject]@{text=$word.Text;x=$rect.X;y=$rect.Y;width=$rect.Width;height=$rect.Height;confidence=$null}
        }
        $lines += [pscustomobject]@{text=(Normalize-CjkSpaces $line.Text);raw_text=$line.Text;words=$words;confidence=$null}
    }
    [pscustomobject]@{
        engine='windows-winrt';language=$Language;text=(Normalize-CjkSpaces $ocr.Text);raw_text=$ocr.Text;
        lines=$lines;source_path=$resolved;width=$decoder.PixelWidth;height=$decoder.PixelHeight;
        confidence=$null;scope='single-image';verified_full_video=$false
    } | ConvertTo-Json -Depth 8 -Compress
    $bitmap.Dispose();$stream.Dispose()
    exit 0
} catch {
    [pscustomobject]@{engine='windows-winrt';error=$_.Exception.Message;language=$Language} | ConvertTo-Json -Compress
    exit 1
}
