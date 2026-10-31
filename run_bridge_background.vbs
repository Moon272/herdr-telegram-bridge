Set WshShell = CreateObject("WScript.Shell")
    WshShell.CurrentDirectory = "C:\Users\Vinh-Dev\Desktop\Projects\Herdr\herdr-telegram-bridge"
    WshShell.Run "C:\Users\Vinh-Dev\Desktop\Projects\Herdr\herdr-telegram-bridge\.venv\Scripts\python.exe bridge.py", 0, False