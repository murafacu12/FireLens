"""Entry point of FIRELENS.exe (PyInstaller). From source: python desktop_entry.py"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app.desktop import main

    main()
