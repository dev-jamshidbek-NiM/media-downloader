from bgutil_manager import stop_bgutil
from ui import App

if __name__ == "__main__":
    app = App()
    try:
        app.mainloop()
    finally:
        # mainloop tugagandan keyin ham safety
        try:
            app._cancel_all_afters()
        except Exception:
            pass

        try:
            stop_bgutil(getattr(app, "_bgutil_proc", None))
        except Exception:
            pass

        try:
            app.destroy()
        except Exception:
            pass
