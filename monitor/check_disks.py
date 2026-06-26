import json
from winrm_client import run_ps


def check_server(server: dict) -> dict:
    """
    Получает полную информацию с сервера:
    - список дисков
    - загрузка CPU
    - память RAM
    """
    service_specs = []
    for service in server.get("services", []):
        if isinstance(service, str):
            service_specs.append({
                "name": service,
                "display_name": service,
                "label": service
            })
        else:
            name = service.get("name") or service.get("service_name")
            display_name = service.get("display_name") or service.get("displayName") or name
            label = service.get("label") or display_name or name
            if name or display_name:
                service_specs.append({
                    "name": name,
                    "display_name": display_name,
                    "label": label
                })
    service_names_json = json.dumps(service_specs)

    status_script = r"""
    $serviceSpecs = ConvertFrom-Json '__SERVICE_NAMES_JSON__'

    $disks = Get-PSDrive -PSProvider FileSystem |
        Where-Object { $_.Free -gt 0 } |
        Select-Object Name,
            @{N="FreeGB"; E={[math]::Round($_.Free / 1GB, 2)}},
            @{N="UsedGB"; E={[math]::Round($_.Used / 1GB, 2)}}

    $cpu = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average, 1)

    $ram = Get-CimInstance Win32_OperatingSystem
    $ramTotal = [math]::Round($ram.TotalVisibleMemorySize / 1MB, 2)
    $ramFree  = [math]::Round($ram.FreePhysicalMemory / 1MB, 2)
    $uptimeSeconds = [math]::Round(((Get-Date) - $ram.LastBootUpTime).TotalSeconds, 0)

    $services = @()
    foreach ($serviceSpec in $serviceSpecs) {
        $serviceName = $serviceSpec.name
        $displayName = $serviceSpec.display_name
        $label = $serviceSpec.label
        $svc = $null
        if ($serviceName) {
            $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        }
        if (($null -eq $svc) -and $displayName) {
            $svc = Get-Service | Where-Object { $_.DisplayName -eq $displayName } | Select-Object -First 1
        }
        if ($null -eq $svc) {
            $services += [PSCustomObject]@{
                Name = $serviceName
                DisplayName = $displayName
                Label = $label
                Status = "not_found"
            }
        } else {
            $services += [PSCustomObject]@{
                Name = $svc.Name
                DisplayName = $svc.DisplayName
                Label = $label
                Status = [string]$svc.Status
            }
        }
    }

    @{
        Disks    = $disks
        CpuLoad  = $cpu
        RamTotal = $ramTotal
        RamFree  = $ramFree
        UptimeSeconds = $uptimeSeconds
        Services = $services
    } | ConvertTo-Json -Depth 4
    """.replace("__SERVICE_NAMES_JSON__", service_names_json.replace("'", "''"))

    process_script = r"""
    $n=[int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
    if($n -lt 1){$n=1}
    $a=@{}
    Get-Process|%{$a[$_.Id]=if($null -eq $_.CPU){0}else{$_.CPU}}
    Start-Sleep -Seconds 1
    $p=Get-Process|%{
      $old=$a[$_.Id]
      $new=if($null -eq $_.CPU){0}else{$_.CPU}
      $d=if($null -eq $old){0}else{[math]::Max(0,$new-$old)}
      [pscustomobject]@{
        Name=$_.ProcessName
        Id=$_.Id
        CpuPercent=[math]::Round(($d/$n)*100,1)
        CpuSeconds=[math]::Round($new,1)
        MemoryMB=[math]::Round($_.WorkingSet64/1MB,1)
      }
    }
    @{
      TopCpu=($p|?{$_.Name -ne "Idle"}|sort CpuPercent -desc|select -first 5)
      TopMemory=($p|sort MemoryMB -desc|select -first 5)
    }|ConvertTo-Json -Depth 4
    """

    result = run_ps(
        host=server["host"],
        script=status_script,
        username=server.get("username"),
        password=server.get("password")
    )
    data = json.loads(result)

    process_result = run_ps(
        host=server["host"],
        script=process_script,
        username=server.get("username"),
        password=server.get("password")
    )
    process_data = json.loads(process_result)

    disks = data.get("Disks", [])
    if isinstance(disks, dict):
        disks = [disks]

    services = data.get("Services", [])
    if isinstance(services, dict):
        services = [services]

    top_cpu = process_data.get("TopCpu", [])
    if isinstance(top_cpu, dict):
        top_cpu = [top_cpu]

    top_memory = process_data.get("TopMemory", [])
    if isinstance(top_memory, dict):
        top_memory = [top_memory]

    return {
        "disks":     disks,
        "cpu_load":  float(data.get("CpuLoad", 0)),
        "ram_total": float(data.get("RamTotal", 0)),
        "ram_free":  float(data.get("RamFree", 0)),
        "uptime_seconds": int(float(data.get("UptimeSeconds", 0))),
        "services":  services,
        "top_cpu":   top_cpu,
        "top_memory": top_memory,
    }


# Оставляем для обратной совместимости
def check_disks(server: dict) -> list:
    return check_server(server)["disks"]
