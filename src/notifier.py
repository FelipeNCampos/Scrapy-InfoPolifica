from threading import Lock

try:
    from windows_toasts import InteractableWindowsToaster, Toast, WindowsToaster
except ImportError:  # pragma: no cover
    InteractableWindowsToaster = None
    Toast = None
    WindowsToaster = None

APP_NAME = "Scrapy-InfoPolifica"
_TOAST_LOCK = Lock()
_NOTIFICATION_WARNING_SHOWN = False


def _warn_notification_failure(error):
    global _NOTIFICATION_WARNING_SHOWN

    if _NOTIFICATION_WARNING_SHOWN:
        return

    print(f"Notificacao Windows indisponivel: {error}")
    _NOTIFICATION_WARNING_SHOWN = True


def send_notification(title, message):
    if not WindowsToaster or not Toast:
        _warn_notification_failure("biblioteca windows-toasts nao disponivel")
        return False

    try:
        with _TOAST_LOCK:
            toast = Toast([title, message])

            try:
                WindowsToaster(APP_NAME).show_toast(toast)
            except Exception:
                if not InteractableWindowsToaster:
                    raise

                InteractableWindowsToaster(APP_NAME).show_toast(toast)
        return True
    except Exception as exc:
        _warn_notification_failure(exc)
        return False
