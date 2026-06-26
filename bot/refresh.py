import json
import os
from contextlib import contextmanager

import psycopg2
import winrm

SERVERS_FILE = "/app/config/servers.json"


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD")
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def run_ps(host: str, script: str, username: str = None, password: str = None) -> str:
    username = username or os.getenv("WINRM_USERNAME")
    password = password or os.getenv("WINRM_PASSWORD")

    session = winrm.Session(
        f"http://{host}:5985/wsman",
        auth=(username, password),
        transport="ntlm"
    )
    result = session.run_ps(script)

    if result.status_code != 0:
        raise Exception(result.std_err.decode("utf-8", errors="replace").strip())

    return result.std_out.decode("utf-8", errors="replace").strip()


def load_server(server_name: str) -> dict:
    with open(SERVERS_FILE) as f:
        servers = json.load(f)

    for server in servers:
        if server.get("name") == server_name:
            return server
    raise ValueError(f"Сервер {server_name} не найден в config/servers.json")


def normalize_services(server: dict) -> list:
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
    return service_specs


def check_server(server: dict) -> dict:
    service_specs_json = json.dumps(normalize_services(server))

    status_script = r"""
    $serviceSpecs = ConvertFrom-Json '__SERVICE_SPECS_JSON__'

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
        Disks = $disks
        CpuLoad = $cpu
        RamTotal = $ramTotal
        RamFree = $ramFree
        UptimeSeconds = $uptimeSeconds
        Services = $services
    } | ConvertTo-Json -Depth 4
    """.replace("__SERVICE_SPECS_JSON__", service_specs_json.replace("'", "''"))

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

    status_data = json.loads(run_ps(
        server["host"],
        status_script,
        username=server.get("username"),
        password=server.get("password")
    ))
    process_data = json.loads(run_ps(
        server["host"],
        process_script,
        username=server.get("username"),
        password=server.get("password")
    ))

    disks = status_data.get("Disks", [])
    if isinstance(disks, dict):
        disks = [disks]
    services = status_data.get("Services", [])
    if isinstance(services, dict):
        services = [services]
    top_cpu = process_data.get("TopCpu", [])
    if isinstance(top_cpu, dict):
        top_cpu = [top_cpu]
    top_memory = process_data.get("TopMemory", [])
    if isinstance(top_memory, dict):
        top_memory = [top_memory]

    return {
        "disks": disks,
        "cpu_load": float(status_data.get("CpuLoad", 0)),
        "ram_total": float(status_data.get("RamTotal", 0)),
        "ram_free": float(status_data.get("RamFree", 0)),
        "uptime_seconds": int(float(status_data.get("UptimeSeconds", 0))),
        "services": services,
        "top_cpu": top_cpu,
        "top_memory": top_memory,
    }


def save_online(server_name: str, info: dict):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO server_status
                (server_name, status, error, cpu_load, ram_total, ram_free, uptime_seconds)
            VALUES (%s, 'online', NULL, %s, %s, %s, %s)
            """,
            (
                server_name,
                info["cpu_load"],
                info["ram_total"],
                info["ram_free"],
                info["uptime_seconds"]
            )
        )

        for disk in info["disks"]:
            cur.execute(
                """
                INSERT INTO disk_metrics (server_name, disk_name, free_gb, used_gb)
                VALUES (%s, %s, %s, %s)
                """,
                (server_name, disk["Name"], float(disk["FreeGB"]), float(disk["UsedGB"]))
            )

        for service in info["services"]:
            service_name = service.get("Name")
            if not service_name:
                continue
            cur.execute(
                """
                INSERT INTO service_status (server_name, service_name, display_name, status)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    server_name,
                    service_name,
                    service.get("Label") or service.get("DisplayName") or service_name,
                    service.get("Status", "unknown")
                )
            )

        for metric_type, processes in (("cpu", info["top_cpu"]), ("memory", info["top_memory"])):
            for process in processes:
                cur.execute(
                    """
                    INSERT INTO process_metrics
                        (server_name, metric_type, process_name, process_id,
                         cpu_percent, cpu_seconds, memory_mb)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        server_name,
                        metric_type,
                        process.get("Name"),
                        process.get("Id"),
                        process.get("CpuPercent"),
                        process.get("CpuSeconds"),
                        process.get("MemoryMB")
                    )
                )


def save_offline(server_name: str, status: str, error: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO server_status (server_name, status, error)
            VALUES (%s, %s, %s)
            """,
            (server_name, status, error)
        )


def parse_status(error_text: str) -> str:
    e = error_text.lower()
    if "credentials were rejected" in e:
        return "auth_failed"
    if "access is denied" in e:
        return "access_denied"
    if "timed out" in e:
        return "timeout"
    if "name or service not known" in e:
        return "dns_error"
    if "connection refused" in e:
        return "winrm_refused"
    if "max retries exceeded" in e:
        return "host_unreachable"
    return "unknown"


def refresh_server(server_name: str):
    try:
        server = load_server(server_name)
        info = check_server(server)
        save_online(server_name, info)
        return True, None
    except Exception as e:
        error = str(e)
        try:
            save_offline(server_name, parse_status(error), error)
        except Exception:
            pass
        return False, error
