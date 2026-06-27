"""
bot/backup_bot_winrm.py

WinRM-функции для модуля бэкапов (бот).
"""
import json
import os
from winrm_client import run_ps

DELETABLE_EXTENSIONS = {".bak", ".trn", ".dt", ".zip"}


def list_backup_files(host: str, backup_path: str,
                       username: str = None, password: str = None) -> list:
    """
    Возвращает список файлов рекурсивно из backup_path.
    Каждый файл: {file_name, full_path, size_gb, modified}
    """
    script = f"""
    $path = "{backup_path}"
    if (-not (Test-Path $path)) {{
        "[]"
        return
    }}
    $files = Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object Name, FullName,
            @{{N="SizeGB";  E={{[math]::Round($_.Length / 1GB, 4)}}}},
            @{{N="Modified"; E={{$_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")}}}}
    if ($files) {{ $files | ConvertTo-Json -Depth 2 }} else {{ "[]" }}
    """
    result = run_ps(host, script, username, password)
    if not result or result.strip() == "[]":
        return []

    data = json.loads(result)
    if isinstance(data, dict):
        data = [data]

    return [
        {
            "file_name":  f["Name"],
            "full_path":  f["FullName"],
            "size_gb":    float(f["SizeGB"]),
            "modified":   f["Modified"],
        }
        for f in data
    ]


def delete_backup_files(host: str, file_paths: list,
                         username: str = None, password: str = None) -> list:
    """
    Удаляет файлы на удалённом хосте.
    Проверяет расширение перед удалением (защита).
    Возвращает список (full_path, ok, error).
    """
    # Фильтруем — удаляем только разрешённые расширения
    safe_paths = [
        p for p in file_paths
        if os.path.splitext(p)[1].lower() in DELETABLE_EXTENSIONS
    ]
    blocked = set(file_paths) - set(safe_paths)
    results = [(p, False, "Запрещённое расширение") for p in blocked]

    if not safe_paths:
        return results

    paths_json = json.dumps(safe_paths).replace("'", "''")
    script = f"""
    $paths = '{paths_json}' | ConvertFrom-Json
    $results = @()
    foreach ($path in $paths) {{
        try {{
            Remove-Item -Path $path -Force -ErrorAction Stop
            $results += [PSCustomObject]@{{ Path=$path; OK=$true; Error="" }}
        }} catch {{
            $results += [PSCustomObject]@{{ Path=$path; OK=$false; Error=$_.Exception.Message }}
        }}
    }}
    $results | ConvertTo-Json -Depth 2
    """

    try:
        result = run_ps(host, script, username, password)
        if not result:
            return results + [(p, False, "Нет ответа") for p in safe_paths]

        data = json.loads(result)
        if isinstance(data, dict):
            data = [data]

        results += [
            (row["Path"], bool(row["OK"]), row.get("Error", ""))
            for row in data
        ]
    except Exception as e:
        results += [(p, False, str(e)) for p in safe_paths]

    return results