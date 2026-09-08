import multiprocessing
import os
import sys


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--chrome-launcher":
        import runpy
        helper, url = sys.argv[2:4]
        sys.argv = [helper, url]
        runpy.run_path(helper, run_name="__main__")
    elif "--self-test" in sys.argv:
        from smartops_desktop.selftest import main
        raise SystemExit(main())
    else:
        try:
            from smartops_desktop.gui import main
            raise SystemExit(main())
        except Exception:
            import traceback
            from pathlib import Path
            from smartops_desktop.core import data_root
            root = data_root()
            root.mkdir(parents=True, exist_ok=True)
            (root / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "SmartOps could not start. Details: " + str(root / "startup-error.log"), "SmartOps", 16)
            raise
