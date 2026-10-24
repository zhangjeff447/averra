"""Fail-closed Windows ACL check before using the existing MCP OAuth credential file."""
import os,subprocess

def private(path):
    script = r'''$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:AVERRA_ACL_PATH; $u=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $allowed=@($u,'S-1-5-18','S-1-5-32-544'); $ok=$allowed -contains $a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; foreach($r in $a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])) { if($r.AccessControlType -eq 'Allow' -and (($r.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::ReadData) -ne 0) -and ($allowed -notcontains $r.IdentityReference.Value)) {$ok=$false} }; if($ok){'PRIVATE'}else{'UNSAFE'}'''
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],env={**os.environ,'AVERRA_ACL_PATH':str(path)},capture_output=True,text=True,timeout=10)
        return result.returncode==0 and result.stdout.strip()=='PRIVATE'
    except (OSError,subprocess.TimeoutExpired):return False
