[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Src
)
$ErrorActionPreference = "Stop"

function Set-SourceReplacement {
  param(
    [Parameter(Mandatory)] [string]$RelativePath,
    [Parameter(Mandatory)] [string]$OldText,
    [Parameter(Mandatory)] [string]$NewText
  )
  $path = Join-Path $Src $RelativePath
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "resume source file is missing: $RelativePath"
  }
  $content = [IO.File]::ReadAllText($path)
  if ($content.Contains($OldText)) {
    [IO.File]::WriteAllText($path, $content.Replace($OldText, $NewText))
    Write-Host "==> updated restored source: $RelativePath"
    return
  }
  if (-not $content.Contains($NewText)) {
    throw "restored source does not contain the expected old or new text: $RelativePath"
  }
  Write-Host "==> restored source already current: $RelativePath"
}

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\mediarecorder\media_recorder.cc" `
  -OldText "const std::string ph_type = type.LowerASCII().Utf8();" `
  -NewText "const std::string ph_type = type.ToAsciiLower().Utf8();"

Set-SourceReplacement `
  -RelativePath "chrome\browser\component_updater\widevine_cdm_component_installer.cc" `
  -OldText '#include "base/json/json_file_value_deserializer.h"  // UXR' `
  -NewText '#include "base/json/json_file_value_serializer.h"  // UXR'

Set-SourceReplacement `
  -RelativePath "chrome\browser\component_updater\widevine_cdm_component_installer.cc" `
  -OldText "const base::Value::Dict* ph_dict =" `
  -NewText "const base::DictValue* ph_dict ="
