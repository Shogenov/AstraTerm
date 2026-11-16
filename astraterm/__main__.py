import sys

if "--version" in sys.argv:
    from . import __version__
    print(__version__)
elif "--diagnose" in sys.argv:
    import json
    from .core import detect_dependencies
    result = {"python": sys.version, "clients": detect_dependencies()}
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import Gtk, Vte
        result["gtk"] = "{}.{}.{}".format(Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
        result["vte"] = "{}.{}.{}".format(Vte.get_major_version(), Vte.get_minor_version(), Vte.get_micro_version())
    except Exception as exc:
        result["gui_error"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
else:
    from .app import main
    main()
