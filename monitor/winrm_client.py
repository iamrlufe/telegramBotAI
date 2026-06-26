import os
import winrm


def run_ps(host: str, script: str, username: str = None, password: str = None) -> str:
    """
    Выполняет PowerShell скрипт на удалённом Windows сервере.
    Если username/password не переданы — берёт из WINRM_USERNAME / WINRM_PASSWORD.
    """
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
